"""Turn stored investigations into plain values for the templates.

Reads the stored JSON defensively: results saved by older versions (for example evidence
as plain strings) still render.
"""

from datetime import datetime
from typing import Any

from app.database.models import Investigation
from app.evidence.logs import strip_timestamps

MAX_LOG_LINES = 80


def safe_url(url: str | None) -> str | None:
    """Only https links are rendered (never javascript: or similar)."""
    return url if url and url.startswith("https://") else None


def _time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.1f} min"


def _confidence(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def summary_row(investigation: Investigation) -> dict[str, Any]:
    return {
        "id": investigation.id,
        "status": investigation.status,
        "repository": investigation.repository,
        "workflow": investigation.workflow or "?",
        "run_number": investigation.run_number or "?",
        "failed_stage": investigation.failed_stage or "-",
        "category": investigation.category or "-",
        "confidence": _confidence(investigation.confidence),
        "created": _time(investigation.created_at),
        "duration": _duration(investigation.duration_seconds),
    }


def _evidence_items(validation: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "source": item.get("source") or "-",
            "quote": item.get("quote", item.get("text", "")),
            "valid": bool(item.get("valid")),
            "reason": item.get("reason"),
        }
        for item in validation.get("items", [])
    ]


def _tool_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "tool": call.get("tool"),
            "path": (call.get("arguments") or {}).get("path"),
            "ok": call.get("ok"),
            "error": call.get("error"),
            "reasoning": call.get("reasoning"),
        }
        for call in result.get("tool_calls") or []
    ]


def _jobs(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = []
    for job in evidence.get("failed_jobs") or []:
        log = job.get("log") or {}
        excerpt = log.get("error_excerpt")
        if not excerpt and log.get("content"):
            excerpt = "\n".join(log["content"].splitlines()[-MAX_LOG_LINES:])
        jobs.append({
            "name": job.get("name"),
            "url": safe_url(job.get("html_url")),
            "failed_steps": [step.get("name") for step in job.get("failed_steps") or []],
            "excerpt": strip_timestamps(excerpt) if excerpt else None,
            "log_error": log.get("error"),
        })
    return jobs


def _commit(evidence: dict[str, Any]) -> dict[str, Any] | None:
    commit = evidence.get("commit")
    if not commit:
        return None
    return {
        "sha_short": (commit.get("sha") or "")[:12],
        "message": (commit.get("message") or "").splitlines()[0] if commit.get("message") else "",
        "author": commit.get("author_name"),
        "files": [
            f"{f.get('status')} {f.get('filename')} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
            for f in commit.get("changed_files") or []
        ],
    }


def detail(investigation: Investigation) -> dict[str, Any]:
    result = investigation.result or {}
    evidence = investigation.evidence or {}
    assessment = result.get("confidence_assessment") or {}
    return {
        **summary_row(investigation),
        "trigger": investigation.trigger,
        "head_branch": investigation.head_branch,
        "head_sha_short": (investigation.head_sha or "")[:12] or "-",
        "run_url": safe_url(investigation.run_url),
        "model": investigation.model,
        "mode": investigation.mode,
        "error": investigation.error,
        "analysis": result.get("analysis"),
        "evidence_status": investigation.evidence_status,
        "confidence_detail": {
            "final": _confidence(result.get("confidence")),
            "basis": assessment.get("basis"),
            "reported": _confidence(assessment.get("reported")),
            "adjusted": bool(assessment.get("adjusted")),
            "reason": assessment.get("reason"),
        },
        "evidence_items": _evidence_items(result.get("evidence_validation") or {}),
        "tool_calls": _tool_calls(result),
        "jobs": _jobs(evidence),
        "commit": _commit(evidence),
        "collection_errors": [f"{e.get('source')}: {e.get('message')}" for e in evidence.get("errors") or []],
    }
