"""Shared current policy check for lexical, derivation and semantic operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lychd.domain.archive.contracts import ArchiveAuthorization, ArchiveRefused

if TYPE_CHECKING:
    from uuid import UUID

    from lychd.domain.archive.contracts import ArchiveAction, ArchiveProfile, ArchiveSubject
    from lychd.domain.archive.ports import ArchiveAuthorizer, AuthorizationCheck


def authorization_check(
    profile: ArchiveProfile,
    authorizer: ArchiveAuthorizer,
    subject: ArchiveSubject,
    action: ArchiveAction,
    purpose: str,
) -> AuthorizationCheck:
    """Build a decision closure, never a cached authorization result."""

    def check(object_id: UUID | None, binding_digest: str) -> None:
        request = ArchiveAuthorization(
            subject=subject,
            namespace_id=profile.namespace_id,
            owner_subject=profile.owner_subject,
            policy_revision=profile.policy_revision,
            action=action,
            purpose=purpose,
            object_id=object_id,
            binding_digest=binding_digest,
        )
        if authorizer.authorize(request) is not True:
            msg = "authority"
            raise ArchiveRefused(msg)

    return check
