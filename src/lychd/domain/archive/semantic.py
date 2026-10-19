"""Strict semantic request and text-free receipt values for one configured consumer."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from lychd.domain.animation.embeddings import embedding_digest
from lychd.domain.archive.contracts import (
    MAX_NOTE_CHARS,
    MAX_QUERY_CHARS,
    MAX_RECALL_CHARS,
    MAX_RESULTS,
    PROFILE,
    ArchiveSubject,
    ArchiveValue,
    Digest,
    Label,
    RecordRef,
    digest,
    text_digest,
)
from lychd.domain.archive.derivation import GenerationManifest, GenerationRef

SEMANTIC_POLICY = "postgres-exact-cosine@1"
Cosine = Annotated[float, Field(strict=True, ge=-1, le=1, allow_inf_nan=False)]


class SemanticRecallRequest(ArchiveValue):
    namespace_id: UUID
    request_id: UUID
    generation: GenerationRef
    query: Annotated[str, Field(strict=True, min_length=1, max_length=MAX_QUERY_CHARS, repr=False)]
    min_cosine: Cosine
    max_results: Annotated[int, Field(strict=True, ge=1, le=MAX_RESULTS)] = 5
    char_budget: Annotated[int, Field(strict=True, ge=1, le=MAX_RECALL_CHARS)] = 8_000
    run_id: UUID | None = None

    # The HTTP decoder passes strict=False, so enforce numeric identity before coercion.
    @field_validator("max_results", "char_budget", mode="before")
    @classmethod
    def integer_budget(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            msg = "Semantic result and character budgets require integers."
            raise ValueError(msg)  # noqa: TRY004 - Pydantic must project an input error, not leak TypeError.
        return value

    @field_validator("min_cosine", mode="before")
    @classmethod
    def numeric_threshold(cls, value: object) -> float | int:
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            msg = "Semantic cosine threshold requires a number."
            raise ValueError(msg)  # noqa: TRY004 - Pydantic must project an input error, not leak TypeError.
        return value

    @field_validator("query")
    @classmethod
    def valid_query(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            msg = "Semantic query must be nonblank and contain no NUL."
            raise ValueError(msg)
        value.encode("utf-8")
        return value

    def binding_digest(self, manifest: GenerationManifest, subject: ArchiveSubject, purpose: str) -> str:
        """Include exact raw query bytes and every default; repr suppression is not exclusion."""
        return digest(
            {
                "request": self.model_dump(mode="json"),
                "manifest": manifest.model_dump(mode="json"),
                "subject": subject.model_dump(mode="json"),
                "purpose": purpose,
                "retrieval_policy": SEMANTIC_POLICY,
            }
        )


class SemanticQueryExecution(ArchiveValue):
    """One local operation, never a Run or a ledger of all duplicate/failing attempts."""

    execution_id: UUID
    grant_id: UUID
    profile_digest: Digest
    operation: Literal["embed_query"] = "embed_query"
    item_id: Annotated[str, Field(strict=True, min_length=36, max_length=36)]
    input_digest: Digest
    rendered_digest: Digest
    vector_digest: Digest
    tokens: Annotated[int, Field(strict=True, ge=1, le=511)]
    receipt_digest: Digest

    @model_validator(mode="after")
    def native_receipt(self) -> SemanticQueryExecution:
        """Close the exact Core metadata whitelist, excluding local attribution identities."""
        if str(UUID(self.item_id)) != self.item_id:
            msg = "Semantic query item identity must be a canonical UUID."
            raise ValueError(msg)
        metadata = self.model_dump(
            mode="json", exclude={"execution_id", "grant_id", "profile_digest", "operation", "receipt_digest"}
        )
        expected = embedding_digest({"profile": self.profile_digest, "operation": self.operation, "items": [metadata]})
        if expected != self.receipt_digest:
            msg = "Semantic query native receipt integrity mismatch."
            raise ValueError(msg)
        return self


class SemanticPrepared(ArchiveValue):
    """Preflight snapshot fence; this is neither a retained job nor a query attempt."""

    generation_version: Annotated[int, Field(strict=True, ge=1)]


class SemanticCoverage(ArchiveValue):
    """Counts at final serialized selection, historical when replayed."""

    promoted_notes: Annotated[int, Field(strict=True, ge=0)]
    eligible_notes: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def subset(self) -> SemanticCoverage:
        if self.eligible_notes > self.promoted_notes:
            msg = "Eligible coverage cannot exceed current promoted notes."
            raise ValueError(msg)
        return self


class SemanticSelection(ArchiveValue):
    reference: RecordRef
    source_key: Label
    source_revision: Label
    source_digest: Digest
    score: Cosine
    projection_digest: Digest
    projected_chars: Annotated[int, Field(strict=True, ge=1, le=MAX_NOTE_CHARS)]
    derivation_id: UUID
    row_version: Annotated[int, Field(strict=True, ge=1)]
    vector_digest: Digest

    @model_validator(mode="after")
    def whole_note(self) -> SemanticSelection:
        if self.projection_digest != self.source_digest:
            msg = "Whole-note projection must bind the exact source bytes."
            raise ValueError(msg)
        return self


class SemanticRecallReceipt(ArchiveValue):
    """Immutable schema2 selection evidence without query, vectors or source text."""

    schema_version: Literal[2] = 2
    receipt_id: UUID
    request_id: UUID
    namespace_id: UUID
    subject: ArchiveSubject
    purpose: Label
    profile: Literal["local-public-notes@1"] = PROFILE
    retrieval_policy: Literal["postgres-exact-cosine@1"] = SEMANTIC_POLICY
    policy_revision: Label
    query_digest: Digest
    request_digest: Digest
    run_id: UUID | None
    max_results: Annotated[int, Field(strict=True, ge=1, le=MAX_RESULTS)]
    char_budget: Annotated[int, Field(strict=True, ge=1, le=MAX_RECALL_CHARS)]
    min_cosine: Cosine
    generation: GenerationRef
    generation_version: Annotated[int, Field(strict=True, ge=1)]
    query_execution: SemanticQueryExecution
    coverage: SemanticCoverage
    selected: Annotated[tuple[SemanticSelection, ...], Field(max_length=MAX_RESULTS)]
    omitted_for_budget: Annotated[int, Field(strict=True, ge=0, le=MAX_RESULTS)]
    created_at: datetime
    receipt_digest: Digest

    def computed_digest(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"receipt_digest"}))

    @model_validator(mode="after")
    def coherent_selection(self) -> SemanticRecallReceipt:
        count = len(self.selected) + self.omitted_for_budget
        order = [(-item.score, item.reference.record_id.int) for item in self.selected]
        if (
            count > self.max_results
            or count > self.coverage.eligible_notes
            or sum(item.projected_chars for item in self.selected) > self.char_budget
            or any(item.score < self.min_cosine for item in self.selected)
            or order != sorted(order)
            or len({item.reference.record_id for item in self.selected}) != len(self.selected)
            or len({item.derivation_id for item in self.selected}) != len(self.selected)
            or len({item.source_key for item in self.selected}) != len(self.selected)
            or self.query_execution.item_id != str(self.request_id)
            or self.query_execution.input_digest != self.query_digest
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            msg = "Semantic selection bounds, order or request attribution mismatch."
            raise ValueError(msg)
        if self.computed_digest() != self.receipt_digest:
            msg = "Semantic receipt integrity mismatch."
            raise ValueError(msg)
        return self


class SemanticRecallResult(ArchiveValue):
    """Winning selection plus work incurred by this caller, including a losing publisher."""

    receipt: SemanticRecallReceipt
    current_query_execution: SemanticQueryExecution | None

    @model_validator(mode="after")
    def compatible_execution(self) -> SemanticRecallResult:
        current = self.current_query_execution
        retained = self.receipt.query_execution
        if current is not None and (
            current.profile_digest != retained.profile_digest
            or current.item_id != retained.item_id
            or current.input_digest != retained.input_digest
            or current.rendered_digest != retained.rendered_digest
        ):
            msg = "Current query execution does not match the retained request."
            raise ValueError(msg)
        return self


class SemanticRecallItem(ArchiveValue):
    selection: SemanticSelection
    text: Annotated[str, Field(strict=True, min_length=1, max_length=MAX_NOTE_CHARS, repr=False)]

    @field_validator("text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            msg = "Semantic source must be nonblank and contain no NUL."
            raise ValueError(msg)
        value.encode("utf-8")
        return value

    @model_validator(mode="after")
    def exact_bytes(self) -> SemanticRecallItem:
        if (
            text_digest(self.text) != self.selection.projection_digest
            or len(self.text) != self.selection.projected_chars
        ):
            msg = "Semantic projection bytes do not match the exact selection."
            raise ValueError(msg)
        return self


class SemanticRecallProjection(ArchiveValue):
    """Transient attributed context; source bytes grant no instruction or other authority."""

    receipt: SemanticRecallReceipt
    items: Annotated[tuple[SemanticRecallItem, ...], Field(max_length=MAX_RESULTS)]

    @model_validator(mode="after")
    def exact_selection(self) -> SemanticRecallProjection:
        if tuple(item.selection for item in self.items) != self.receipt.selected:
            msg = "Semantic projection must resolve the entire exact ordered selection."
            raise ValueError(msg)
        return self
