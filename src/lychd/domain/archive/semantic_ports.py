"""Serialized semantic persistence seam; no model work or durable query-attempt ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from lychd.domain.archive.contracts import ArchiveSubject
    from lychd.domain.archive.derivation import GenerationManifest
    from lychd.domain.archive.ports import AuthorizationCheck
    from lychd.domain.archive.semantic import (
        SemanticPrepared,
        SemanticQueryExecution,
        SemanticRecallProjection,
        SemanticRecallReceipt,
        SemanticRecallRequest,
    )


class SemanticStore(Protocol):
    """Own namespace/current-generation/source fences and retained row integrity.

    Preflight releases its transaction before inference. Complete runs only after
    lease closure: under the namespace lock, check current authority and a matching
    completed duplicate first. Only an unpublished request requires the same active
    generation/version and current eligible rows before exact rank/limit and whole-note
    no-backfill budget fitting. Coverage describes that final serialized snapshot.
    Inspect is historical metadata; resolve checks all current exact source/vector fences.
    """

    async def preflight(
        self,
        manifest: GenerationManifest,
        subject: ArchiveSubject,
        request: SemanticRecallRequest,
        purpose: str,
        check: AuthorizationCheck,
    ) -> SemanticPrepared | SemanticRecallReceipt: ...

    async def complete(
        self,
        manifest: GenerationManifest,
        subject: ArchiveSubject,
        request: SemanticRecallRequest,
        purpose: str,
        prepared: SemanticPrepared,
        execution: SemanticQueryExecution,
        vector: tuple[float, ...],
        check: AuthorizationCheck,
    ) -> SemanticRecallReceipt: ...

    async def inspect(
        self, manifest: GenerationManifest, request: SemanticRecallRequest, check: AuthorizationCheck
    ) -> SemanticRecallReceipt | None: ...

    async def resolve(
        self,
        manifest: GenerationManifest,
        subject: ArchiveSubject,
        receipt_id: UUID,
        receipt_digest: str,
        purpose: str,
        check: AuthorizationCheck,
    ) -> SemanticRecallProjection: ...
