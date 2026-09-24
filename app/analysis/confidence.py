"""Conservative confidence policy, enforced on top of the model's self-reported score.

The model reports a confidence and its basis; the backend applies ceilings so the final
score never implies more certainty than the evidence supports.
"""

from app.analysis.models import ConfidenceAssessment, EvidenceValidation, RootCauseAnalysis

MAX_CONFIDENCE = 0.95  # log evidence never proves a cause with certainty
MAX_INFERRED_CONFIDENCE = 0.89  # "below 0.9" when the cause is not directly shown
MAX_INSUFFICIENT_CONFIDENCE = 0.5


def assess_confidence(analysis: RootCauseAnalysis, validation: EvidenceValidation) -> ConfidenceAssessment:
    limits = [(MAX_CONFIDENCE, "log evidence cannot prove a cause with certainty")]
    if analysis.confidence_basis == "inferred":
        limits.append((MAX_INFERRED_CONFIDENCE, "root cause is inferred, not directly shown"))
    if analysis.confidence_basis == "insufficient":
        limits.append((MAX_INSUFFICIENT_CONFIDENCE, "evidence is insufficient"))
    if analysis.category == "unknown":
        limits.append((MAX_INSUFFICIENT_CONFIDENCE, "category is unknown"))
    if validation.status != "verified":
        limits.append((MAX_INFERRED_CONFIDENCE, f"cited evidence is not verified ({validation.status})"))

    ceiling, reason = min(limits, key=lambda limit: limit[0])
    adjusted = analysis.confidence > ceiling
    return ConfidenceAssessment(
        reported=analysis.confidence,
        final=round(min(analysis.confidence, ceiling), 2),
        basis=analysis.confidence_basis,
        ceiling=ceiling,
        adjusted=adjusted,
        reason=reason if adjusted else None,
    )
