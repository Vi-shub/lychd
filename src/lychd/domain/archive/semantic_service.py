"""Explicit exact semantic query: preflight, one counted Core lease, close, publish."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lychd.domain.animation.embeddings import (
    EmbeddingBatch,
    EmbeddingGrant,
    EmbeddingInput,
    canonical_float32_vector,
    embedding_vector_digest,
)
from lychd.domain.archive.authorization import authorization_check
from lychd.domain.archive.contracts import ArchiveRefused, ArchiveSubject, text_digest
from lychd.domain.archive.derivation import VECTOR_DIMENSION, GenerationManifest, GenerationRef
from lychd.domain.archive.semantic import (
    SEMANTIC_POLICY,
    SemanticPrepared,
    SemanticQueryExecution,
    SemanticRecallProjection,
    SemanticRecallReceipt,
    SemanticRecallRequest,
    SemanticRecallResult,
)

if TYPE_CHECKING:
    from lychd.domain.archive.derivation_ports import EmbeddingLeaser
    from lychd.domain.archive.ports import ArchiveAuthorizer
    from lychd.domain.archive.semantic_ports import SemanticStore


class ArchiveSemanticService:
    """One immutable configured binding; no startup registration, activation or automatic retry.

    The host leaser must share ordinary lease accounting without a Run-event writer
    for these caller-supplied correlation ids. An execution id attributes this local
    operation only; it is not a fabricated durable Run identity.
    """

    def __init__(
        self,
        *,
        generation_id: UUID,
        manifest: GenerationManifest,
        store: SemanticStore,
        authorizer: ArchiveAuthorizer,
        dispatcher: EmbeddingLeaser,
    ) -> None:
        """Bind one configured generation to explicit persistence, policy and lease owners."""
        self.manifest = GenerationManifest.model_validate(manifest.model_dump(mode="json"))
        self.reference = GenerationRef(generation_id=generation_id, manifest_digest=self.manifest.digest)
        self._store = store
        self._authorizer = authorizer
        self._dispatcher = dispatcher

    def _request(self, request: SemanticRecallRequest) -> None:
        if request.namespace_id != self.manifest.archive.namespace_id:
            msg = "namespace"
            raise ArchiveRefused(msg)
        if request.generation != self.reference:
            msg = "generation_binding"
            raise ArchiveRefused(msg)

    def _receipt(
        self, receipt: SemanticRecallReceipt, request: SemanticRecallRequest, subject: ArchiveSubject, purpose: str
    ) -> SemanticRecallReceipt:
        try:
            validated = SemanticRecallReceipt.model_validate(receipt.model_dump(mode="json"))
        except ValueError:
            msg = "receipt_integrity"
            raise ArchiveRefused(msg) from None
        profile = self.manifest.archive
        execution = validated.query_execution
        if (
            validated.namespace_id != request.namespace_id
            or validated.request_id != request.request_id
            or validated.run_id != request.run_id
            or validated.subject != subject
            or validated.purpose != purpose
            or validated.profile != profile.profile
            or validated.policy_revision != profile.policy_revision
            or validated.retrieval_policy != SEMANTIC_POLICY
            or validated.generation != self.reference
            or validated.query_digest != text_digest(request.query)
            or validated.request_digest != request.binding_digest(self.manifest, subject, purpose)
            or validated.max_results != request.max_results
            or validated.char_budget != request.char_budget
            or validated.min_cosine != request.min_cosine
            or execution.profile_digest != self.manifest.embedding.digest
            or execution.rendered_digest != text_digest(self.manifest.embedding.query_prefix + request.query)
            or execution.tokens > self.manifest.embedding.max_input_tokens
        ):
            msg = "receipt_binding"
            raise ArchiveRefused(msg)
        return validated

    def _grant(self, grant: EmbeddingGrant) -> UUID:
        if (
            grant.binding != self.manifest.binding
            or grant.operation != "embed_query"
            or grant.spec.key != self.manifest.binding.capability_key
            or grant.spec.embedding_profile != self.manifest.embedding
        ):
            msg = "embedding_grant"
            raise ArchiveRefused(msg)
        try:
            return UUID(grant.lease.grant_id)
        except (ValueError, TypeError, AttributeError):
            msg = "embedding_grant"
            raise ArchiveRefused(msg) from None

    def _result(
        self, batch: EmbeddingBatch, request: SemanticRecallRequest, execution_id: UUID, grant_id: UUID
    ) -> tuple[SemanticQueryExecution, tuple[float, ...]]:
        profile = self.manifest.embedding
        if batch.profile_digest != profile.digest or batch.operation != "embed_query" or len(batch.items) != 1:
            msg = "embedding_result"
            raise ArchiveRefused(msg)
        item = batch.items[0]
        vector = canonical_float32_vector(item.vector)
        if (
            item.item_id != str(request.request_id)
            or item.input_digest != text_digest(request.query)
            or item.rendered_digest != text_digest(profile.query_prefix + request.query)
            or len(vector) != VECTOR_DIMENSION
            or tuple(item.vector) != vector
            or abs(math.sqrt(sum(value * value for value in vector)) - 1) > profile.normalization_tolerance
            or item.vector_digest != embedding_vector_digest(vector)
            or type(item.tokens) is not int
            or not 1 <= item.tokens <= profile.max_input_tokens
        ):
            msg = "embedding_result"
            raise ArchiveRefused(msg)
        try:
            execution = SemanticQueryExecution(
                execution_id=execution_id,
                grant_id=grant_id,
                profile_digest=batch.profile_digest,
                operation=batch.operation,
                item_id=item.item_id,
                input_digest=item.input_digest,
                rendered_digest=item.rendered_digest,
                vector_digest=item.vector_digest,
                tokens=item.tokens,
                receipt_digest=batch.receipt_digest,
            )
        except ValueError:
            msg = "embedding_receipt"
            raise ArchiveRefused(msg) from None
        return execution, vector

    def _input(self, request: SemanticRecallRequest) -> EmbeddingInput:
        if (
            len((self.manifest.embedding.query_prefix + request.query).encode("utf-8"))
            > self.manifest.embedding.max_input_bytes
        ):
            msg = "embedding_input_size"
            raise ArchiveRefused(msg)
        # Core owns prefix rendering. This is the exact unmodified original query.
        return EmbeddingInput(str(request.request_id), text_digest(request.query), request.query)

    async def recall(
        self, subject: ArchiveSubject, request: SemanticRecallRequest, *, purpose: str
    ) -> SemanticRecallResult:
        self._request(request)
        check = authorization_check(self.manifest.archive, self._authorizer, subject, "semantic_recall", purpose)
        binding = request.binding_digest(self.manifest, subject, purpose)
        check(request.request_id, binding)
        prepared = await self._store.preflight(self.manifest, subject, request, purpose, check)
        check(request.request_id, binding)
        if isinstance(prepared, SemanticRecallReceipt):
            receipt = self._receipt(prepared, request, subject, purpose)
            check(receipt.receipt_id, receipt.receipt_digest)
            return SemanticRecallResult(receipt=receipt, current_query_execution=None)
        prepared = SemanticPrepared.model_validate(prepared.model_dump(mode="json"))
        query_input = self._input(request)
        execution_id = uuid4()
        async with self._dispatcher.lease_embedding(
            self.manifest.binding,
            operation="embed_query",
            run_id=str(request.run_id or execution_id),
            priority=50,
        ) as grant:
            check(request.request_id, binding)
            grant_id = self._grant(grant)
            check(request.request_id, binding)
            batch = await grant.driver.embed((query_input,), authorize=lambda: check(request.request_id, binding))
            check(request.request_id, binding)
            execution, vector = self._result(batch, request, execution_id, grant_id)
        # No inference or driver/counted-lease cleanup remains active at publication.
        check(request.request_id, binding)
        retained = await self._store.complete(
            self.manifest, subject, request, purpose, prepared, execution, vector, check
        )
        check(request.request_id, binding)
        receipt = self._receipt(retained, request, subject, purpose)
        check(receipt.receipt_id, receipt.receipt_digest)
        return SemanticRecallResult(receipt=receipt, current_query_execution=execution)

    async def inspect_recall(
        self,
        subject: ArchiveSubject,
        expected: SemanticRecallRequest,
        *,
        recall_purpose: str,
        purpose: str,
    ) -> SemanticRecallReceipt | None:
        """Inspect historical selection under current management authority without source reads."""
        self._request(expected)
        check = authorization_check(self.manifest.archive, self._authorizer, subject, "inspect", purpose)
        binding = expected.binding_digest(self.manifest, subject, recall_purpose)
        check(expected.request_id, binding)
        retained = await self._store.inspect(self.manifest, expected, check)
        check(expected.request_id, binding)
        if retained is None:
            return None
        receipt = self._receipt(retained, expected, subject, recall_purpose)
        check(receipt.receipt_id, receipt.receipt_digest)
        return receipt

    async def resolve_receipt(
        self, subject: ArchiveSubject, receipt_id: UUID, receipt_digest: str, *, purpose: str
    ) -> SemanticRecallProjection:
        """Resolve all exact current fences in storage, then recheck after session cleanup."""
        check = authorization_check(self.manifest.archive, self._authorizer, subject, "semantic_resolve", purpose)
        check(receipt_id, receipt_digest)
        projection = await self._store.resolve(self.manifest, subject, receipt_id, receipt_digest, purpose, check)
        check(receipt_id, receipt_digest)
        try:
            projection = SemanticRecallProjection.model_validate(projection.model_dump(mode="json"))
        except ValueError:
            msg = "receipt_integrity"
            raise ArchiveRefused(msg) from None
        receipt = projection.receipt
        if (
            receipt.receipt_id != receipt_id
            or receipt.receipt_digest != receipt_digest
            or receipt.namespace_id != self.manifest.archive.namespace_id
            or receipt.generation != self.reference
            or receipt.profile != self.manifest.archive.profile
            or receipt.policy_revision != self.manifest.archive.policy_revision
            or receipt.subject != subject
            or receipt.purpose != purpose
            or receipt.query_execution.profile_digest != self.manifest.embedding.digest
            or receipt.query_execution.tokens > self.manifest.embedding.max_input_tokens
        ):
            msg = "receipt_binding"
            raise ArchiveRefused(msg)
        check(receipt_id, receipt_digest)
        return projection
