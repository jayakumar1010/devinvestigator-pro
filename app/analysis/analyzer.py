from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Literal

from pydantic import ValidationError

from app.analysis.confidence import assess_confidence
from app.analysis.grounding import validate_evidence
from app.analysis.models import AnalysisResult, LLMCallInfo, RootCauseAnalysis, ToolCallRecord
from app.analysis.prompts import build_messages
from app.evidence.models import FailureEvidence
from app.llm.base import LLMError, LLMMessage, LLMProvider, LLMResult

def _flat_schema(schema: dict) -> dict:
    """Inline $ref definitions: grammar-constrained decoding (e.g. Ollama) is most reliable on a flat schema."""
    definitions = schema.get("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(definitions[node["$ref"].rsplit("/", 1)[-1]])
            return {key: resolve(value) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [resolve(value) for value in node]
        return node

    return resolve(schema)


ANALYSIS_SCHEMA = _flat_schema(RootCauseAnalysis.model_json_schema())


def parse_analysis(result: LLMResult) -> RootCauseAnalysis:
    try:
        return RootCauseAnalysis.model_validate(result.content)
    except ValidationError as exc:
        raise LLMError(
            f"{result.provider}/{result.model} returned output that does not match the analysis schema "
            f"({exc.error_count()} errors)"
        ) from exc


def build_result(
    evidence: FailureEvidence,
    analysis: RootCauseAnalysis,
    messages: list[LLMMessage],
    llm: LLMCallInfo,
    *,
    mode: Literal["single_pass", "agent"] = "single_pass",
    tool_calls: Sequence[ToolCallRecord] = (),
) -> AnalysisResult:
    """Validate citations against everything sent, apply the confidence policy, assemble the result."""
    validation = validate_evidence(analysis.evidence, messages)
    assessment = assess_confidence(analysis, validation)
    return AnalysisResult(
        repository=evidence.repository.full_name,
        workflow=evidence.workflow.name,
        run_id=evidence.run.id,
        run_number=evidence.run.run_number,
        run_url=evidence.run.html_url,
        status=evidence.status,
        failed_stage=evidence.failed_stage,
        mode=mode,
        analysis=analysis,
        evidence_validation=validation,
        confidence=assessment.final,
        confidence_assessment=assessment,
        llm=llm,
        tool_calls=list(tool_calls),
        analyzed_at=datetime.now(timezone.utc),
    )


async def analyze_failure(evidence: FailureEvidence, provider: LLMProvider) -> AnalysisResult:
    """Single-pass analysis: one LLM call over the collected evidence."""
    messages = build_messages(evidence)
    result = await provider.complete_json(messages, ANALYSIS_SCHEMA)
    llm = LLMCallInfo(
        provider=result.provider,
        model=result.model,
        duration_seconds=result.duration_seconds,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    return build_result(evidence, parse_analysis(result), messages, llm)
