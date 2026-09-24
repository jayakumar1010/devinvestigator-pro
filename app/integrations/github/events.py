"""Failure detection rules for GitHub Actions runs, jobs and steps."""

from app.integrations.github.models import WorkflowJob, WorkflowRunEvent, WorkflowStep

FAILED_RUN_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
FAILED_JOB_CONCLUSIONS = frozenset({"failure", "timed_out"})


def is_failed_workflow_run(event: WorkflowRunEvent) -> bool:
    return event.action == "completed" and event.workflow_run.conclusion in FAILED_RUN_CONCLUSIONS


def failed_jobs(jobs: list[WorkflowJob]) -> list[WorkflowJob]:
    return [job for job in jobs if job.conclusion in FAILED_JOB_CONCLUSIONS]


def failed_steps(job: WorkflowJob) -> list[WorkflowStep]:
    return [step for step in job.steps if step.conclusion in FAILED_JOB_CONCLUSIONS]
