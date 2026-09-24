import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.database import repository
from app.integrations.github.auth import TokenAuth
from app.integrations.github.client import GitHubClient
from app.integrations.github.models import WorkflowRunEvent
from app.llm.base import LLMError
from app.orchestrator import InvestigationOrchestrator
from tests.conftest import API_URL, FakeGitHub, load_fixture
from tests.test_agent import ScriptedProvider, step
from tests.test_analysis import GOOD_ANALYSIS, FakeProvider
from tests.test_evidence import REPO, RUN_ID, add_happy_path_routes


def event() -> WorkflowRunEvent:
    return WorkflowRunEvent.model_validate(load_fixture("workflow_run_failed.json"))


def settings(database_url: str, **overrides) -> Settings:
    values = {"analysis_mode": "single_pass", "worker_poll_seconds": 0.05, **overrides}
    return Settings(_env_file=None, database_url=database_url, **values)


@pytest.fixture
def github(fake_github: FakeGitHub) -> FakeGitHub:
    add_happy_path_routes(fake_github)
    fake_github.add("GET", f"{REPO}/actions/runs/{RUN_ID}", httpx.Response(200, json=load_fixture("workflow_run_failed.json")["workflow_run"]))
    return fake_github


def make_orchestrator(
    config: Settings, sessionmaker, github: FakeGitHub, provider, notifier=None
) -> InvestigationOrchestrator:
    async def no_notifier(*args, **kwargs):
        raise AssertionError("notifications are off in this test")

    return InvestigationOrchestrator(
        config,
        sessionmaker,
        client_factory=lambda s, installation_id=None: GitHubClient(API_URL, TokenAuth("t"), transport=github.transport),
        provider_factory=lambda s: provider,
        notifier=notifier or no_notifier,
    )


async def submit_and_process(orchestrator: InvestigationOrchestrator, sessionmaker) -> int:
    investigation_id, _ = await orchestrator.submit(event(), "delivery-1")
    async with sessionmaker() as session:
        assert await repository.claim_next(session) == investigation_id
    await orchestrator.process(investigation_id)
    return investigation_id


async def stored(sessionmaker, investigation_id: int):
    async with sessionmaker() as session:
        return await repository.get(session, investigation_id)


async def wait_until_finished(sessionmaker, investigation_id: int):
    for _ in range(200):
        investigation = await stored(sessionmaker, investigation_id)
        if investigation.status in ("completed", "failed", "collected"):
            return investigation
        await asyncio.sleep(0.05)
    raise AssertionError(f"investigation {investigation_id} did not finish")


class BrokenProvider:
    name, model = "broken", "broken-model"

    async def complete_json(self, messages, schema):
        raise LLMError("Ollama returned 404: model 'x' not found")


class SlowProvider:
    name, model = "slow", "slow-model"

    async def complete_json(self, messages, schema):
        await asyncio.sleep(30)


async def test_submit_queues_once_per_run_attempt(database_url, sessionmaker, github) -> None:
    orchestrator = make_orchestrator(settings(database_url), sessionmaker, github, FakeProvider(GOOD_ANALYSIS))
    first = await orchestrator.submit(event(), "delivery-1")
    again = await orchestrator.submit(event(), "delivery-2")
    assert (first[1], again[1]) == (True, False)
    assert first[0] == again[0]


async def test_single_pass_investigation_is_stored(database_url, sessionmaker, github) -> None:
    orchestrator = make_orchestrator(settings(database_url), sessionmaker, github, FakeProvider(GOOD_ANALYSIS))
    investigation = await stored(sessionmaker, await submit_and_process(orchestrator, sessionmaker))

    assert investigation.status == "completed"
    assert (investigation.category, investigation.mode, investigation.error) == ("dependency_failure", "single_pass", None)
    assert (investigation.trigger, investigation.delivery_id, investigation.installation_id) == ("webhook", "delivery-1", 555)
    assert investigation.evidence["failed_stage"] == "build / Install dependencies"
    assert investigation.result["repository"] == "acme/frontend"


