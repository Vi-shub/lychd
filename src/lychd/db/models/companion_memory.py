"""Private reviewed companion aliases; no audio, credentials or conversation log."""

from __future__ import annotations

from uuid import UUID

from advanced_alchemy.base import UUIDAuditBase
from sqlalchemy import CheckConstraint, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class CompanionRecipe(UUIDAuditBase):
    """One exact alias per namespace/key, with canonical document custody.

    Run and request UUIDs are lab provenance. They deliberately have no foreign
    keys: the local native memory Run need not exist in the durable Run ledger.
    """

    __tablename__ = "companion_recipe"
    __table_args__ = (
        UniqueConstraint("namespace", "key", name="uq_companion_recipe_namespace_key"),
        CheckConstraint("namespace ~ '^[0-9a-f]{64}$'", name="namespace_sha256"),
        CheckConstraint("key ~ '^[0-9a-f]{64}$'", name="key_sha256"),
        CheckConstraint("language IN ('sk', 'cs', 'en')", name="language_closed"),
        CheckConstraint(
            "char_length(phrase) BETWEEN 1 AND 200 AND octet_length(phrase) BETWEEN 1 AND 800",
            name="phrase_bound",
        ),
        CheckConstraint("octet_length(recipe_bytes) BETWEEN 1 AND 4096", name="document_bound"),
        CheckConstraint("recipe_digest ~ '^[0-9a-f]{64}$'", name="document_sha256"),
    )

    namespace: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(2))
    phrase: Mapped[str] = mapped_column(String(200))
    recipe_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    recipe_digest: Mapped[str] = mapped_column(String(64))
    learned_request_id: Mapped[UUID]
    learned_run_id: Mapped[UUID]
