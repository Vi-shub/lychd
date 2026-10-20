"""Restrictive durable identity and current-policy records for local Ward."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from advanced_alchemy.base import UUIDAuditBase
from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class WardPrincipal(UUIDAuditBase):
    __tablename__ = "ward_principal"
    __table_args__ = (
        CheckConstraint("kind IN ('human','service','peer','node')", name="kind"),
        UniqueConstraint("id", "kind", name="uq_ward_principal_kind_identity"),
        CheckConstraint("generation >= 1 AND policy_generation >= 1", name="generations"),
    )
    kind: Mapped[str] = mapped_column(String(16))
    generation: Mapped[int] = mapped_column(default=1)
    policy_generation: Mapped[int] = mapped_column(default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class WardUser(UUIDAuditBase):
    __tablename__ = "ward_user"
    __table_args__ = (
        CheckConstraint("principal_kind = 'human'", name="human"),
        ForeignKeyConstraint(
            ["principal_id", "principal_kind"], ["ward_principal.id", "ward_principal.kind"], ondelete="RESTRICT"
        ),
    )
    principal_id: Mapped[UUID] = mapped_column(unique=True)
    principal_kind: Mapped[str] = mapped_column(String(16), default="human")


class WardCredential(UUIDAuditBase):
    __tablename__ = "ward_credential"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="generation"),
        UniqueConstraint("id", "principal_id", name="uq_ward_credential_principal_identity"),
        CheckConstraint("secret_digest ~ '^[0-9a-f]{64}$'", name="digest"),
    )
    principal_id: Mapped[UUID] = mapped_column(ForeignKey("ward_principal.id", ondelete="RESTRICT"), index=True)
    issuer_id: Mapped[UUID] = mapped_column()
    protocol: Mapped[str] = mapped_column(String(32))
    audience: Mapped[str] = mapped_column(String(64))
    secret_digest: Mapped[str] = mapped_column(String(64), unique=True)
    generation: Mapped[int] = mapped_column(default=1)
    exchanged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class WardSession(UUIDAuditBase):
    """Authentication continuity, unrelated to Bridge's session table."""

    __tablename__ = "ward_session"
    __table_args__ = (
        ForeignKeyConstraint(
            ["credential_id", "principal_id"],
            ["ward_credential.id", "ward_credential.principal_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "generation >= 1 AND principal_generation >= 1 AND credential_generation >= 1", name="generations"
        ),
        CheckConstraint("secret_digest ~ '^[0-9a-f]{64}$'", name="digest"),
    )
    principal_id: Mapped[UUID] = mapped_column(ForeignKey("ward_principal.id", ondelete="RESTRICT"), index=True)
    credential_id: Mapped[UUID] = mapped_column(index=True)
    secret_digest: Mapped[str] = mapped_column(String(64), unique=True)
    audience: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(default=1)
    principal_generation: Mapped[int] = mapped_column()
    credential_generation: Mapped[int] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class WardPermission(UUIDAuditBase):
    """One exact assignment; absent and revoked rows both deny."""

    __tablename__ = "ward_permission"
    __table_args__ = (
        UniqueConstraint("principal_id", "action", "object_id", "audience", "purpose", name="uq_ward_permission_exact"),
    )
    principal_id: Mapped[UUID] = mapped_column(ForeignKey("ward_principal.id", ondelete="RESTRICT"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    object_id: Mapped[UUID] = mapped_column()
    audience: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(96))
    expires_at: Mapped[datetime] = mapped_column()
    allowed: Mapped[bool] = mapped_column(default=False)


class WardAudit(UUIDAuditBase):
    """Fixed event vocabulary and identity/generations only; no secret or payload."""

    __tablename__ = "ward_audit"
    __table_args__ = (
        CheckConstraint(
            "(event = 'publication-attempt' AND outcome IS NOT NULL AND outcome IN ('unknown','refused') AND expected_policy_generation IS NOT NULL AND expected_policy_generation >= 1 AND issuer_id IS NOT NULL) OR (event <> 'publication-attempt' AND outcome IS NULL)",
            name="publication_outcome",
        ),
    )
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expected_policy_generation: Mapped[int | None] = mapped_column(nullable=True)
    issuer_id: Mapped[UUID | None] = mapped_column(nullable=True)
    event: Mapped[str] = mapped_column(String(32))
    permitted: Mapped[bool] = mapped_column()
    principal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ward_principal.id", ondelete="RESTRICT"), nullable=True
    )
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ward_credential.id", ondelete="RESTRICT"), nullable=True
    )
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("ward_session.id", ondelete="RESTRICT"), nullable=True)
    policy_generation: Mapped[int | None] = mapped_column(nullable=True)
    principal_generation: Mapped[int | None] = mapped_column(nullable=True)
    credential_generation: Mapped[int | None] = mapped_column(nullable=True)
    session_generation: Mapped[int | None] = mapped_column(nullable=True)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    object_id: Mapped[UUID | None] = mapped_column(nullable=True)
    audience: Mapped[str | None] = mapped_column(String(64), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(96), nullable=True)
