"""Provider-neutral failure evidence: the structured object later phases reason over."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class RepositoryInfo(BaseModel):
    full_name: str
    owner: str
    name: str
    default_branch: str | None = None
    html_url: str | None = None
    private: bool = False


class WorkflowInfo(BaseModel):
    id: int
    name: str | None = None
    path: str | None = None


class RunInfo(BaseModel):
    id: int
    run_number: int
    run_attempt: int
    event: str
    status: str | None = None
    conclusion: str | None = None
    head_branch: str | None = None
    head_sha: str
    html_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class FailedStep(BaseModel):
    number: int
    name: str
    conclusion: str | None = None


class JobLogExcerpt(BaseModel):
    available: bool
    content: str | None = None
    error_excerpt: str | None = None  # the failing section, extracted from `content`
    total_bytes: int | None = None
    truncated: bool = False
    error: str | None = None


class FailedJobEvidence(BaseModel):
    id: int
    name: str
    conclusion: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    html_url: str | None = None
    runner_name: str | None = None
    labels: list[str] = []
    failed_steps: list[FailedStep] = []
    log: JobLogExcerpt


class ChangedFile(BaseModel):
    filename: str
    status: str | None = None
    additions: int = 0
    deletions: int = 0


class CommitInfo(BaseModel):
    sha: str
    message: str | None = None
    author_name: str | None = None
    authored_at: datetime | None = None
    html_url: str | None = None
    changed_files_available: bool = False
    changed_files: list[ChangedFile] = []


class CollectionError(BaseModel):
    source: str
    message: str


class FailureEvidence(BaseModel):
    provider: Literal["github_actions"] = "github_actions"
    delivery_id: str | None = None
    collected_at: datetime
    repository: RepositoryInfo
    workflow: WorkflowInfo
    run: RunInfo
    status: str
    failed_stage: str | None = None
    failed_jobs: list[FailedJobEvidence] = []
    commit: CommitInfo | None = None
    errors: list[CollectionError] = []

    def log_summary(self, error_excerpt_chars: int = 1200) -> dict[str, Any]:
        """Evidence without full log contents, suitable for application logs.

        The failing section is kept (shortened): it is the part an operator reading
        the logs actually needs. GitHub masks registered secrets in job logs as `***`.
        """
        data = self.model_dump(mode="json")
        for job in data["failed_jobs"]:
            job["log"].pop("content", None)
            excerpt = job["log"].get("error_excerpt")
            if excerpt and len(excerpt) > error_excerpt_chars:
                job["log"]["error_excerpt"] = excerpt[-error_excerpt_chars:]
        return data
