"""Collect GitHub Actions failure evidence and normalize it into `FailureEvidence`.

Fetching lives in `collect_workflow_failure_evidence`; normalization is the pure function
`build_failure_evidence`. Collection is best-effort: a source that fails is recorded in
`errors` instead of aborting the whole investigation.
"""

import asyncio
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import TypeVar

from app.evidence.logs import extract_error_excerpt, strip_ansi
from app.evidence.models import (
    ChangedFile,
    CollectionError,
    CommitInfo,
    FailedJobEvidence,
    FailedStep,
    FailureEvidence,
    JobLogExcerpt,
    RepositoryInfo,
    RunInfo,
    WorkflowInfo,
)
from app.integrations.github.client import GitHubClient, JobLog
from app.integrations.github.errors import GitHubAPIError
from app.integrations.github.events import failed_jobs, failed_steps
from app.integrations.github.models import Commit, Repository, WorkflowJob, WorkflowRunEvent

T = TypeVar("T")


async def _attempt(call: Awaitable[T], source: str, errors: list[CollectionError]) -> T | None:
    try:
        return await call
    except GitHubAPIError as exc:
        errors.append(CollectionError(source=source, message=str(exc)))
        return None


async def _fetch_log(client: GitHubClient, owner: str, repo: str, job_id: int) -> JobLog | GitHubAPIError:
    try:
        return await client.get_job_logs(owner, repo, job_id)
    except GitHubAPIError as exc:
        return exc


async def collect_workflow_failure_evidence(
    client: GitHubClient,
    event: WorkflowRunEvent,
    *,
    delivery_id: str | None = None,
    max_job_logs: int = 5,
) -> FailureEvidence:
    owner, repo = event.repository.owner.login, event.repository.name
    run = event.workflow_run
    errors: list[CollectionError] = []

    repository, jobs, commit = await asyncio.gather(
        _attempt(client.get_repository(owner, repo), "repository", errors),
        _attempt(client.get_workflow_jobs(owner, repo, run.id, attempt=run.run_attempt), "workflow_jobs", errors),
        _attempt(client.get_commit(owner, repo, run.head_sha), "commit", errors),
    )
    jobs = jobs or []

    log_targets = failed_jobs(jobs)[:max_job_logs]
    logs = await asyncio.gather(*(_fetch_log(client, owner, repo, job.id) for job in log_targets))

    return build_failure_evidence(
        event,
        repository=repository,
        jobs=jobs,
        job_logs={job.id: log for job, log in zip(log_targets, logs)},
        commit=commit,
        errors=errors,
        delivery_id=delivery_id,
    )


async def collect_evidence_for_run(
    client: GitHubClient, owner: str, repo: str, run_id: int, *, max_job_logs: int = 5
) -> FailureEvidence:
    """Evidence for a run looked up by id, for manual investigations (no webhook involved)."""
    run, repository = await asyncio.gather(
        client.get_workflow_run(owner, repo, run_id), client.get_repository(owner, repo)
    )
    event = WorkflowRunEvent(action="completed", workflow_run=run, repository=repository)
    return await collect_workflow_failure_evidence(client, event, max_job_logs=max_job_logs)


def build_failure_evidence(
    event: WorkflowRunEvent,
    *,
    repository: Repository | None,
    jobs: list[WorkflowJob],
    job_logs: dict[int, JobLog | GitHubAPIError],
    commit: Commit | None,
    errors: list[CollectionError],
    delivery_id: str | None = None,
    collected_at: datetime | None = None,
) -> FailureEvidence:
    run = event.workflow_run
    repo = repository or event.repository
    failed = failed_jobs(jobs)

    return FailureEvidence(
        delivery_id=delivery_id,
        collected_at=collected_at or datetime.now(timezone.utc),
        repository=RepositoryInfo(
            full_name=repo.full_name,
            owner=repo.owner.login,
            name=repo.name,
            default_branch=repo.default_branch,
            html_url=repo.html_url,
            private=repo.private,
        ),
        workflow=WorkflowInfo(
            id=run.workflow_id,
            name=run.name or (event.workflow.name if event.workflow else None),
            path=run.path or (event.workflow.path if event.workflow else None),
        ),
        run=RunInfo(
            id=run.id,
            run_number=run.run_number,
            run_attempt=run.run_attempt,
            event=run.event,
            status=run.status,
            conclusion=run.conclusion,
            head_branch=run.head_branch,
            head_sha=run.head_sha,
            html_url=run.html_url,
            created_at=run.created_at,
            updated_at=run.updated_at,
        ),
        status=(run.conclusion or "unknown").upper(),
        failed_stage=_failed_stage(failed),
        failed_jobs=[_job_evidence(job, job_logs.get(job.id)) for job in failed],
        commit=_commit_info(event, commit),
        errors=list(errors),
    )


def _failed_stage(failed: list[WorkflowJob]) -> str | None:
    for job in failed:
        steps = failed_steps(job)
        if steps:
            return f"{job.name} / {steps[0].name}"
    return failed[0].name if failed else None


def _job_evidence(job: WorkflowJob, log: JobLog | GitHubAPIError | None) -> FailedJobEvidence:
    if isinstance(log, JobLog):
        content = strip_ansi(log.content)
        excerpt = JobLogExcerpt(
            available=True,
            content=content,
            error_excerpt=extract_error_excerpt(content),
            total_bytes=log.total_bytes,
            truncated=log.truncated,
        )
    elif isinstance(log, GitHubAPIError):
        excerpt = JobLogExcerpt(available=False, error=str(log))
    else:
        excerpt = JobLogExcerpt(available=False, error="not fetched: failed-job log limit reached")

    return FailedJobEvidence(
        id=job.id,
        name=job.name,
        conclusion=job.conclusion,
        started_at=job.started_at,
        completed_at=job.completed_at,
        html_url=job.html_url,
        runner_name=job.runner_name,
        labels=job.labels,
        failed_steps=[FailedStep(number=s.number, name=s.name, conclusion=s.conclusion) for s in failed_steps(job)],
        log=excerpt,
    )


def _commit_info(event: WorkflowRunEvent, commit: Commit | None) -> CommitInfo | None:
    if commit is not None:
        author = commit.commit.author
        return CommitInfo(
            sha=commit.sha,
            message=commit.commit.message,
            author_name=author.name if author else None,
            authored_at=author.date if author else None,
            html_url=commit.html_url,
            changed_files_available=True,
            changed_files=[
                ChangedFile(filename=f.filename, status=f.status, additions=f.additions, deletions=f.deletions)
                for f in commit.files
            ],
        )

    head = event.workflow_run.head_commit
    if head is None:
        return None
    return CommitInfo(
        sha=event.workflow_run.head_sha,
        message=head.message,
        author_name=head.author.name if head.author else None,
        authored_at=head.timestamp,
    )

