import pytest

from app.analysis.confidence import (
    MAX_CONFIDENCE,
    MAX_INFERRED_CONFIDENCE,
    MAX_INSUFFICIENT_CONFIDENCE,
    assess_confidence,
)
from app.analysis.models import EvidenceCheck, EvidenceValidation, RootCauseAnalysis
from app.core.config import Settings

VERIFIED = EvidenceValidation(status="verified", checked=1, invalid=0, items=[EvidenceCheck(quote="x" * 10, valid=True)])
INVALID = EvidenceValidation(
    status="contains_invalid_evidence", checked=1, invalid=1,
    items=[EvidenceCheck(quote="made up", valid=False, reason="not found")],
)


def analysis(confidence: float, basis: str = "direct", category: str = "dependency_failure") -> RootCauseAnalysis:
    return RootCauseAnalysis(
        summary="s", evidence=["x" * 10], root_cause="r", category=category,
        confidence_basis=basis, confidence=confidence, recommendation="fix",
    )


def test_certainty_is_never_allowed() -> None:
    result = assess_confidence(analysis(1.0), VERIFIED)
    assert (result.reported, result.final, result.adjusted) == (1.0, MAX_CONFIDENCE, True)
    assert "certainty" in result.reason


def test_direct_and_verified_high_confidence_is_kept() -> None:
    result = assess_confidence(analysis(0.93), VERIFIED)
    assert (result.final, result.adjusted, result.reason) == (0.93, False, None)


@pytest.mark.parametrize("reported", [0.9, 0.95, 1.0])
def test_inferred_stays_below_0_9(reported: float) -> None:
    result = assess_confidence(analysis(reported, basis="inferred"), VERIFIED)
    assert result.final == MAX_INFERRED_CONFIDENCE < 0.9
    assert "inferred" in result.reason


def test_insufficient_is_capped_low() -> None:
    result = assess_confidence(analysis(0.8, basis="insufficient"), VERIFIED)
    assert result.final == MAX_INSUFFICIENT_CONFIDENCE


def test_unknown_category_is_capped_low_even_if_basis_claims_direct() -> None:
    result = assess_confidence(analysis(0.9, category="unknown"), VERIFIED)
    assert result.final == MAX_INSUFFICIENT_CONFIDENCE


def test_unverified_evidence_cannot_support_high_confidence() -> None:
    result = assess_confidence(analysis(0.95, basis="direct"), INVALID)
    assert result.final == MAX_INFERRED_CONFIDENCE
    assert "not verified" in result.reason


def test_the_strictest_limit_wins() -> None:
    result = assess_confidence(analysis(0.9, basis="insufficient"), INVALID)
    assert result.final == MAX_INSUFFICIENT_CONFIDENCE


def test_low_confidence_is_never_raised() -> None:
    result = assess_confidence(analysis(0.3, basis="inferred"), INVALID)
    assert (result.final, result.adjusted) == (0.3, False)


def test_default_local_model_is_gemma() -> None:
    assert Settings(_env_file=None).ollama_model == "hf.co/ggml-org/gemma-4-31b-it-GGUF:Q4_K_M"
