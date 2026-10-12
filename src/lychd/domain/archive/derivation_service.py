"""Explicit synchronous derivation: claim, disclose, close the lease, then CAS publish."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING
from uuid import UUID

from lychd.domain.animation.embeddings import (
    EmbeddingBatch,
    EmbeddingInput,
    canonical_float32_vector,
    embedding_digest,
    embedding_vector_digest,
)
from lychd.domain.archive.authorization import authorization_check
from lychd.domain.archive.contracts import ArchiveRefused, ArchiveSubject, digest, text_digest
from lychd.domain.archive.derivation import (
    VECTOR_DIMENSION,
    DerivationAttempt,
    DerivationReceipt,
    DerivationRequest,
    DerivationStatus,
    GenerationManifest,
    GenerationRef,
    GenerationStatus,
)

if TYPE_CHECKING:
    from lychd.domain.archive.derivation_ports import DerivationStore, EmbeddingLeaser
    from lychd.domain.archive.ports import ArchiveAuthorizer


SHA256_HEX_LENGTH = 64


class ArchiveDerivationService:
    """One configured generation; registration and execution never happen at startup."""

    def __init__(
        self,
        *,
        generation_id: UUID,
        manifest: GenerationManifest,
        store: DerivationStore,
        authorizer: ArchiveAuthorizer,
        dispatcher: EmbeddingLeaser,
    ) -> None:
        """Bind one immutable configured generation and its ordinary Core lease owner."""
        self.manifest = GenerationManifest.model_validate(manifest.model_dump(mode="json"))
        self.reference = GenerationRef(generation_id=generation_id, manifest_digest=self.manifest.digest)
        self._store = store
        self._authorizer = authorizer
        self._dispatcher = dispatcher

    def _reference(self, reference: GenerationRef) -> None:
        if reference != self.reference:
            msg = "generation_binding"
            raise ArchiveRefused(msg)

    async def discover(self, subject: ArchiveSubject, *, purpose: str) -> GenerationRef:
        """Authorize configured identity only; make no storage or runtime observation."""
        check = authorization_check(self.manifest.archive, self._authorizer, subject, "generation_discover", purpose)
        check(self.reference.generation_id, digest(self.reference))
        return self.reference

    async def register(self, subject: ArchiveSubject, reference: GenerationRef, *, purpose: str) -> GenerationStatus:
        check = authorization_check(self.manifest.archive, self._authorizer, subject, "generation_register", purpose)
        check(reference.generation_id, digest(reference))
        self._reference(reference)
        result = await self._store.register(self.manifest, reference, check)
        check(reference.generation_id, digest(result))
        return result

    async def retire(
        self,
        subject: ArchiveSubject,
        reference: GenerationRef,
        *,
        expected_version: int,
        purpose: str,
    ) -> GenerationStatus:
        check = authorization_check(self.manifest.archive, self._authorizer, subject, "generation_retire", purpose)
        check(
            reference.generation_id,
            digest({"reference": reference.model_dump(mode="json"), "version": expected_version}),
        )
        self._reference(reference)
        if expected_version < 1:
            msg = "version"
            raise ArchiveRefused(msg)
        result = await self._store.retire(self.manifest, reference, expected_version, check)
        check(reference.generation_id, digest(result))
        return result

    async def inspect(self, subject: ArchiveSubject, request: DerivationRequest, *, purpose: str) -> DerivationStatus:
        check = authorization_check(self.manifest.archive, self._authorizer, subject, "derive_status", purpose)
        check(request.source.record_id, digest(request))
        self._reference(request.generation)
        result = await self._store.inspect(self.manifest, request, check)
        check(result.derivation_id, digest(result))
        return result

    def _result(self, batch: EmbeddingBatch, attempt: DerivationAttempt) -> tuple[DerivationReceipt, tuple[float, ...]]:
        profile = self.manifest.embedding
        if batch.profile_digest != profile.digest or batch.operation != "embed_document" or len(batch.items) != 1:
            msg = "embedding_result"
            raise ArchiveRefused(msg)
        item = batch.items[0]
        vector = canonical_float32_vector(item.vector)
        if (
            item.item_id != str(attempt.status.derivation_id)
            or item.input_digest != attempt.status.source_digest
            or item.rendered_digest != text_digest(profile.document_prefix + attempt.text)
            or len(vector) != VECTOR_DIMENSION
            or abs(math.sqrt(sum(value * value for value in vector)) - 1) > profile.normalization_tolerance
            or item.vector_digest != embedding_vector_digest(vector)
            or type(item.tokens) is not int
            or not 1 <= item.tokens <= profile.max_input_tokens
        ):
            msg = "embedding_result"
            raise ArchiveRefused(msg)
        if len(batch.receipt_digest) != SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in batch.receipt_digest
        ):
            msg = "embedding_receipt"
            raise ArchiveRefused(msg)
        receipt = DerivationReceipt(
            profile_digest=batch.profile_digest,
            item_id=item.item_id,
            input_digest=item.input_digest,
            rendered_digest=item.rendered_digest,
            vector_digest=item.vector_digest,
            tokens=item.tokens,
            receipt_digest=batch.receipt_digest,
        )
        expected = embedding_digest(
            {
                "profile": batch.profile_digest,
                "operation": batch.operation,
                "items": [receipt.model_dump(mode="json", exclude={"profile_digest", "operation", "receipt_digest"})],
            }
        )
        if expected != batch.receipt_digest:
            msg = "embedding_receipt"
            raise ArchiveRefused(msg)
        return receipt, vector

    def _input(self, attempt: DerivationAttempt) -> EmbeddingInput:
        profile = self.manifest.embedding
        if len((profile.document_prefix + attempt.text).encode()) > profile.max_input_bytes:
            msg = "embedding_input_size"
            raise ArchiveRefused(msg)
        return EmbeddingInput(str(attempt.status.derivation_id), attempt.status.source_digest, attempt.text)

    async def derive(self, subject: ArchiveSubject, request: DerivationRequest, *, purpose: str) -> DerivationStatus:
        check = authorization_check(self.manifest.archive, self._authorizer, subject, "derive", purpose)
        check(request.source.record_id, digest(request))
        self._reference(request.generation)
        prepared = await self._store.prepare(self.manifest, request, check)
        check(prepared.derivation_id, digest(prepared))
        if prepared.state == "complete":
            return prepared
        attempt: DerivationAttempt | None = None
        try:
            async with self._dispatcher.lease_embedding(
                self.manifest.binding,
                operation="embed_document",
                run_id=str(request.run_id or prepared.derivation_id),
                priority=50,
            ) as grant:
                check(prepared.derivation_id, digest(prepared))
                attempt = await self._store.claim(self.manifest, request, prepared, subject, purpose, check)
                # Session/commit cleanup awaits have ended: reauthorize at source handoff.
                check(attempt.status.derivation_id, attempt.binding_digest)
                current_attempt = attempt
                batch = await grant.driver.embed(
                    (self._input(attempt),),
                    authorize=lambda: check(current_attempt.status.derivation_id, current_attempt.binding_digest),
                )
                receipt, vector = self._result(batch, attempt)
            # Both transport and counted lease are closed before any durable result write.
            check(attempt.status.derivation_id, attempt.binding_digest)
            result = await self._store.complete(self.manifest, attempt, receipt, vector, check)
        except Exception as error:
            if attempt is not None:
                try:
                    check(attempt.status.derivation_id, attempt.binding_digest)
                    await self._store.fail(self.manifest, attempt, check)
                except Exception:  # noqa: BLE001 - stale/denied cleanup must not replace the original refusal
                    error.add_note("Derivation failure persistence was refused; inspect retained attempt status.")
            raise
        check(result.derivation_id, digest(result))
        return result
