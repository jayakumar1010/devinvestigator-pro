import json
from datetime import datetime, timezone

import httpx

from app.evidence.collector import (
    build_failure_evidence,
    collect_workflow_failure_evidence,
)
from app.integrations.github.client import GitHubClient, JobLog
from app.integrations.github.errors import GitHubAPIError
from app.integrations.github.auth import TokenAuth
from app.integrations.github.models import Commit, WorkflowJob, WorkflowRunEvent
from tests.conftest import API_URL, FakeGitHub, load_fixture

REPO = f"{API_URL}/repos/acme/frontend"
RUN_ID = 9876543210
SHA = "abc123def4567890abc123def4567890abc123de"
SIGNED_LOG_URL = "https://results-receiver.actions.githubusercontent.com/logs/2002?sig=SIGNED"
LOG_TEXT = "Run npm ci\nnpm ERR! code ERESOLVE\nnpm ERR! Could not resolve dependency\nError: Process completed with exit code 1.\n"


def event() -> WorkflowRunEvent:
    return WorkflowRunEvent.model_validate(load_fixture("workflow_run_failed.json"))


def jobs() -> list[WorkflowJob]:
    return [WorkflowJob.model_validate(j) for j in load_fixture("workflow_jobs.json")["jobs"]]


def test_build_failure_evidence_normalizes_everything() -> None:
    collected_at = datetime(2026, 9, 15, 10, 5, tzinfo=timezone.utc)
    evidence = build_failure_evidence(
        event(),
        repository=None,
        jobs=jobs(),
        job_logs={2002: JobLog(LOG_TEXT, len(LOG_TEXT), False)},
        commit=Commit.model_validate(load_fixture("commit.json")),
        errors=[],
        delivery_id="delivery-1",
        collected_at=collected_at,
    )

    assert evidence.model_dump(mode="json") == {
        "provider": "github_actions",
        "delivery_id": "delivery-1",
        "collected_at": "2026-09-15T10:05:00Z",
        "repository": {
            "full_name": "acme/frontend",
            "owner": "acme",
            "name": "frontend",
            "default_branch": "main",
            "html_url": "https://github.com/acme/frontend",
            "private": False,
        },
        "workflow": {"id": 123456, "name": "CI", "path": ".github/workflows/ci.yml"},
        "run": {
            "id": RUN_ID,
            "run_number": 182,
            "run_attempt": 1,
            "event": "push",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "head_sha": SHA,
            "html_url": "https://github.com/acme/frontend/actions/runs/9876543210",
            "created_at": "2026-09-15T10:00:00Z",
            "updated_at": "2026-09-15T10:04:12Z",
        },
        "status": "FAILURE",
        "failed_stage": "build / Install dependencies",
        "failed_jobs": [
            {
                "id": 2002,
                "name": "build",
                "conclusion": "failure",
                "started_at": "2026-09-15T10:00:10Z",
                "completed_at": "2026-09-15T10:04:00Z",
                "html_url": "https://github.com/acme/frontend/actions/runs/9876543210/job/2002",
                "runner_name": "GitHub Actions 2",
                "labels": ["ubuntu-latest"],
                "failed_steps": [{"number": 3, "name": "Install dependencies", "conclusion": "failure"}],
                "log": {
                    "available": True,
                    "content": LOG_TEXT,
                    "error_excerpt": None,  # this sample has no ##[error] marker
                    "total_bytes": len(LOG_TEXT),
                    "truncated": False,
                    "error": None,
                },
            }
        ],
        "commit": {
            "sha": SHA,
            "message": "Upgrade dependencies",
            "author_name": "Dev One",
            "authored_at": "2026-09-15T09:59:30Z",
            "html_url": f"https://github.com/acme/frontend/commit/{SHA}",
            "changed_files_available": True,
            "changed_files": [
                {"filename": "package.json", "status": "modified", "additions": 3, "deletions": 3},
                {"filename": "package-lock.json", "status": "modified", "additions": 27, "deletions": 9},
            ],
        },
        "errors": [],
    }


def test_log_failure_is_recorded_on_the_job() -> None:
    evidence = build_failure_evidence(
        event(),
        repository=None,
        jobs=jobs(),
        job_logs={2002: GitHubAPIError(410, "/logs", "Gone")},
        commit=None,
        errors=[],
    )
    log = evidence.failed_jobs[0].log
    assert log.available is False
    assert log.content is None
    assert "410" in log.error


