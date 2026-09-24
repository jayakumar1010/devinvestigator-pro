from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FailureCategory = Literal[
    "dependency_failure",
    "build_failure",
    "test_failure",
    "lint_failure",
    "configuration_error",
    "infrastructure_failure",
    "timeout",
    "permission_error",
    "network_failure",
    "unknown",
]

ConfidenceBasis = Literal["direct", "inferred", "insufficient"]


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: str = Field(
        description='Where the quote comes from, e.g. "job log", "lib/discount.js line 6", "commit" or "comparison".'
    )
    quote: str = Field(
        description="Text copied exactly from the evidence: one line or consecutive lines, "
        "with no file names, line numbers or labels added."
    )


class RootCauseAnalysis(BaseModel):
    """What the LLM must return. Its JSON schema is sent to the provider as the output format.

    Field order is the order the model writes in: evidence first, then conclusions.
    """

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(description="One sentence: what failed and where.")
    evidence: list[EvidenceItem] = Field(
        description="Supporting facts: where each comes from and a quote copied exactly from the evidence. "
        "Each quote is checked against the input."
    )
    root_cause: str = Field(
        description="Why the failure happened: what is wrong in the code, configuration, dependencies or "
        "environment. Not a restatement of the error message or symptom."
    )
    category: FailureCategory = Field(description="The failure mechanism, as defined in the instructions.")
    confidence_basis: ConfidenceBasis = Field(
        description="direct: a line in the evidence explicitly states the cause. inferred: the cause is deduced "
        "from symptoms, or the relevant code or configuration is not in the evidence. insufficient: the evidence "
        "cannot establish the cause."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="0.0 to 1.0. direct: at most 0.95. inferred: below 0.9. insufficient: 0.5 or below. Never 1.0.",
    )
    recommendation: str = Field(
        description="Concrete next step that fixes the root cause, not a workaround that hides it. A recommendation only."
    )

    @field_validator("evidence", mode="before")
    @classmethod
    def _plain_strings_as_items(cls, value: object) -> object:
        # Results stored before evidence had a source, and some models, give plain strings.
        if isinstance(value, list):
            return [{"source": "unspecified", "quote": v} if isinstance(v, str) else v for v in value]
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _percent_to_fraction(cls, value: object) -> object:
        # Some models answer 94 when they mean 0.94.
        if isinstance(value, (int, float)) and 1 < value <= 100:
            return value / 100
        return value


class LLMCallInfo(BaseModel):
    provider: str
    model: str
    duration_seconds: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    calls: int = 1


class ToolCallRecord(BaseModel):
    step: int
    tool: str
    arguments: dict[str, str] = {}
    reasoning: str
    ok: bool
    error: str | None = None
    result_chars: int
    duration_seconds: float


class EvidenceCheck(BaseModel):
    source: str = "unspecified"
    quote: str
    valid: bool
    reason: str | None = None


class EvidenceValidation(BaseModel):
    """Each cited evidence item checked against what the model was actually sent."""

    status: Literal["verified", "contains_invalid_evidence", "no_evidence"]
    checked: int
    invalid: int
    items: list[EvidenceCheck]


class ConfidenceAssessment(BaseModel):
    """The model's reported confidence and the final score after the confidence policy."""

    reported: float
    final: float
    basis: ConfidenceBasis
    ceiling: float
    adjusted: bool
    reason: str | None = None


class AnalysisResult(BaseModel):
    repository: str
    workflow: str | None = None
    run_id: int
    run_number: int
    run_url: str | None = None
    status: str
    failed_stage: str | None = None
    mode: Literal["single_pass", "agent"] = "single_pass"
    analysis: RootCauseAnalysis  # exactly as the model returned it
    evidence_validation: EvidenceValidation
    confidence: float  # final score, after the confidence policy
    confidence_assessment: ConfidenceAssessment
    llm: LLMCallInfo
    tool_calls: list[ToolCallRecord] = []
    analyzed_at: datetime
