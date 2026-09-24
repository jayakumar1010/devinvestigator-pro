import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import health, webhooks
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.session import create_engine, create_sessionmaker, database_backend, init_db
from app.orchestrator import InvestigationOrchestrator
from app.web import routes as web

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
logger = logging.getLogger("devinvestigator")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database_url = settings.database_url.get_secret_value()
    engine = create_engine(database_url)
    await init_db(engine)
    sessionmaker = create_sessionmaker(engine)
    orchestrator = InvestigationOrchestrator(settings, sessionmaker)
    await orchestrator.start()
    app.state.orchestrator = orchestrator
    app.state.sessionmaker = sessionmaker
    logger.info(
        "Investigations: database=%s auto_analyze=%s mode=%s model=%s",
        database_backend(database_url), settings.auto_analyze, settings.analysis_mode, settings.active_model,
    )
    logger.info(
        "Web page /investigations: %s",
        "enabled (HTTP Basic auth)" if settings.dashboard_password else "disabled (set DASHBOARD_PASSWORD)",
    )
    try:
        yield
    finally:
        await orchestrator.stop()
        await engine.dispose()


docs = settings.enable_api_docs
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if docs else None,
    redoc_url="/redoc" if docs else None,
    openapi_url="/openapi.json" if docs else None,
)
app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(web.router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }


logger.info("%s %s starting (environment=%s)", settings.app_name, settings.app_version, settings.environment)
# Report which settings are present, never their values.
logger.info(
    "GitHub integration: auth=%s webhook_secret_configured=%s api_url=%s",
    settings.github_auth_mode, settings.github_webhook_secret is not None, settings.github_api_url,
)
