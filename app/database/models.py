from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Investigation(Base):
    """One investigation of one failed CI run attempt."""

    __tablename__ = "investigations"
    __table_args__ = (
        UniqueConstraint("provider", "repository", "run_id", "run_attempt", name="uq_investigations_run_attempt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # queued, running, completed, failed, collected
    trigger: Mapped[str] = mapped_column(String(16))  # webhook, manual
    provider: Mapped[str] = mapped_column(String(32))
    repository: Mapped[str] = mapped_column(String(255), index=True)
    run_id: Mapped[int] = mapped_column(BigInteger)
    run_attempt: Mapped[int] = mapped_column(Integer)
    run_number: Mapped[int | None] = mapped_column(Integer)
    workflow: Mapped[str | None] = mapped_column(String(255))
    run_url: Mapped[str | None] = mapped_column(String(500))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    head_branch: Mapped[str | None] = mapped_column(String(255))
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    delivery_id: Mapped[str | None] = mapped_column(String(64))
    failed_stage: Mapped[str | None] = mapped_column(String(500))
    error: Mapped[str | None] = mapped_column(Text)

    mode: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(32), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    root_cause: Mapped[str | None] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    confidence_basis: Mapped[str | None] = mapped_column(String(16))
    evidence_status: Mapped[str | None] = mapped_column(String(32))

    notification_url: Mapped[str | None] = mapped_column(String(500))  # posted comment
    notification_error: Mapped[str | None] = mapped_column(Text)

    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # FailureEvidence
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # AnalysisResult

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