async def test_agent_mode_uses_the_tools(database_url, sessionmaker, github) -> None:
    github.add("GET", f"{REPO}/contents/package.json", httpx.Response(200, text='{"dependencies": {"react": "17.0.2"}}'))
    provider = ScriptedProvider([step("get_file", "package.json"), step("finish"), GOOD_ANALYSIS])
    orchestrator = make_orchestrator(settings(database_url, analysis_mode="agent"), sessionmaker, github, provider)
    investigation = await stored(sessionmaker, await submit_and_process(orchestrator, sessionmaker))

    assert (investigation.status, investigation.mode) == ("completed", "agent")
    tool_call = investigation.result["tool_calls"][0]
    assert (tool_call["tool"], tool_call["arguments"], tool_call["ok"]) == ("get_file", {"path": "package.json"}, True)


async def test_llm_errors_mark_the_investigation_failed(database_url, sessionmaker, github) -> None:
    orchestrator = make_orchestrator(settings(database_url), sessionmaker, github, BrokenProvider())
    investigation = await stored(sessionmaker, await submit_and_process(orchestrator, sessionmaker))

    assert investigation.status == "failed"
    assert "model 'x' not found" in investigation.error
    assert investigation.evidence is not None  # evidence is kept for a retry
    assert investigation.duration_seconds is not None


async def test_github_errors_mark_the_investigation_failed(database_url, sessionmaker, fake_github) -> None:
    orchestrator = make_orchestrator(settings(database_url), sessionmaker, fake_github, FakeProvider(GOOD_ANALYSIS))
    investigation = await stored(sessionmaker, await submit_and_process(orchestrator, sessionmaker))

    assert investigation.status == "failed"
    assert "404" in investigation.error
    assert investigation.evidence is None


async def test_timeout_marks_the_investigation_failed(database_url, sessionmaker, github) -> None:
    config = settings(database_url, investigation_timeout_seconds=0.3)
    orchestrator = make_orchestrator(config, sessionmaker, github, SlowProvider())
    investigation = await stored(sessionmaker, await submit_and_process(orchestrator, sessionmaker))

    assert investigation.status == "failed"
    assert "timed out" in investigation.error


async def test_auto_analyze_off_collects_evidence_only(database_url, sessionmaker, github) -> None:
    provider = FakeProvider(GOOD_ANALYSIS)
    orchestrator = make_orchestrator(settings(database_url, auto_analyze=False), sessionmaker, github, provider)
    investigation = await stored(sessionmaker, await submit_and_process(orchestrator, sessionmaker))

    assert investigation.status == "collected"
    assert investigation.evidence is not None and investigation.result is None
    assert provider.calls == []


async def test_worker_investigates_submitted_runs(database_url, sessionmaker, github) -> None:
    orchestrator = make_orchestrator(settings(database_url), sessionmaker, github, FakeProvider(GOOD_ANALYSIS))
    await orchestrator.start()
    try:
        investigation_id, _ = await orchestrator.submit(event(), "delivery-1")
        investigation = await wait_until_finished(sessionmaker, investigation_id)
    finally:
        await orchestrator.stop()
    assert investigation.status == "completed"


async def test_restart_resumes_interrupted_investigations(database_url, sessionmaker, github) -> None:
    fields = repository.run_fields("acme/frontend", event().workflow_run, installation_id=None, delivery_id=None, trigger="webhook")
    async with sessionmaker() as session:
        investigation, _ = await repository.create_investigation(session, fields)
        await repository.claim_next(session)  # "running" when the old process died

    orchestrator = make_orchestrator(settings(database_url), sessionmaker, github, FakeProvider(GOOD_ANALYSIS))
    await orchestrator.start()
    try:
        finished = await wait_until_finished(sessionmaker, investigation.id)
    finally:
        await orchestrator.stop()
    assert finished.status == "completed"


async def test_no_comment_is_posted_by_default(database_url, sessionmaker, github) -> None:
    # make_orchestrator's default notifier fails if called; the default settings must not call it.
    orchestrator = make_orchestrator(settings(database_url), sessionmaker, github, FakeProvider(GOOD_ANALYSIS))
    investigation = await stored(sessionmaker, await submit_and_process(orchestrator, sessionmaker))

    assert investigation.status == "completed"
    assert investigation.notification_url is None and investigation.notification_error is None


