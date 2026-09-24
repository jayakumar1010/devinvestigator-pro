import json

import pytest

from app import cli
from app.agent.investigator import AgentRun
from app.analysis.analyzer import analyze_failure
from app.llm.base import LLMMessage
from app.llm.base import LLMError
from tests.test_analysis import GOOD_ANALYSIS, FakeProvider, make_evidence

ARGS = ["acme/frontend", "9876543210"]


@pytest.fixture
def fake_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    async def collect(settings, owner, repo, run_id):
        assert (owner, repo, run_id) == ("acme", "frontend", 9876543210)
        return make_evidence()

    monkeypatch.setattr(cli, "_collect", collect)


def test_preview_prints_exact_prompt_without_calling_llm(fake_collect, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "build_llm_provider", lambda settings: pytest.fail("preview must not call the LLM"))

    assert cli.main(["preview", *ARGS]) == 0
    out = capsys.readouterr().out
    assert "===== SYSTEM MESSAGE" in out
    assert "===== USER MESSAGE" in out
    assert "npm ERR! Conflicting peer dependency: react@18.3.1" in out


def test_preview_can_print_schema(fake_collect, capsys) -> None:
    assert cli.main(["preview", *ARGS, "--schema"]) == 0
    assert '"dependency_failure"' in capsys.readouterr().out


