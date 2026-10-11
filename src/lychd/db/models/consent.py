from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from advanced_alchemy.base import UUIDAuditBase
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class Consent(UUIDAuditBase):
    """Honest HitL record: the DB flag a run parks on + the audit trail."""

    __tablename__ = "consent"
    __table_args__ = (
        Index("ix_consent_run_status", "run_id", "status"),
        CheckConstraint(
            "(NOT native_review_required AND native_binding IS NULL) OR "
            "(native_review_required AND native_binding IS NOT NULL AND jsonb_typeof(native_binding) = 'object')",
            name="native_binding_requirement",
        ),
        CheckConstraint(
            "status = 'pending' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decision_receipt",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"))
    tool_name: Mapped[str] = mapped_column(String(200))
    tool_call_id: Mapped[str] = mapped_column(String(100))  # pydantic-ai deferred id
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # tool args, censored
    native_review_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    native_binding: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(10),
        default="pending",
    )  # pending|granted|denied|expired|cancelled
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    preauth_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #   set when auto-granted by a CodexPreauthorization; NULL means live Magus consent
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
