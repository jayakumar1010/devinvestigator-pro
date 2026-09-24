"""Team rules: known failures answered without asking a model.

A repository can carry a `.devinvestigator.yml` describing failures the team already
understands. A matching rule produces the answer directly, so it is instant, free and
always the same. The matched log line becomes the evidence, so it still passes the usual
verification.
"""

import logging
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.analysis.models import EvidenceItem, FailureCategory, RootCauseAnalysis
from app.evidence.logs import strip_timestamps
from app.evidence.models import FailureEvidence

logger = logging.getLogger(__name__)

MAX_RULES = 100
MAX_CONFIDENCE = 0.9  # a rule is a team's judgement, not proof from this run


class Rule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    match: str = Field(min_length=3)  # text that appears in the failing log
    category: FailureCategory = "unknown"
    root_cause: str
    fix: str
    confidence: float = 0.8

    @field_validator("confidence")
    @classmethod
    def _cap(cls, value: float) -> float:
        return min(max(value, 0.0), MAX_CONFIDENCE)


def load_rules(text: str) -> list[Rule]:
    """Parse a .devinvestigator.yml. A broken rule is skipped, not fatal."""
    try:
        document = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        logger.warning("Ignoring the rules file: it is not valid YAML (%s)", type(exc).__name__)
        return []
    if not isinstance(document, dict):
        return []

    rules = []
    for entry in (document.get("rules") or [])[:MAX_RULES]:
        try:
            rules.append(Rule.model_validate(entry))
        except ValidationError as exc:
            logger.warning("Ignoring one rule in the rules file (%d problems)", exc.error_count())
    return rules


def failing_text(evidence: FailureEvidence) -> str:
    """What the rules are matched against: the failing jobs' error sections."""
    parts = []
    for job in evidence.failed_jobs:
        log = job.log
        parts.append(strip_timestamps(log.error_excerpt or log.content or ""))
    return "\n".join(parts)


def match_rule(rules: list[Rule], evidence: FailureEvidence) -> tuple[Rule, str] | None:
    """The first matching rule and the log line that matched it."""
    text = failing_text(evidence)
    lowered = text.lower()
    for rule in rules:
        needle = rule.match.lower()
        if needle not in lowered:
            continue
        line = next((raw.strip() for raw in text.splitlines() if needle in raw.lower()), rule.match)
        return rule, line
    return None


def rule_analysis(rule: Rule, matched_line: str) -> RootCauseAnalysis:
    return RootCauseAnalysis(
        summary=f"Known failure: {rule.name}.",
        evidence=[EvidenceItem(source="job log", quote=matched_line)],
        root_cause=rule.root_cause,
        category=rule.category,
        confidence_basis="direct",
        confidence=rule.confidence,
        recommendation=rule.fix,
    )