async def test_the_comment_url_is_stored(database_url, sessionmaker, github) -> None:
    posted = {}

    async def notifier(client, config, investigation_id, evidence, result, **kwargs):
        posted.update(investigation_id=investigation_id, category=result.analysis.category, **kwargs)
        return "https://github.com/acme/frontend/commit/abc#comment-1"

    config = settings(database_url, notify_github_comments=True, github_comment_token="write-token")
    orchestrator = make_orchestrator(config, sessionmaker, github, FakeProvider(GOOD_ANALYSIS), notifier=notifier)
    investigation = await stored(sessionmaker, await submit_and_process(orchestrator, sessionmaker))

    assert investigation.notification_url == "https://github.com/acme/frontend/commit/abc#comment-1"
    assert posted["category"] == "dependency_failure"


async def test_a_failed_comment_does_not_lose_the_investigation(database_url, sessionmaker, github) -> None:
    async def notifier(*args, **kwargs):
        raise RuntimeError("403 Resource not accessible by personal access token")

    config = settings(database_url, notify_github_comments=True, github_comment_token="write-token")
    orchestrator = make_orchestrator(config, sessionmaker, github, FakeProvider(GOOD_ANALYSIS), notifier=notifier)
    investigation = await stored(sessionmaker, await submit_and_process(orchestrator, sessionmaker))

    assert investigation.status == "completed"  # the result is kept
    assert investigation.category == "dependency_failure"
    assert "403" in investigation.notification_error
    assert investigation.notification_url is None


async def test_a_repeated_failure_reuses_the_previous_answer(database_url, sessionmaker, github) -> None:
    """The second time the same failure appears, the model is not asked again."""
    provider = FakeProvider(GOOD_ANALYSIS)
    config = settings(database_url)
    orchestrator = make_orchestrator(config, sessionmaker, github, provider)

    first_id = await submit_and_process(orchestrator, sessionmaker)
    first = await stored(sessionmaker, first_id)
    calls_after_first = len(provider.calls)

    # The same run fails again as a new attempt: same signature, new investigation.
    second_event = event()
    second_event.workflow_run.run_attempt = 2
    second_id, created = await orchestrator.submit(second_event, "delivery-2")
    assert created
    async with sessionmaker() as session:
        assert await repository.claim_next(session) == second_id
    await orchestrator.process(second_id)
    second = await stored(sessionmaker, second_id)

    assert len(provider.calls) == calls_after_first  # no new model call
    assert second.status == "completed"
    assert second.mode == "reused"
    assert second.reused_from_id == first.id
    assert second.root_cause == first.root_cause
    assert second.result["mode"] == "reused"
    assert second.evidence is not None  # evidence is still collected and stored


async def test_reuse_can_be_switched_off(database_url, sessionmaker, github) -> None:
    provider = FakeProvider(GOOD_ANALYSIS)
    config = settings(database_url, reuse_previous_results=False)
    orchestrator = make_orchestrator(config, sessionmaker, github, provider)

    await submit_and_process(orchestrator, sessionmaker)
    calls_after_first = len(provider.calls)

    second_event = event()
    second_event.workflow_run.run_attempt = 2
    second_id, _ = await orchestrator.submit(second_event, "delivery-2")
    async with sessionmaker() as session:
        await repository.claim_next(session)
    await orchestrator.process(second_id)

    assert len(provider.calls) == calls_after_first + 1  # analysed again
    assert (await stored(sessionmaker, second_id)).mode == "single_pass"


async def test_the_comment_reports_how_often_the_failure_happened(database_url, sessionmaker, github) -> None:
    seen = {}

    async def notifier(client, config, investigation_id, evidence, result, **kwargs):
        seen[investigation_id] = kwargs
        return "https://github.com/c/1"

    config = settings(database_url, notify_github_comments=True, github_comment_token="write-token")
    orchestrator = make_orchestrator(config, sessionmaker, github, FakeProvider(GOOD_ANALYSIS), notifier=notifier)

    first_id = await submit_and_process(orchestrator, sessionmaker)
    second_event = event()
    second_event.workflow_run.run_attempt = 2
    second_id, _ = await orchestrator.submit(second_event, "delivery-2")
    async with sessionmaker() as session:
        await repository.claim_next(session)
    await orchestrator.process(second_id)

    assert seen[first_id] == {"repeat_count": 1, "reused_from": None}
    assert seen[second_id] == {"repeat_count": 2, "reused_from": first_id}
