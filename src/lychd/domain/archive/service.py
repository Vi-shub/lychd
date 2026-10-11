"""Explicitly assembled authority boundary for one public-note namespace."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from lychd.domain.archive.contracts import (
    ArchiveAuthorization,
    ArchiveProfile,
    ArchiveRefused,
    ArchiveSubject,
    Lifecycle,
    NoteProposal,
    NoteRecord,
    RecallProjection,
    RecallReceipt,
    RecallRequest,
    digest,
)

if TYPE_CHECKING:
    from uuid import UUID

    from lychd.domain.archive.ports import ArchiveAuthorizer, ArchiveStore, AuthorizationCheck


class ArchiveService:
    """No default subject, permissive policy, or process-global repository exists."""

    def __init__(self, *, profile: ArchiveProfile, store: ArchiveStore, authorizer: ArchiveAuthorizer) -> None:
        """Require explicit namespace, store and current authorizer composition."""
        self.profile = profile
        self._store = store
        self._authorizer = authorizer

    def _check(
        self,
        subject: ArchiveSubject,
        namespace_id: UUID,
        action: Literal["admit", "promote", "archive", "revoke", "recall", "resolve"],
        purpose: str,
    ) -> AuthorizationCheck:
        if namespace_id != self.profile.namespace_id:
            msg = "namespace"
            raise ArchiveRefused(msg)

        def check(object_id: UUID | None, binding_digest: str) -> None:
            request = ArchiveAuthorization(
                subject=subject,
                namespace_id=namespace_id,
                owner_subject=self.profile.owner_subject,
                policy_revision=self.profile.policy_revision,
                action=action,
                purpose=purpose,
                object_id=object_id,
                binding_digest=binding_digest,
            )
            if self._authorizer.authorize(request) is not True:
                msg = "authority"
                raise ArchiveRefused(msg)

        return check

    async def admit(self, subject: ArchiveSubject, proposal: NoteProposal, *, purpose: str) -> NoteRecord:
        check = self._check(subject, proposal.namespace_id, "admit", purpose)
        check(proposal.request_id, digest(proposal))
        record = await self._store.admit(self.profile, proposal, check)
        check(record.reference.record_id, digest(record))
        return record

    async def _transition(
        self,
        subject: ArchiveSubject,
        record_id: UUID,
        expected_version: int,
        target: Lifecycle,
        purpose: str,
    ) -> NoteRecord:
        if expected_version < 1:
            msg = "version"
            raise ArchiveRefused(msg)
        actions: dict[Lifecycle, Literal["promote", "archive", "revoke"]] = {
            Lifecycle.PROMOTED: "promote",
            Lifecycle.ARCHIVED: "archive",
            Lifecycle.REVOKED: "revoke",
        }
        check = self._check(subject, self.profile.namespace_id, actions[target], purpose)
        check(record_id, digest({"record_id": str(record_id), "expected_version": expected_version, "target": target}))
        record = await self._store.transition(self.profile, record_id, expected_version, target, check)
        check(record.reference.record_id, digest(record))
        return record

    async def promote(
        self, subject: ArchiveSubject, record_id: UUID, *, expected_version: int, purpose: str
    ) -> NoteRecord:
        return await self._transition(subject, record_id, expected_version, Lifecycle.PROMOTED, purpose)

    async def archive(
        self, subject: ArchiveSubject, record_id: UUID, *, expected_version: int, purpose: str
    ) -> NoteRecord:
        return await self._transition(subject, record_id, expected_version, Lifecycle.ARCHIVED, purpose)

    async def revoke(
        self, subject: ArchiveSubject, record_id: UUID, *, expected_version: int, purpose: str
    ) -> NoteRecord:
        return await self._transition(subject, record_id, expected_version, Lifecycle.REVOKED, purpose)

    async def recall(self, subject: ArchiveSubject, request: RecallRequest, *, purpose: str) -> RecallReceipt:
        check = self._check(subject, request.namespace_id, "recall", purpose)
        check(request.request_id, digest(request))
        receipt = await self._store.recall(self.profile, subject, request, purpose, check)
        check(receipt.receipt_id, receipt.receipt_digest)
        return receipt

    async def resolve_receipt(
        self, subject: ArchiveSubject, receipt_id: UUID, receipt_digest: str, *, purpose: str
    ) -> RecallProjection:
        check = self._check(subject, self.profile.namespace_id, "resolve", purpose)
        check(receipt_id, receipt_digest)
        projection = await self._store.resolve(self.profile, subject, receipt_id, receipt_digest, purpose, check)
        # Transaction exit and connection cleanup may await after the locked check.
        # Recheck current authority at the actual content handoff, without another await.
        check(receipt_id, receipt_digest)
        return projection
