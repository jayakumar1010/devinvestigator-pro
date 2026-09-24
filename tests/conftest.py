import hashlib
import hmac
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.database.session import create_engine, create_sessionmaker, init_db
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
WEBHOOK_SECRET = "test-webhook-secret"
GITHUB_TOKEN = "test-github-token"
API_URL = "https://api.github.com"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


Handler = httpx.Response | Callable[[httpx.Request], httpx.Response]


class FakeGitHub:
    """In-memory GitHub API: routes keyed by (method, host, path); records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str, str], Handler] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, url: str, handler: Handler) -> None:
        parsed = httpx.URL(url)
        self.routes[(method, parsed.host, parsed.path)] = handler

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.host, request.url.path))
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return route(request) if callable(route) else route

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, github_webhook_secret=WEBHOOK_SECRET, github_token=GITHUB_TOKEN)


@pytest.fixture
def api_client(settings: Settings) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/test.db"


@pytest.fixture
async def sessionmaker(database_url: str):
    engine = create_engine(database_url)
    await init_db(engine)
    yield create_sessionmaker(engine)
    await engine.dispose()
