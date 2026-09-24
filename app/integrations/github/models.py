"""Pydantic models for the parts of GitHub webhook and REST payloads we use.

Unknown fields are ignored so GitHub adding fields never breaks parsing.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class GitHubModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Account(GitHubModel):
    login: str
    id: int | None = None


class Repository(GitHubModel):
    id: int
    name: str
    full_name: str
    owner: Account
    private: bool = False
    html_url: str | None = None
    default_branch: str | None = None


class CommitAuthor(GitHubModel):
    name: str | None = None
    date: datetime | None = None


class HeadCommit(GitHubModel):
    id: str
    message: str
    timestamp: datetime | None = None
    author: CommitAuthor | None = None


class WorkflowRun(GitHubModel):
    id: int
    name: str | None = None
    workflow_id: int
    path: str | None = None
    display_title: str | None = None
    run_number: int
    run_attempt: int = 1
    event: str
    status: str | None = None
    conclusion: str | None = None
    head_branch: str | None = None
    head_sha: str
    html_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    run_started_at: datetime | None = None
    head_commit: HeadCommit | None = None


class WorkflowStep(GitHubModel):
    number: int
    name: str
    status: str | None = None
    conclusion: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkflowJob(GitHubModel):
    id: int
    run_id: int
    run_attempt: int | None = None
    name: str
    status: str | None = None
    conclusion: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    html_url: str | None = None
    runner_name: str | None = None
    labels: list[str] = []
    steps: list[WorkflowStep] = []


class CommitDetail(GitHubModel):
    message: str
    author: CommitAuthor | None = None


class CommitFile(GitHubModel):
    filename: str
    status: str | None = None
    additions: int = 0
    deletions: int = 0
    patch: str | None = None  # unified diff, present in commit and compare responses


class Commit(GitHubModel):
    sha: str
    html_url: str | None = None
    commit: CommitDetail
    files: list[CommitFile] = []


class WorkflowRef(GitHubModel):
    id: int
    name: str | None = None
    path: str | None = None


class Installation(GitHubModel):
    id: int


class WorkflowRunEvent(GitHubModel):
    """Payload of a `workflow_run` webhook delivery."""

    action: str
    workflow_run: WorkflowRun
    workflow: WorkflowRef | None = None
    repository: Repository
    installation: Installation | None = None
    sender: Account | None = None


class Comparison(GitHubModel):
    """Response of GET /repos/{owner}/{repo}/compare/{base}...{head}."""

    status: str | None = None
    ahead_by: int = 0
    behind_by: int = 0
    total_commits: int = 0
    commits: list[Commit] = []
    files: list[CommitFile] = []
