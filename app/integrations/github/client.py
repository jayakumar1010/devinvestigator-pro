"""Read-only GitHub REST API client for GitHub Actions investigations.

All data methods go through `_get`; this client never sends write requests. The only
non-GET call in this package is installation-token creation in `auth.py`, which does
not modify any repository.
"""

import json
from dataclasses import dataclass
from typing import Self
from urllib.parse import quote, urlsplit

import httpx

from app.core.config import Settings
from app.integrations.github.auth import GitHubAuth, build_github_auth
from app.integrations.github.errors import GitHubAPIError
from app.integrations.github.events import failed_jobs
from app.integrations.github.models import Commit, Comparison, Repository, WorkflowJob, WorkflowRun

API_VERSION = "2022-11-28"
JOBS_PER_PAGE = 100
MAX_JOB_PAGES = 10
MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class JobLog:
    content: str
    total_bytes: int
    truncated: bool


@dataclass(frozen=True)
class FileContent:
    path: str
    kind: str  # "file" or "directory"
    text: str = ""
    entries: tuple[str, ...] = ()
    total_bytes: int = 0
    binary: bool = False


def _directory_entries(body: bytes) -> tuple[str, ...] | None:
    """The contents API answers a directory path with a JSON list, even when raw content is requested."""
    if not body.lstrip().startswith(b"["):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, list) or not all(isinstance(e, dict) and "type" in e and "path" in e for e in data):
        return None
    return tuple(f"{e['type']} {e['path']}" for e in data)


def _seg(value: str | int) -> str:
    return quote(str(value), safe="")


def _error_message(response: httpx.Response) -> str:
    try:
        message = response.json().get("message")
    except ValueError:
        message = None
    return str(message or response.reason_phrase)[:200]


def _tail_to_job_log(tail: bytes, total_bytes: int) -> JobLog:
    truncated = total_bytes > len(tail)
    if truncated:
        # Drop the partial first line left by cutting the log at a byte offset.
        newline = tail.find(b"\n")
        if newline != -1:
            tail = tail[newline + 1 :]
    return JobLog(tail.decode("utf-8", errors="replace"), total_bytes, truncated)


