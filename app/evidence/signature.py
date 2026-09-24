"""A stable fingerprint for a failure, so the same failure can be recognised again.

Two runs of the same problem produce the same signature even though line numbers, commit
hashes, durations and temporary paths differ, because those are normalised away.
"""

import hashlib
import re

from app.evidence.logs import strip_timestamps
from app.evidence.models import FailureEvidence

SIGNATURE_LENGTH = 16
MEANINGFUL_LINES = 3
MIN_LINE_CHARS = 8

# Anything that changes between two runs of the same failure: hashes, numbers, paths, urls.
_VARIABLE = re.compile(r"https?://\S+|/\S+|[0-9a-f]{7,}|\d+")
_WHITESPACE = re.compile(r"\s+")


def normalize_line(line: str) -> str:
    return _WHITESPACE.sub(" ", _VARIABLE.sub("#", line)).strip().lower()


def error_lines(evidence: FailureEvidence, limit: int = MEANINGFUL_LINES) -> list[str]:
    """The first few real error lines of the first failed job, normalised."""
    if not evidence.failed_jobs:
        return []
    log = evidence.failed_jobs[0].log
    text = log.error_excerpt or log.content or ""
    lines = []
    for raw in strip_timestamps(text).splitlines():
        line = normalize_line(raw)
        # Skip GitHub's own group/command markers and very short lines.
        if not line or line.startswith("##[") or line.startswith("[command]") or len(line) < MIN_LINE_CHARS:
            continue
        lines.append(line)
        if len(lines) == limit:
            break
    return lines


def failure_signature(evidence: FailureEvidence) -> str:
    parts = [
        evidence.repository.full_name,
        evidence.workflow.name or "",
        evidence.failed_stage or "",
        *error_lines(evidence),
    ]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:SIGNATURE_LENGTH]
