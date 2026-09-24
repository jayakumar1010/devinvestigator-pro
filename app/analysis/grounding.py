"""Check that the evidence an LLM cites was actually in what it was sent.

Only each item's "quote" is checked; "source" is a label saying where it comes from. A
quote is valid only if, after light normalization, it appears verbatim in the evidence
messages sent to the model (user role: the initial evidence and every tool result). Our
system prompt and the model's own earlier replies do not count. Paraphrases, invented
lines and log lines that were never sent (for example with the timestamps we strip) are
invalid.
"""

import re
from collections.abc import Sequence

from app.analysis.models import EvidenceCheck, EvidenceItem, EvidenceValidation
from app.llm.base import LLMMessage

MIN_CITATION_CHARS = 4
_WHITESPACE = re.compile(r"\s+")
# The line-number gutter get_file adds ("   6 | "). Ignored on both sides, so consecutive
# code lines can be quoted without it.
_LINE_NUMBER_GUTTER = re.compile(r"^[ \t]*\d+ \| ?", re.MULTILINE)
# Quote characters are dropped on both sides: models often wrap citations in them, and the
# evidence block is JSON, so values sent as "key": "value" can be cited as key: value.
_QUOTES = str.maketrans("", "", '"`')


def normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", _LINE_NUMBER_GUTTER.sub("", text).translate(_QUOTES)).strip()


def validate_evidence(items: Sequence[EvidenceItem | str], messages: list[LLMMessage]) -> EvidenceValidation:
    sent = normalize("\n".join(m.content for m in messages if m.role == "user"))
    checks: list[EvidenceCheck] = []
    for item in items:
        if isinstance(item, str):
            item = EvidenceItem(source="unspecified", quote=item)
        needle = normalize(item.quote)
        if len(needle) > 2 and needle[0] == needle[-1] == "'":
            needle = needle[1:-1].strip()
        if len(needle) < MIN_CITATION_CHARS:
            reason = "too short to verify"
        elif needle in sent:
            reason = None
        else:
            reason = "not found in the evidence sent to the model"
        checks.append(EvidenceCheck(source=item.source, quote=item.quote, valid=reason is None, reason=reason))

    invalid = sum(not c.valid for c in checks)
    if not checks:
        status = "no_evidence"
    elif invalid:
        status = "contains_invalid_evidence"
    else:
        status = "verified"
    return EvidenceValidation(status=status, checked=len(checks), invalid=invalid, items=checks)
