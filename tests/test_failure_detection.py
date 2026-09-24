import pytest

from app.integrations.github.events import failed_jobs, failed_steps, is_failed_workflow_run
from app.integrations.github.models import WorkflowJob, WorkflowRunEvent
from tests.conftest import load_fixture


def make_event(action: str = "completed", conclusion: str | None = "failure") -> WorkflowRunEvent:
    payload = load_fixture("workflow_run_failed.json")
    payload["action"] = action
    payload["workflow_run"]["conclusion"] = conclusion
    return WorkflowRunEvent.model_validate(payload)


def test_webhook_payload_parses_and_ignores_unknown_fields() -> None:
    event = make_event()
    assert event.repository.full_name == "acme/frontend"
    assert event.repository.owner.login == "acme"
    assert event.workflow_run.run_number == 182
    assert event.workflow_run.head_commit is not None
    assert event.workflow_run.head_commit.message == "Upgrade dependencies"
    assert not hasattr(event.workflow_run, "some_future_field")


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
def test_failed_conclusions_are_detected(conclusion: str) -> None:
    assert is_failed_workflow_run(make_event(conclusion=conclusion))


@pytest.mark.parametrize("conclusion", ["success", "cancelled", "skipped", "neutral", "action_required", None])
def test_non_failed_conclusions_are_not_detected(conclusion: str | None) -> None:
    assert not is_failed_workflow_run(make_event(conclusion=conclusion))


@pytest.mark.parametrize("action", ["requested", "in_progress"])
def test_only_completed_runs_count(action: str) -> None:
    assert not is_failed_workflow_run(make_event(action=action, conclusion="failure"))


def test_failed_jobs_and_steps() -> None:
    jobs = [WorkflowJob.model_validate(j) for j in load_fixture("workflow_jobs.json")["jobs"]]
    jobs.append(WorkflowJob(id=3, run_id=1, name="deploy", conclusion="timed_out"))
    jobs.append(WorkflowJob(id=4, run_id=1, name="lint", conclusion="cancelled"))

    failed = failed_jobs(jobs)
    assert [job.name for job in failed] == ["build", "deploy"]
    assert [step.name for step in failed_steps(failed[0])] == ["Install dependencies"]
    assert failed_steps(failed[1]) == []
