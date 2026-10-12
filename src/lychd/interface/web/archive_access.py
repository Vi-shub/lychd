"""Explicit same-host Archive custody at the application composition boundary."""

from __future__ import annotations

from dataclasses import dataclass

from lychd.domain.archive import ArchiveAuthorization, ArchiveProfile, ArchiveRefused, ArchiveSubject
from lychd.domain.codex.sigil import Sigil


@dataclass(frozen=True)
class LocalArchivePolicy:
    """Bind one configured subject to the authenticated local bootstrap generation.

    This mapping is selected by the composition root. It is not remote IAM, a
    name-based fallback, or a wildcard-scope authorization rule.
    """

    profile: ArchiveProfile
    caller: Sigil
    derivations: bool = False

    def subject_for(self, caller: Sigil) -> ArchiveSubject:
        """Refuse persisted callers outside the exact composed bootstrap binding."""
        if caller != self.caller:
            msg = "local_subject"
            raise ArchiveRefused(msg)
        return self.profile.owner_subject

    def authorize(self, request: ArchiveAuthorization) -> bool:
        """Re-evaluate the exact local subject, namespace, policy, action and purpose."""
        if (
            request.subject != self.profile.owner_subject
            or request.owner_subject != self.profile.owner_subject
            or request.namespace_id != self.profile.namespace_id
            or request.policy_revision != self.profile.policy_revision
        ):
            return False
        if request.action in {"admit", "promote", "archive", "revoke"}:
            return request.purpose == "archive:manage"
        if self.derivations:
            purposes = {
                "generation_discover": "archive:derive:manage",
                "generation_register": "archive:derive:manage",
                "generation_retire": "archive:derive:manage",
                "derive": "archive:derive",
                "derive_status": "archive:derive:status",
            }
            if request.action in purposes:
                return request.purpose == purposes[request.action]
        return request.action in {"recall", "resolve"} and request.purpose == "archive:answer"
