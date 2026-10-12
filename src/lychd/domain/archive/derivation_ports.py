"""Explicit store and Core lease seams; neither owns a detached job or model activation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from lychd.domain.animation.embeddings import EmbeddingBinding, EmbeddingGrant, EmbeddingOperation
    from lychd.domain.archive.contracts import ArchiveSubject
    from lychd.domain.archive.derivation import (
        DerivationAttempt,
        DerivationReceipt,
        DerivationRequest,
        DerivationStatus,
        GenerationManifest,
        GenerationRef,
        GenerationStatus,
    )
    from lychd.domain.archive.ports import AuthorizationCheck


class EmbeddingLeaser(Protocol):
    def lease_embedding(
        self, binding: EmbeddingBinding, *, operation: EmbeddingOperation, run_id: str, priority: int = 50
    ) -> AbstractAsyncContextManager[EmbeddingGrant]: ...


class DerivationStore(Protocol):
    async def register(
        self, manifest: GenerationManifest, reference: GenerationRef, check: AuthorizationCheck
    ) -> GenerationStatus: ...

    async def retire(
        self, manifest: GenerationManifest, reference: GenerationRef, expected_version: int, check: AuthorizationCheck
    ) -> GenerationStatus: ...

    async def prepare(
        self, manifest: GenerationManifest, request: DerivationRequest, check: AuthorizationCheck
    ) -> DerivationStatus: ...

    async def inspect(
        self, manifest: GenerationManifest, request: DerivationRequest, check: AuthorizationCheck
    ) -> DerivationStatus: ...

    async def claim(
        self,
        manifest: GenerationManifest,
        request: DerivationRequest,
        expected: DerivationStatus,
        subject: ArchiveSubject,
        purpose: str,
        check: AuthorizationCheck,
    ) -> DerivationAttempt: ...

    async def complete(
        self,
        manifest: GenerationManifest,
        attempt: DerivationAttempt,
        receipt: DerivationReceipt,
        vector: tuple[float, ...],
        check: AuthorizationCheck,
    ) -> DerivationStatus: ...

    async def fail(
        self, manifest: GenerationManifest, attempt: DerivationAttempt, check: AuthorizationCheck
    ) -> None: ...