class GitHubClient:
    def __init__(
        self,
        api_url: str,
        auth: GitHubAuth | None,
        *,
        installation_id: int | None = None,
        timeout: float = 30.0,
        log_max_bytes: int = 200_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth
        self._installation_id = installation_id
        self._log_max_bytes = log_max_bytes
        headers = {"User-Agent": "DevInvestigator"}
        self._api = httpx.AsyncClient(
            base_url=api_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )
        # Pre-signed log download URLs get their own client so GitHub credentials are never sent there.
        self._download = httpx.AsyncClient(
            headers=headers, timeout=timeout, transport=transport, follow_redirects=True
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        installation_id: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        return cls(
            settings.github_api_url,
            build_github_auth(settings),
            installation_id=installation_id,
            timeout=settings.github_request_timeout_seconds,
            log_max_bytes=settings.github_log_max_bytes,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._download.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _get(
        self, path: str, params: dict[str, str | int] | None = None, *, accept: str = "application/vnd.github+json"
    ) -> httpx.Response:
        headers = {"Accept": accept, "X-GitHub-Api-Version": API_VERSION}
        if self._auth is not None:
            headers["Authorization"] = await self._auth.authorization(self._api, self._installation_id)
        try:
            response = await self._api.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubAPIError(None, path, type(exc).__name__) from exc
        if response.status_code >= 400:
            raise GitHubAPIError(response.status_code, path, _error_message(response))
        return response

    async def get_repository(self, owner: str, repo: str) -> Repository:
        response = await self._get(f"/repos/{_seg(owner)}/{_seg(repo)}")
        return Repository.model_validate(response.json())

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> WorkflowRun:
        response = await self._get(f"/repos/{_seg(owner)}/{_seg(repo)}/actions/runs/{_seg(run_id)}")
        return WorkflowRun.model_validate(response.json())

    async def get_workflow_jobs(
        self, owner: str, repo: str, run_id: int, attempt: int | None = None
    ) -> list[WorkflowJob]:
        """Jobs of one attempt of a run (or of the latest attempt when `attempt` is None)."""
        base = f"/repos/{_seg(owner)}/{_seg(repo)}/actions/runs/{_seg(run_id)}"
        if attempt is None:
            path, params = f"{base}/jobs", {"filter": "latest"}
        else:
            path, params = f"{base}/attempts/{_seg(attempt)}/jobs", {}

        jobs: list[WorkflowJob] = []
        for page in range(1, MAX_JOB_PAGES + 1):
            data = (await self._get(path, {**params, "per_page": JOBS_PER_PAGE, "page": page})).json()
            batch = data.get("jobs", [])
            jobs.extend(WorkflowJob.model_validate(job) for job in batch)
            if len(batch) < JOBS_PER_PAGE or len(jobs) >= data.get("total_count", 0):
                break
        return jobs

    async def get_failed_jobs(
        self, owner: str, repo: str, run_id: int, attempt: int | None = None
    ) -> list[WorkflowJob]:
        return failed_jobs(await self.get_workflow_jobs(owner, repo, run_id, attempt))

    async def get_job_logs(self, owner: str, repo: str, job_id: int) -> JobLog:
        """Plain-text job log, keeping only the last `log_max_bytes` bytes (errors are usually at the end)."""
        path = f"/repos/{_seg(owner)}/{_seg(repo)}/actions/jobs/{_seg(job_id)}/logs"
        response = await self._get(path)
        if not response.is_redirect:
            content = response.content
            return _tail_to_job_log(content[-self._log_max_bytes :], len(content))

        location = response.headers.get("location", "")
        if urlsplit(location).scheme != "https":
            raise GitHubAPIError(response.status_code, path, "log redirect is missing or not https")
        return await self._download_log(location, path)

    async def _download_log(self, url: str, path: str) -> JobLog:
        tail = bytearray()
        total = 0
        try:
            async with self._download.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise GitHubAPIError(response.status_code, path, "log download failed")
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    tail += chunk
                    if len(tail) > self._log_max_bytes:
                        del tail[: len(tail) - self._log_max_bytes]
        except httpx.HTTPError as exc:
            # Deliberately omit the URL: it carries a signed query string.
            raise GitHubAPIError(None, path, f"log download failed: {type(exc).__name__}") from exc
        return _tail_to_job_log(bytes(tail), total)

    async def get_commit(self, owner: str, repo: str, sha: str) -> Commit:
        response = await self._get(f"/repos/{_seg(owner)}/{_seg(repo)}/commits/{_seg(sha)}")
        return Commit.model_validate(response.json())

    async def get_file(self, owner: str, repo: str, path: str, ref: str) -> FileContent:
        """A file's raw content, or a directory listing, at `ref`. "" is the repository root."""
        clean = path.strip("/")
        url = f"/repos/{_seg(owner)}/{_seg(repo)}/contents"
        if clean:
            url += "/" + quote(clean, safe="/")
        response = await self._get(url, {"ref": ref}, accept="application/vnd.github.raw+json")
        body = response.content
        entries = _directory_entries(body)
        if entries is not None:
            return FileContent(path=clean, kind="directory", entries=entries)
        data = body[:MAX_FILE_BYTES]
        if b"\x00" in data[:8192]:
            return FileContent(path=clean, kind="file", total_bytes=len(body), binary=True)
        return FileContent(path=clean, kind="file", text=data.decode("utf-8", errors="replace"), total_bytes=len(body))

    async def list_workflow_runs(
        self,
        owner: str,
        repo: str,
        workflow_id: int,
        *,
        branch: str | None = None,
        status: str | None = None,
        per_page: int = 20,
    ) -> list[WorkflowRun]:
        params: dict[str, str | int] = {"per_page": per_page}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status
        response = await self._get(
            f"/repos/{_seg(owner)}/{_seg(repo)}/actions/workflows/{_seg(workflow_id)}/runs", params
        )
        return [WorkflowRun.model_validate(run) for run in response.json().get("workflow_runs", [])]

    async def compare_commits(self, owner: str, repo: str, base: str, head: str) -> Comparison:
        response = await self._get(f"/repos/{_seg(owner)}/{_seg(repo)}/compare/{_seg(base)}...{_seg(head)}")
        return Comparison.model_validate(response.json())

    async def list_pull_requests_for_commit(self, owner: str, repo: str, sha: str) -> list[int]:
        """Open pull requests that contain this commit, newest first (read-only)."""
        response = await self._get(f"/repos/{_seg(owner)}/{_seg(repo)}/commits/{_seg(sha)}/pulls")
        return [pull["number"] for pull in response.json() if pull.get("state") == "open"]
