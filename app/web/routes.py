"""Read-only web page for investigations, behind HTTP Basic auth.

Deliberately read-only (no forms or buttons that change anything), so browser-sent Basic
credentials cannot be abused for cross-site requests. Retry stays in the CLI.
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import repository
from app.web import views
from app.web.auth import require_dashboard_user

STATUSES = ("queued", "running", "completed", "failed", "collected")
REFRESH_SECONDS = 10
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))  # autoescape on
router = APIRouter(include_in_schema=False, dependencies=[Depends(require_dashboard_user)])


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sessionmaker = getattr(request.app.state, "sessionmaker", None)
    if sessionmaker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The database is not ready")
    return sessionmaker


def _render(request: Request, template: str, context: dict) -> HTMLResponse:
    response = templates.TemplateResponse(request, template, context)
    response.headers.update(SECURITY_HEADERS)
    return response


@router.get("/investigations", response_class=HTMLResponse)
async def investigation_list(
    request: Request,
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    status: str | None = None,
) -> HTMLResponse:
    if status not in STATUSES:
        status = None
    async with sessionmaker() as session:
        rows = await repository.list_recent(session, limit=100, status=status)
        counts = await repository.count_by_status(session)
    busy = counts.get("queued", 0) + counts.get("running", 0)
    return _render(request, "list.html", {
        "rows": [views.summary_row(row) for row in rows],
        "counts": counts,
        "total": sum(counts.values()),
        "status": status,
        "statuses": STATUSES,
        "refresh": REFRESH_SECONDS if busy else None,
    })


@router.get("/investigations/{investigation_id}", response_class=HTMLResponse)
async def investigation_detail(
    request: Request,
    investigation_id: int,
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> HTMLResponse:
    async with sessionmaker() as session:
        investigation = await repository.get(session, investigation_id)
    if investigation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Investigation not found")
    return _render(request, "detail.html", {
        "d": views.detail(investigation),
        "refresh": REFRESH_SECONDS if investigation.status in ("queued", "running") else None,
    })
