"""Bounded values for the explicitly selected local public-note Archive profile."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Label = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PROFILE = "local-public-notes@1"
POLICY = "postgres-simple-lexical@1"
MAX_NOTE_CHARS = 16_000
MAX_QUERY_CHARS = 2_000
MAX_RESULTS = 20
MAX_RECALL_CHARS = 32_000
MAX_METADATA_RESULTS = 50
MAX_METADATA_CURSOR_CHARS = 2048


class ArchiveValue(BaseModel):
    """Frozen JSON values with no open metadata bag."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ArchiveRefused(ValueError):  # noqa: N818 - named domain refusal
    """Safe typed refusal without source or query bytes in the exception."""

    def __init__(self, code: str) -> None:
        """Retain a stable refusal code without echoing protected content."""
        self.code = code
        super().__init__(f"Archive refused: {code}.")


def digest(value: Any) -> str:
    """Digest this profile's deterministic local JSON encoding, not portable JCS."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def text_digest(text: str) -> str:
    """Bind exact UTF-8 bytes without silently normalizing source text."""
    return sha256(text.encode()).hexdigest()


class ArchiveSubject(ArchiveValue):
    """Identity supplied by trusted host composition, never by a Sigil fallback."""

    identity: Label


class ArchiveProfile(ArchiveValue):
    """One configured namespace and its trusted retained ownership/policy binding."""

    profile: Literal["local-public-notes@1"] = PROFILE
    namespace_id: UUID
    owner_subject: ArchiveSubject
    policy_revision: Label


class NoteProposal(ArchiveValue):
    namespace_id: UUID
    request_id: UUID
    source_key: Label
    source_revision: Label
    source_digest: Digest
    text: Annotated[str, Field(min_length=1, max_length=MAX_NOTE_CHARS)]
    producer: Label
    observed_at: datetime
    classification: Literal["public"] = "public"

    @field_validator("text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        """Reject PostgreSQL-incompatible NUL and content with no material."""
        if not value.strip() or "\x00" in value:
            msg = "Public note text must be nonblank and contain no NUL."
            raise ValueError(msg)
        value.encode("utf-8")
        return value

    @model_validator(mode="after")
    def exact_source(self) -> NoteProposal:
        """Require an exact source digest and timezone-aware observation."""
        if self.source_digest != text_digest(self.text):
            msg = "Public note source digest does not match its text."
            raise ValueError(msg)
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            msg = "Public note observation time must be timezone-aware."
            raise ValueError(msg)
        return self

    def source_binding(self) -> dict[str, Any]:
        """Immutable revision closure, excluding an intake attempt's request id."""
        return self.model_dump(mode="json", exclude={"request_id"})


class Lifecycle(StrEnum):
    CANDIDATE = "candidate"
    PROMOTED = "promoted"
    ARCHIVED = "archived"
    REVOKED = "revoked"


class RecordRef(ArchiveValue):
    record_id: UUID
    revision_digest: Digest
    lifecycle_version: Annotated[int, Field(ge=1)]


class NoteRecord(ArchiveValue):
    namespace_id: UUID
    reference: RecordRef
    source_key: Label
    source_revision: Label
    source_digest: Digest
    producer: Label
    observed_at: datetime
    lifecycle: Lifecycle
    created_at: datetime