def test_jobs_beyond_log_limit_are_marked_not_fetched() -> None:
    evidence = build_failure_evidence(event(), repository=None, jobs=jobs(), job_logs={}, commit=None, errors=[])
    assert evidence.failed_jobs[0].log.available is False
    assert "limit" in evidence.failed_jobs[0].log.error


def test_commit_falls_back_to_webhook_head_commit() -> None:
    evidence = build_failure_evidence(event(), repository=None, jobs=jobs(), job_logs={}, commit=None, errors=[])
    assert evidence.commit is not None
    assert evidence.commit.message == "Upgrade dependencies"
    assert evidence.commit.author_name == "Dev One"
    assert evidence.commit.changed_files_available is False
    assert evidence.commit.changed_files == []


def test_startup_failure_without_jobs() -> None:
    payload = load_fixture("workflow_run_failed.json")
    payload["workflow_run"]["conclusion"] = "startup_failure"
    evidence = build_failure_evidence(
        WorkflowRunEvent.model_validate(payload), repository=None, jobs=[], job_logs={}, commit=None, errors=[]
    )
    assert evidence.status == "STARTUP_FAILURE"
    assert evidence.failed_jobs == []
    assert evidence.failed_stage is None


def test_failed_stage_falls_back_to_job_name_without_failed_step() -> None:
    timed_out = WorkflowJob(id=9, run_id=RUN_ID, name="e2e", conclusion="timed_out")
    evidence = build_failure_evidence(event(), repository=None, jobs=[timed_out], job_logs={}, commit=None, errors=[])
    assert evidence.failed_stage == "e2e"


def test_log_summary_excludes_log_content() -> None:
    evidence = build_failure_evidence(
        event(), repository=None, jobs=jobs(), job_logs={2002: JobLog(LOG_TEXT, len(LOG_TEXT), False)},
        commit=None, errors=[],
    )
    summary = evidence.log_summary()
    assert "content" not in summary["failed_jobs"][0]["log"]
    assert "ERESOLVE" not in json.dumps(summary)
    assert evidence.failed_jobs[0].log.content == LOG_TEXT  # original untouched


def add_happy_path_routes(fake: FakeGitHub) -> None:
    fake.add("GET", REPO, httpx.Response(200, json=load_fixture("workflow_run_failed.json")["repository"]))
    fake.add("GET", f"{REPO}/actions/runs/{RUN_ID}/attempts/1/jobs", httpx.Response(200, json=load_fixture("workflow_jobs.json")))
    fake.add("GET", f"{REPO}/actions/jobs/2002/logs", httpx.Response(302, headers={"Location": SIGNED_LOG_URL}))
    fake.add("GET", SIGNED_LOG_URL, httpx.Response(200, text=LOG_TEXT))
    fake.add("GET", f"{REPO}/commits/{SHA}", httpx.Response(200, json=load_fixture("commit.json")))


async def test_collector_end_to_end(fake_github: FakeGitHub) -> None:
    add_happy_path_routes(fake_github)
    async with GitHubClient(API_URL, TokenAuth("t"), transport=fake_github.transport) as client:
        evidence = await collect_workflow_failure_evidence(client, event(), delivery_id="d-1")

    assert evidence.errors == []
    assert evidence.failed_stage == "build / Install dependencies"
    assert [job.name for job in evidence.failed_jobs] == ["build"]
    assert "ERESOLVE" in evidence.failed_jobs[0].log.content
    assert [f.filename for f in evidence.commit.changed_files] == ["package.json", "package-lock.json"]
    # Only the failed job's log is fetched.
    assert not any(r.url.path.endswith("/jobs/2001/logs") for r in fake_github.requests)
    assert {r.method for r in fake_github.requests} == {"GET"}


