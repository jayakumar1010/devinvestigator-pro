"""GitHub credentials: a token, or a GitHub App installation token."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
import jwt

from app.core.config import Settings
from app.integrations.github.errors import GitHubAPIError

# Installation tokens are requested with read-only permissions, whatever the App itself was granted.
READ_ONLY_PERMISSIONS = {"actions": "read", "contents": "read", "metadata": "read"}


class GitHubAuth(Protocol):
    async def authorization(self, http: httpx.AsyncClient, installation_id: int | None) -> str:
        """Return an Authorization header value."""
        ...


class TokenAuth:
    def __init__(self, token: str) -> None:
        self._token = token

    def __repr__(self) -> str:
        return "TokenAuth(token='***')"

    async def authorization(self, http: httpx.AsyncClient, installation_id: int | None) -> str:
        return f"Bearer {self._token}"


@dataclass
class _CachedToken:
    value: str
    expires_at: float


class AppAuth:
    def __init__(self, app_id: str, private_key: str, clock: Callable[[], float] = time.time) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._clock = clock
        self._tokens: dict[int, _CachedToken] = {}
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"AppAuth(app_id={self._app_id!r})"

    def _app_jwt(self) -> str:
        now = int(self._clock())
        claims = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(claims, self._private_key, algorithm="RS256")

    async def authorization(self, http: httpx.AsyncClient, installation_id: int | None) -> str:
        path = f"/app/installations/{installation_id}/access_tokens"
        if installation_id is None:
            raise GitHubAPIError(None, path, "GitHub App auth requires an installation id")

        async with self._lock:
            cached = self._tokens.get(installation_id)
            if cached is None or cached.expires_at - 300 <= self._clock():
                cached = await self._create_installation_token(http, path)
                self._tokens[installation_id] = cached
        return f"Bearer {cached.value}"

    async def _create_installation_token(self, http: httpx.AsyncClient, path: str) -> _CachedToken:
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self._app_jwt()}"}
        try:
            response = await http.post(path, json={"permissions": READ_ONLY_PERMISSIONS}, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubAPIError(None, path, type(exc).__name__) from exc
        if response.status_code != 201:
            raise GitHubAPIError(response.status_code, path, "could not create installation token")
        data = response.json()
        return _CachedToken(data["token"], datetime.fromisoformat(data["expires_at"]).timestamp())


def build_github_auth(settings: Settings) -> GitHubAuth | None:
    if settings.github_app_id:
        return AppAuth(settings.github_app_id, _load_app_private_key(settings))
    if settings.github_token:
        return TokenAuth(settings.github_token.get_secret_value())
    return None


def _load_app_private_key(settings: Settings) -> str:
    if settings.github_app_private_key:
        return settings.github_app_private_key.get_secret_value().replace("\\n", "\n")
    if settings.github_app_private_key_path:
        return Path(settings.github_app_private_key_path).read_text()
    raise ValueError("GITHUB_APP_ID is set but GITHUB_APP_PRIVATE_KEY / GITHUB_APP_PRIVATE_KEY_PATH is not")
