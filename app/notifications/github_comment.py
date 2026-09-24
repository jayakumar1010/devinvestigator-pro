"""Post an investigation result as a GitHub comment.

This is the only place DevInvestigator writes to GitHub. It uses its own token
(GITHUB_COMMENT_TOKEN), is off unless NOTIFY_GITHUB_COMMENTS=true, and calls exactly two
endpoints: create a pull request comment, or create a commit comment.
"""

import logging
from typing import Any, Self

import httpx

from app.analysis.models import AnalysisResult
from app.core.config import Settings
from app.evidence.models import FailureEvidence
from app.integrations.github.client import API_VERSION, GitHubClient, _seg
from app.notifications.render import render_comment

logger = logging.getLogger(__name__)


class GitHubCommentError(Exception):
    pass


class GitHubCommenter:
    """Writes comments, nothing else."""

    def __init__(
        self, api_url: str, token: str, *, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "DevInvestigator"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _post(self, path: str, body: str) -> str:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Authorization": f"Bearer {self._token}",
        }
        try:
            response = await self._http.post(path, json={"body": body}, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubCommentError(f"comment request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("message")
            except ValueError:
                message = None
            raise GitHubCommentError(f"GitHub returned {response.status_code} for {path}: {message or 'error'}")
        payload: dict[str, Any] = response.json()
        return payload.get("html_url", "")

    async def comment_on_pull_request(self, owner: str, repo: str, number: int, body: str) -> str:
        return await self._post(f"/repos/{_seg(owner)}/{_seg(repo)}/issues/{_seg(number)}/comments", body)

    async def comment_on_commit(self, owner: str, repo: str, sha: str, body: str) -> str:
        return await self._post(f"/repos/{_seg(owner)}/{_seg(repo)}/commits/{_seg(sha)}/comments", body)


def page_url(settings: Settings, investigation_id: int) -> str | None:
    if not settings.public_base_url:
        return None
    return f"{settings.public_base_url.rstrip('/')}/investigations/{investigation_id}"


async def post_investigation_comment(
    read_client: GitHubClient,
    settings: Settings,
    investigation_id: int,
    evidence: FailureEvidence,
    result: AnalysisResult,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Comment on the pull request the commit belongs to, or on the commit itself."""
    if settings.github_comment_token is None:
        raise GitHubCommentError("NOTIFY_GITHUB_COMMENTS is on but GITHUB_COMMENT_TOKEN is not set")

    owner, repo = evidence.repository.owner, evidence.repository.name
    sha = evidence.run.head_sha
    body = render_comment(result, page_url=page_url(settings, investigation_id))

    pulls: list[int] = []
    try:
        pulls = await read_client.list_pull_requests_for_commit(owner, repo, sha)
    except Exception as exc:  # not fatal: fall back to a commit comment
        logger.info("Could not look up pull requests for %s: %s", sha[:12], exc)

    async with GitHubCommenter(
        settings.github_api_url,
        settings.github_comment_token.get_secret_value(),
        timeout=settings.github_request_timeout_seconds,
        transport=transport,
    ) as commenter:
        if pulls:
            return await commenter.comment_on_pull_request(owner, repo, pulls[0], body)
        return await commenter.comment_on_commit(owner, repo, sha, body)
