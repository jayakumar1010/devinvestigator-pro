from app.analysis.analyzer import analyze_failure
from app.database import repository
from app.integrations.github.models import WorkflowRun
from tests.conftest import load_fixture
from tests.test_analysis import GOOD_ANALYSIS, FakeProvider, make_evidence


def fields(run_id: int = 9876543210, attempt: int = 1) -> dict:
    run = WorkflowRun.model_validate({**load_fixture("workflow_run_failed.json")["workflow_run"], "id": run_id, "run_attempt": attempt})
    return repository.run_fields("acme/frontend", run, installation_id=555, delivery_id="d-1", trigger="webhook")


async def test_one_investigation_per_run_attempt(sessionmaker) -> None:
    async with sessionmaker() as session:
        first, created = await repository.create_investigation(session, fields())
        again, created_again = await repository.create_investigation(session, fields())
        rerun, created_rerun = await repository.create_investigation(session, fields(attempt=2))

    assert (created, created_again, created_rerun) == (True, False, True)
    assert again.id == first.id != rerun.id
    assert (first.status, first.workflow, first.run_number, first.installation_id) == ("queued", "CI", 182, 555)


async def test_claim_next_takes_the_oldest_queued(sessionmaker) -> None:
    async with sessionmaker() as session:
        a, _ = await repository.create_investigation(session, fields(1))
        b, _ = await repository.create_investigation(session, fields(2))
        assert await repository.claim_next(session) == a.id
        assert await repository.claim_next(session) == b.id
        assert await repository.claim_next(session) is None

    async with sessionmaker() as session:
        claimed = await repository.get(session, a.id)
    assert claimed.status == "running"
    assert claimed.started_at is not None


async def test_reset_interrupted_queues_running_investigations_again(sessionmaker) -> None:
    async with sessionmaker() as session:
        inv, _ = await repository.create_investigation(session, fields())
        await repository.claim_next(session)
        assert await repository.reset_interrupted(session) == 1
        assert await repository.claim_next(session) == inv.id


async def test_complete_stores_the_result(sessionmaker) -> None:
    result = await analyze_failure(make_evidence(), FakeProvider(GOOD_ANALYSIS))
    async with sessionmaker() as session:
        inv, _ = await repository.create_investigation(session, fields())
        await repository.claim_next(session)
        await repository.save_evidence(session, inv.id, make_evidence())
        await repository.complete(session, inv.id, result)

    async with sessionmaker() as session:
        stored = await repository.get(session, inv.id)
    assert stored.status == "completed"
    assert (stored.category, stored.confidence, stored.confidence_basis) == ("dependency_failure", 0.9, "direct")
    assert (stored.mode, stored.model, stored.evidence_status) == ("single_pass", "fake-model", "verified")
    assert stored.root_cause == GOOD_ANALYSIS["root_cause"]
    assert stored.result["analysis"]["recommendation"] == GOOD_ANALYSIS["recommendation"]
    assert stored.evidence["failed_stage"] == "build / Install dependencies"
    assert stored.finished_at is not None and stored.duration_seconds >= 0


async def test_fail_then_retry(sessionmaker) -> None:
    async with sessionmaker() as session:
        inv, _ = await repository.create_investigation(session, fields())
        await repository.claim_next(session)
        await repository.fail(session, inv.id, "x" * 3000)

    async with sessionmaker() as session:
        failed = await repository.get(session, inv.id)
        assert (failed.status, len(failed.error)) == ("failed", 2000)
        assert await repository.requeue(session, inv.id) is True
        assert await repository.requeue(session, inv.id) is False  # already queued
        assert await repository.requeue(session, 999) is False

    async with sessionmaker() as session:
        queued = await repository.get(session, inv.id)
    assert (queued.status, queued.error, queued.started_at) == ("queued", None, None)


async def test_mark_collected(sessionmaker) -> None:
    async with sessionmaker() as session:
        inv, _ = await repository.create_investigation(session, fields())
        await repository.claim_next(session)
        await repository.mark_collected(session, inv.id)
    async with sessionmaker() as session:
        assert (await repository.get(session, inv.id)).status == "collected"


async def test_list_recent_is_newest_first(sessionmaker) -> None:
    async with sessionmaker() as session:
        ids = [(await repository.create_investigation(session, fields(n)))[0].id for n in (1, 2, 3)]
        recent = await repository.list_recent(session, limit=2)
    assert [inv.id for inv in recent] == [ids[2], ids[1]]