class NoteListCursor(ArchiveValue):
    """Exact keyset position bound to one namespace and source filter."""

    namespace_id: UUID
    source_key: Label | None = None
    created_at: datetime
    record_id: UUID

    @field_validator("created_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "Metadata cursor time must be timezone-aware."
            raise ValueError(msg)
        return value

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def decode(cls, value: str) -> NoteListCursor:
        """Reject oversized, malformed and noncanonical continuation values."""
        if not value or len(value) > MAX_METADATA_CURSOR_CHARS:
            msg = "Invalid Archive metadata cursor."
            raise ValueError(msg)
        try:
            raw = base64.b64decode(value, altchars=b"-_", validate=True)
            cursor = cls.model_validate_json(raw)
        except (ValueError, binascii.Error):
            msg = "Invalid Archive metadata cursor."
            raise ValueError(msg) from None
        if cursor.encode() != value:
            msg = "Invalid Archive metadata cursor."
            raise ValueError(msg)
        return cursor


class NoteListRequest(ArchiveValue):
    """Finite metadata inspection, never a recall or text-search request."""

    namespace_id: UUID
    limit: Annotated[int, Field(ge=1, le=MAX_METADATA_RESULTS)] = MAX_METADATA_RESULTS
    source_key: Label | None = None
    cursor: Annotated[str, Field(min_length=1, max_length=MAX_METADATA_CURSOR_CHARS)] | None = None

    @model_validator(mode="after")
    def bound_cursor(self) -> NoteListRequest:
        if self.cursor is not None:
            cursor = NoteListCursor.decode(self.cursor)
            if (cursor.namespace_id, cursor.source_key) != (self.namespace_id, self.source_key):
                msg = "Archive metadata cursor does not match this query."
                raise ValueError(msg)
        return self


class NoteListPage(ArchiveValue):
    """Metadata across every lifecycle, including retained revoked tombstones."""

    records: Annotated[tuple[NoteRecord, ...], Field(max_length=MAX_METADATA_RESULTS)]
    next_cursor: Annotated[str, Field(max_length=MAX_METADATA_CURSOR_CHARS)] | None = None


class RecallRequest(ArchiveValue):
    namespace_id: UUID
    request_id: UUID
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)]
    max_results: Annotated[int, Field(ge=1, le=MAX_RESULTS)] = 5
    char_budget: Annotated[int, Field(ge=1, le=MAX_RECALL_CHARS)] = 8_000
    min_score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 0.0
    run_id: UUID | None = None

    def binding_digest(self, profile: ArchiveProfile, subject: ArchiveSubject, purpose: str) -> str:
        """Bind the complete request, including defaults, to its retained admission."""
        return digest(
            {
                "request": self.model_dump(mode="json"),
                "subject": subject.model_dump(),
                "purpose": purpose,
                "profile": profile.model_dump(mode="json"),
            }
        )

    @field_validator("query")
    @classmethod
    def valid_query(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            msg = "Recall query must be nonblank and contain no NUL."
            raise ValueError(msg)
        value.encode("utf-8")
        return value


class RecallSelection(ArchiveValue):
    reference: RecordRef
    source_key: Label
    source_revision: Label
    source_digest: Digest
    score: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    projection_digest: Digest
    projected_chars: Annotated[int, Field(ge=1, le=MAX_NOTE_CHARS)]


class RecallReceipt(ArchiveValue):
    """Durable selection evidence. Neither raw query nor recalled text is retained."""

    schema_version: Literal[1] = 1
    receipt_id: UUID
    request_id: UUID
    namespace_id: UUID
    subject: ArchiveSubject
    purpose: Label
    profile: Literal["local-public-notes@1"] = PROFILE
    retrieval_policy: Literal["postgres-simple-lexical@1"] = POLICY
    policy_revision: Label
    query_digest: Digest
    request_digest: Digest
    run_id: UUID | None
    max_results: Annotated[int, Field(ge=1, le=MAX_RESULTS)]
    char_budget: Annotated[int, Field(ge=1, le=MAX_RECALL_CHARS)]
    min_score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    selected: Annotated[tuple[RecallSelection, ...], Field(max_length=MAX_RESULTS)]
    omitted_for_budget: Annotated[int, Field(ge=0, le=MAX_RESULTS)]
    created_at: datetime
    receipt_digest: Digest

    def computed_digest(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"receipt_digest"}))


class RecallItem(ArchiveValue):
    selection: RecallSelection
    text: Annotated[str, Field(min_length=1, max_length=MAX_NOTE_CHARS)]


class RecallProjection(ArchiveValue):
    """Transient exact source bytes resolved from a currently valid receipt."""

    receipt: RecallReceipt
    items: Annotated[tuple[RecallItem, ...], Field(max_length=MAX_RESULTS)]


type ArchiveAction = Literal[
    "profile_discover",
    "inspect",
    "admit",
    "promote",
    "archive",
    "revoke",
    "recall",
    "resolve",
    "generation_discover",
    "generation_register",
    "generation_retire",
    "derive",
    "derive_status",
]


class ArchiveAuthorization(ArchiveValue):
    subject: ArchiveSubject
    namespace_id: UUID
    owner_subject: ArchiveSubject
    policy_revision: Label
    action: ArchiveAction
    purpose: Label
    object_id: UUID | None
    binding_digest: Digest