def test_analyze_prints_structured_json(fake_collect, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "build_llm_provider", lambda settings: FakeProvider(GOOD_ANALYSIS))

    assert cli.main(["analyze", *ARGS]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["analysis"]["category"] == "dependency_failure"
    assert result["failed_stage"] == "build / Install dependencies"


def test_analyze_show_prompt_goes_to_stderr_keeping_stdout_json(fake_collect, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "build_llm_provider", lambda settings: FakeProvider(GOOD_ANALYSIS))

    assert cli.main(["analyze", *ARGS, "--show-prompt"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "===== USER MESSAGE" in captured.err


def test_analyze_model_override(fake_collect, monkeypatch, capsys) -> None:
    seen = {}

    def build(settings):
        seen["model"] = settings.llm_model
        return FakeProvider(GOOD_ANALYSIS)

    monkeypatch.setattr(cli, "build_llm_provider", build)
    assert cli.main(["analyze", *ARGS, "--model", "gemma3:1b"]) == 0
    assert seen["model"] == "gemma3:1b"


def test_llm_failure_returns_exit_code_3(fake_collect, monkeypatch, capsys) -> None:
    def build(settings):
        raise LLMError("Unknown LLM provider 'nope'")

    monkeypatch.setattr(cli, "build_llm_provider", build)
    assert cli.main(["analyze", *ARGS]) == 3
    assert "analysis failed" in capsys.readouterr().err


def test_non_failed_run_warns(monkeypatch, capsys) -> None:
    async def collect(settings, owner, repo, run_id):
        evidence = make_evidence()
        return evidence.model_copy(update={"run": evidence.run.model_copy(update={"conclusion": "success"})})

    monkeypatch.setattr(cli, "_collect", collect)
    assert cli.main(["preview", *ARGS]) == 0
    assert "warning" in capsys.readouterr().err


def test_invalid_repo_argument() -> None:
    with pytest.raises(SystemExit):
        cli.main(["preview", "no-slash", "1"])


def test_analyze_warns_on_invalid_evidence(fake_collect, monkeypatch, capsys) -> None:
    invented = {**GOOD_ANALYSIS, "evidence": ["This line was never in the log"]}
    monkeypatch.setattr(cli, "build_llm_provider", lambda settings: FakeProvider(invented))

    assert cli.main(["analyze", *ARGS]) == 0
    captured = capsys.readouterr()
    assert "contains_invalid_evidence" in captured.err
    assert json.loads(captured.out)["evidence_validation"]["invalid"] == 1


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}

    async def run_agent(settings, owner, repo, run_id, max_tool_calls, on_step):
        seen.update(owner=owner, repo=repo, run_id=run_id, max_tool_calls=max_tool_calls, model=settings.llm_model)
        result = await analyze_failure(make_evidence(), FakeProvider(GOOD_ANALYSIS))
        return AgentRun(
            result.model_copy(update={"mode": "agent"}),
            [LLMMessage("system", "SYSTEM TEXT"), LLMMessage("user", "EVIDENCE TEXT")],
        )

    monkeypatch.setattr(cli, "_run_agent", run_agent)
    return seen


def test_investigate_prints_the_agent_result(fake_agent, capsys) -> None:
    assert cli.main(["investigate", *ARGS]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "agent"
    assert (fake_agent["owner"], fake_agent["repo"], fake_agent["run_id"]) == ("acme", "frontend", 9876543210)
    assert fake_agent["max_tool_calls"] == 6


def test_investigate_options_and_transcript(fake_agent, capsys) -> None:
    assert cli.main(["investigate", *ARGS, "--max-tool-calls", "2", "--model", "gemma3:1b", "--show-transcript"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # stdout stays pure JSON
    assert "EVIDENCE TEXT" in captured.err
    assert (fake_agent["max_tool_calls"], fake_agent["model"]) == (2, "gemma3:1b")


def test_investigate_llm_failure_returns_exit_code_3(monkeypatch, capsys) -> None:
    async def run_agent(*args, **kwargs):
        raise LLMError("model unavailable")

    monkeypatch.setattr(cli, "_run_agent", run_agent)
    assert cli.main(["investigate", *ARGS]) == 3
    assert "investigation failed" in capsys.readouterr().err


def seed(database_url: str, count: int = 1, fail_first: bool = False) -> list[int]:
    import asyncio

    from app.database import repository
    from app.database.session import create_engine, create_sessionmaker, init_db
    from app.integrations.github.models import WorkflowRun
    from tests.conftest import load_fixture

    async def go() -> list[int]:
        engine = create_engine(database_url)
        await init_db(engine)
        ids = []
        async with create_sessionmaker(engine)() as session:
            for n in range(count):
                run = WorkflowRun.model_validate({**load_fixture("workflow_run_failed.json")["workflow_run"], "id": 1000 + n})
                fields = repository.run_fields("acme/frontend", run, installation_id=None, delivery_id=None, trigger="manual")
                ids.append((await repository.create_investigation(session, fields))[0].id)
            if fail_first:
                await repository.claim_next(session)
                await repository.fail(session, ids[0], "Ollama returned 404: model not found")
        await engine.dispose()
        return ids

    return asyncio.run(go())


@pytest.fixture
def cli_db(monkeypatch, database_url) -> str:
    from app.core.config import Settings

    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None, database_url=database_url))
    return database_url


def test_list_shows_stored_investigations(cli_db, capsys) -> None:
    ids = seed(cli_db, count=2)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "STATUS" in out and "acme/frontend / CI #182" in out
    assert out.count("queued") == 2 and str(ids[1]) in out


def test_show_prints_one_investigation(cli_db, capsys) -> None:
    [investigation_id] = seed(cli_db)
    assert cli.main(["show", str(investigation_id)]) == 0
    details = json.loads(capsys.readouterr().out)
    assert (details["id"], details["status"], details["repository"]) == (investigation_id, "queued", "acme/frontend")
    assert "evidence" not in details


def test_show_unknown_investigation(cli_db, capsys) -> None:
    seed(cli_db)
    assert cli.main(["show", "999"]) == 1
    assert "not found" in capsys.readouterr().err


def test_retry_requeues_a_failed_investigation(cli_db, capsys) -> None:
    [investigation_id] = seed(cli_db, fail_first=True)
    assert cli.main(["retry", str(investigation_id)]) == 0
    assert "queued again" in capsys.readouterr().out
    assert cli.main(["retry", str(investigation_id)]) == 1  # queued, not retryable
    assert "only failed, completed or collected" in capsys.readouterr().err


def test_queue_stores_a_manual_investigation_once(cli_db, monkeypatch, capsys) -> None:
    from app.integrations.github.models import WorkflowRun
    from tests.conftest import load_fixture

    async def fetch_run(settings, owner, repo, run_id):
        return WorkflowRun.model_validate(load_fixture("workflow_run_failed.json")["workflow_run"])

    monkeypatch.setattr(cli, "_fetch_run", fetch_run)
    assert cli.main(["queue", *ARGS]) == 0
    assert "Queued investigation 1" in capsys.readouterr().out
    assert cli.main(["queue", *ARGS]) == 0
    assert "already has investigation 1" in capsys.readouterr().out


def test_comment_preview_renders_a_stored_result(cli_db, capsys) -> None:
    import asyncio

    from app.analysis.analyzer import analyze_failure
    from app.database import repository
    from app.database.session import create_engine, create_sessionmaker, init_db

    [investigation_id] = seed(cli_db)

    async def complete() -> None:
        engine = create_engine(cli_db)
        await init_db(engine)
        async with create_sessionmaker(engine)() as session:
            await repository.claim_next(session)
            result = await analyze_failure(make_evidence(), FakeProvider(GOOD_ANALYSIS))
            await repository.complete(session, investigation_id, result)
        await engine.dispose()

    asyncio.run(complete())
    assert cli.main(["comment-preview", str(investigation_id)]) == 0
    out = capsys.readouterr().out
    assert "DevInvestigator" in out and GOOD_ANALYSIS["root_cause"] in out
    assert "```text" in out


def test_comment_preview_without_a_result(cli_db, capsys) -> None:
    [investigation_id] = seed(cli_db)
    assert cli.main(["comment-preview", str(investigation_id)]) == 1
    assert "no result yet" in capsys.readouterr().err