async def test_collector_records_source_errors_and_continues(fake_github: FakeGitHub) -> None:
    add_happy_path_routes(fake_github)
    fake_github.add("GET", f"{REPO}/actions/runs/{RUN_ID}/attempts/1/jobs", httpx.Response(500, json={"message": "Server Error"}))
    fake_github.add("GET", f"{REPO}/commits/{SHA}", httpx.Response(404, json={"message": "No commit found"}))

    async with GitHubClient(API_URL, TokenAuth("t"), transport=fake_github.transport) as client:
        evidence = await collect_workflow_failure_evidence(client, event())

    assert sorted(e.source for e in evidence.errors) == ["commit", "workflow_jobs"]
    assert evidence.failed_jobs == []
    assert evidence.repository.full_name == "acme/frontend"
    assert evidence.commit.message == "Upgrade dependencies"  # webhook fallback


async def test_collector_respects_failed_job_log_limit(fake_github: FakeGitHub) -> None:
    many = {
        "total_count": 3,
        "jobs": [{"id": i, "run_id": RUN_ID, "name": f"job-{i}", "conclusion": "failure"} for i in (1, 2, 3)],
    }
    add_happy_path_routes(fake_github)
    fake_github.add("GET", f"{REPO}/actions/runs/{RUN_ID}/attempts/1/jobs", httpx.Response(200, json=many))
    for i in (1, 2, 3):
        fake_github.add("GET", f"{REPO}/actions/jobs/{i}/logs", httpx.Response(200, text=f"log {i}"))

    async with GitHubClient(API_URL, TokenAuth("t"), transport=fake_github.transport) as client:
        evidence = await collect_workflow_failure_evidence(client, event(), max_job_logs=2)

    assert [job.log.available for job in evidence.failed_jobs] == [True, True, False]


RAW_JOB_LOG = (
    '2026-09-21T09:24:42.2Z ##[group]Run echo "npm ERR! code ERESOLVE"\n'
    '2026-09-21T09:24:42.2Z \x1b[36;1mecho "npm ERR! code ERESOLVE"\x1b[0m\n'
    "2026-09-21T09:24:42.2Z ##[endgroup]\n"
    "2026-09-21T09:24:42.2Z npm ERR! code ERESOLVE\n"
    "2026-09-21T09:24:42.2Z npm ERR! Conflicting peer dependency: react@18.3.1\n"
    "2026-09-21T09:24:42.2Z ##[error]Process completed with exit code 1.\n"
    "2026-09-21T09:24:42.3Z Post job cleanup.\n"
    "2026-09-21T09:24:42.4Z Cleaning up orphan processes\n"
    "2026-09-21T09:24:42.4Z ##[warning]Node.js 20 is deprecated\n"
)


def test_evidence_strips_ansi_and_extracts_the_failing_section() -> None:
    evidence = build_failure_evidence(
        event(), repository=None, jobs=jobs(),
        job_logs={2002: JobLog(RAW_JOB_LOG, len(RAW_JOB_LOG), False)}, commit=None, errors=[],
    )
    log = evidence.failed_jobs[0].log

    assert "\x1b[" not in log.content  # colour codes removed
    assert log.error_excerpt is not None
    assert "npm ERR! Conflicting peer dependency: react@18.3.1" in log.error_excerpt
    assert "##[error]Process completed with exit code 1." in log.error_excerpt
    assert "Node.js 20 is deprecated" not in log.error_excerpt  # cleanup noise excluded


def test_log_summary_keeps_the_error_excerpt_but_not_the_full_log() -> None:
    evidence = build_failure_evidence(
        event(), repository=None, jobs=jobs(),
        job_logs={2002: JobLog(RAW_JOB_LOG, len(RAW_JOB_LOG), False)}, commit=None, errors=[],
    )
    summary = evidence.log_summary()
    log = summary["failed_jobs"][0]["log"]

    assert "content" not in log
    assert "npm ERR! code ERESOLVE" in log["error_excerpt"]


async def test_collect_evidence_for_run_by_id(fake_github: FakeGitHub) -> None:
    from app.evidence.collector import collect_evidence_for_run

    add_happy_path_routes(fake_github)
    fake_github.add(
        "GET", f"{REPO}/actions/runs/{RUN_ID}",
        httpx.Response(200, json=load_fixture("workflow_run_failed.json")["workflow_run"]),
    )
    async with GitHubClient(API_URL, TokenAuth("t"), transport=fake_github.transport) as client:
        evidence = await collect_evidence_for_run(client, "acme", "frontend", RUN_ID)

    assert evidence.errors == []
    assert evidence.delivery_id is None
    assert evidence.workflow.name == "CI"
    assert evidence.failed_stage == "build / Install dependencies"
