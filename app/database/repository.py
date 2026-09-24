"""Database access for investigations. The agent, tools and analysis never touch the database."""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.analysis.models import AnalysisResult
from app.database.models import Investigation
from app.evidence.models import FailureEvidence
from app.evidence.signature import failure_signature
from app.integrations.github.models import WorkflowRun

REQUEUEABLE = ("failed", "completed", "collected")
_RESULT_FIELDS = (
    "result", "category", "summary", "root_cause", "recommendation", "confidence",
    "confidence_basis", "evidence_status", "mode", "model", "notification_url", "notification_error",
    "reused_from_id",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes; everything is stored in UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def run_fields(
    repository: str, run: WorkflowRun, *, installation_id: int | None, delivery_id: str | None, trigger: str
) -> dict[str, Any]:
    return {
        "provider": "github_actions",
        "repository": repository,
        "run_id": run.id,
        "run_attempt": run.run_attempt,
        "run_number": run.run_number,
        "workflow": run.name,
        "run_url": run.html_url,
        "head_sha": run.head_sha,
        "head_branch": run.head_branch,
        "installation_id": installation_id,
        "delivery_id": delivery_id,
        "trigger": trigger,
    }


async def _find_run(session: AsyncSession, provider: str, repository: str, run_id: int, run_attempt: int) -> Investigation | None:
    return await session.scalar(
        select(Investigation).where(
            Investigation.provider == provider,
            Investigation.repository == repository,
            Investigation.run_id == run_id,
            Investigation.run_attempt == run_attempt,
        )
    )


async def create_investigation(session: AsyncSession, fields: dict[str, Any]) -> tuple[Investigation, bool]:
    """Queue an investigation, or return the existing one for the same run attempt (created=False)."""
    key = (fields["provider"], fields["repository"], fields["run_id"], fields["run_attempt"])
    existing = await _find_run(session, *key)
    if existing is not None:
        return existing, False
    investigation = Investigation(**fields, status="queued", created_at=utcnow())
    session.add(investigation)
    try:
        await session.commit()
    except IntegrityError:  # the same delivery arrived twice at the same moment
        await session.rollback()
        existing = await _find_run(session, *key)
        if existing is None:
            raise
        return existing, False
    return investigation, True


async def get(session: AsyncSession, investigation_id: int) -> Investigation | None:
    # populate_existing: claim_next and reset_interrupted write with UPDATE statements,
    # so an object cached in this session may be stale.
    return await session.get(Investigation, investigation_id, populate_existing=True)


async def list_recent(session: AsyncSession, limit: int = 20, status: str | None = None) -> list[Investigation]:
    """Newest first, without the large evidence/result JSON (not needed for a listing)."""
    query = (
        select(Investigation)
        .options(defer(Investigation.evidence), defer(Investigation.result))
        .order_by(Investigation.id.desc())
        .limit(limit)
    )
    if status is not None:
        query = query.where(Investigation.status == status)
    return list(await session.scalars(query))


async def count_by_status(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Investigation.status, func.count()).group_by(Investigation.status))
    return {status: count for status, count in rows.all()}


async def claim_next(session: AsyncSession) -> int | None:
    """Mark the oldest queued investigation as running and return its id."""
    next_id = await session.scalar(
        select(Investigation.id).where(Investigation.status == "queued").order_by(Investigation.id).limit(1)
    )
    if next_id is None:
        return None
    claimed = await session.execute(
        update(Investigation)
        .where(Investigation.id == next_id, Investigation.status == "queued")
        .values(status="running", started_at=utcnow(), error=None)
    )
    await session.commit()
    return next_id if claimed.rowcount == 1 else None


async def reset_interrupted(session: AsyncSession) -> int:
    """Queue again investigations left running by a restart."""
    reset = await session.execute(
        update(Investigation).where(Investigation.status == "running").values(status="queued", started_at=None)
    )
    await session.commit()
    return reset.rowcount


async def _require(session: AsyncSession, investigation_id: int) -> Investigation:
    investigation = await get(session, investigation_id)
    if investigation is None:
        raise LookupError(f"investigation {investigation_id} not found")
    return investigation


def _finish(investigation: Investigation) -> None:
    now = utcnow()
    investigation.finished_at = now
    if investigation.started_at is not None:
        investigation.duration_seconds = round((now - _as_utc(investigation.started_at)).total_seconds(), 2)


async def find_reusable(
    session: AsyncSession, signature: str, repository: str, *, within_days: int, exclude_id: int
) -> Investigation | None:
    """The most recent completed investigation of the same failure in the same repository."""
    since = utcnow() - timedelta(days=within_days)
    return await session.scalar(
        select(Investigation)
        .where(
            Investigation.signature == signature,
            Investigation.repository == repository,
            Investigation.status == "completed",
            Investigation.id != exclude_id,
            Investigation.created_at >= since,
        )
        .order_by(Investigation.id.desc())
        .limit(1)
    )


async def count_signature(session: AsyncSession, signature: str, repository: str, *, within_days: int = 30) -> int:
    """How many times this failure has been seen recently, including the current one."""
    since = utcnow() - timedelta(days=within_days)
    return await session.scalar(
        select(func.count())
        .select_from(Investigation)
        .where(
            Investigation.signature == signature,
            Investigation.repository == repository,
            Investigation.created_at >= since,
        )
    ) or 0


async def reuse_result(session: AsyncSession, investigation_id: int, source: Investigation) -> AnalysisResult:
    """Copy a previous answer onto this investigation instead of asking the model again."""
    investigation = await _require(session, investigation_id)
    result = AnalysisResult.model_validate(source.result)
    # No model was called: report that honestly instead of copying the original's cost.
    reused_llm = result.llm.model_copy(update={"calls": 0, "duration_seconds": 0.0,
                                               "prompt_tokens": None, "completion_tokens": None})
    result = result.model_copy(update={"mode": "reused", "analyzed_at": utcnow(), "llm": reused_llm})
    investigation.status = "completed"
    investigation.error = None
    investigation.mode = "reused"
    investigation.reused_from_id = source.id
    investigation.model = source.model
    investigation.category = source.category
    investigation.summary = source.summary
    investigation.root_cause = source.root_cause
    investigation.recommendation = source.recommendation
    investigation.confidence = source.confidence
    investigation.confidence_basis = source.confidence_basis
    investigation.evidence_status = source.evidence_status
    investigation.result = result.model_dump(mode="json")
    _finish(investigation)
    await session.commit()
    return result


async def save_evidence(session: AsyncSession, investigation_id: int, evidence: FailureEvidence) -> None:
    investigation = await _require(session, investigation_id)
    investigation.signature = failure_signature(evidence)
    investigation.evidence = evidence.model_dump(mode="json")
    investigation.failed_stage = evidence.failed_stage
    investigation.workflow = evidence.workflow.name
    investigation.run_number = evidence.run.run_number
    investigation.run_url = evidence.run.html_url
    investigation.head_sha = evidence.run.head_sha
    investigation.head_branch = evidence.run.head_branch
    await session.commit()


async def complete(session: AsyncSession, investigation_id: int, result: AnalysisResult) -> None:
    investigation = await _require(session, investigation_id)
    investigation.status = "completed"
    investigation.error = None
    investigation.mode = result.mode
    investigation.model = result.llm.model
    investigation.category = result.analysis.category
    investigation.summary = result.analysis.summary
    investigation.root_cause = result.analysis.root_cause
    investigation.recommendation = result.analysis.recommendation
    investigation.confidence = result.confidence
    investigation.confidence_basis = result.confidence_assessment.basis
    investigation.evidence_status = result.evidence_validation.status
    investigation.result = result.model_dump(mode="json")
    _finish(investigation)
    await session.commit()


async def mark_collected(session: AsyncSession, investigation_id: int) -> None:
    """Evidence stored, analysis skipped (AUTO_ANALYZE=false)."""
    investigation = await _require(session, investigation_id)
    investigation.status = "collected"
    _finish(investigation)
    await session.commit()


async def fail(session: AsyncSession, investigation_id: int, error: str) -> None:
    investigation = await _require(session, investigation_id)
    investigation.status = "failed"
    investigation.error = error[:2000]
    _finish(investigation)
    await session.commit()


async def save_notification(
    session: AsyncSession, investigation_id: int, *, url: str | None = None, error: str | None = None
) -> None:
    investigation = await _require(session, investigation_id)
    investigation.notification_url = url
    investigation.notification_error = error[:2000] if error else None
    await session.commit()


VERDICTS = ("correct", "wrong")


async def set_feedback(session: AsyncSession, investigation_id: int, verdict: str) -> bool:
    """Record whether a reader thought the answer was right."""
    if verdict not in VERDICTS:
        return False
    investigation = await session.get(Investigation, investigation_id)
    if investigation is None:
        return False
    investigation.feedback = verdict
    investigation.feedback_at = utcnow()
    await session.commit()
    return True


async def quality_stats(session: AsyncSession) -> dict[str, Any]:
    """Counts the quality page shows. One query per question, all cheap."""

    async def grouped(column) -> list[tuple[Any, int]]:
        rows = await session.execute(
            select(column, func.count()).where(column.is_not(None)).group_by(column).order_by(func.count().desc())
        )
        return [(value, count) for value, count in rows.all()]

    completed = select(Investigation).where(Investigation.status == "completed").subquery()
    total = await session.scalar(select(func.count()).select_from(Investigation)) or 0
    verified = await session.scalar(
        select(func.count()).select_from(Investigation).where(Investigation.evidence_status == "verified")
    ) or 0
    with_evidence = await session.scalar(
        select(func.count()).select_from(Investigation).where(Investigation.evidence_status.is_not(None))
    ) or 0
    reused = await session.scalar(
        select(func.count()).select_from(Investigation).where(Investigation.mode == "reused")
    ) or 0
    analysed_seconds = await session.scalar(
        select(func.avg(Investigation.duration_seconds)).where(Investigation.mode.in_(("agent", "single_pass")))
    )
    feedback = dict(await grouped(Investigation.feedback))

    return {
        "total": total,
        "by_status": dict(await grouped(Investigation.status)),
        "by_category": await grouped(Investigation.category),
        "by_model": await grouped(Investigation.model),
        "average_confidence": await session.scalar(select(func.avg(Investigation.confidence))),
        "average_seconds": analysed_seconds,
        "verified": verified,
        "with_evidence": with_evidence,
        "reused": reused,
        "seconds_saved": round((analysed_seconds or 0) * reused),
        "feedback_correct": feedback.get("correct", 0),
        "feedback_wrong": feedback.get("wrong", 0),
        "completed": await session.scalar(select(func.count()).select_from(completed)) or 0,
    }


async def requeue(session: AsyncSession, investigation_id: int) -> bool:
    investigation = await get(session, investigation_id)
    if investigation is None or investigation.status not in REQUEUEABLE:
        return False
    investigation.status = "queued"
    investigation.error = None
    investigation.started_at = investigation.finished_at = investigation.duration_seconds = None
    for field in _RESULT_FIELDS:
        setattr(investigation, field, None)
    await session.commit()
    return True
