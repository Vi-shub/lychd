"""Usable process-local privacy preparation and exact-byte seal verification.

This authority attests only to the independently checked bounded fixture profile.
It does not admit egress, count transmissions, hold a pseudonym map, persist across
restart, grant consent, or qualify an external recipient's actual retention.
"""

from __future__ import annotations

import hmac
import secrets
from uuid import uuid4

from lychd.domain.privacy.censor import censor
from lychd.domain.privacy.contracts import (
    CutBinding,
    CutVerification,
    DisclosurePlan,
    PreparationPolicy,
    PreparedCut,
    PrivacyCut,
    PrivacyRefused,
    PublicPrivacyEvent,
    TransformationReceipt,
    WholeProjection,
    bytes_digest,
    canonical_bytes,
    digest,
    manifest,
    semantic_invariants,
    validate_source,
)
from lychd.domain.privacy.verification import checked, verify_candidate


def make_plan(
    projection: WholeProjection,
    binding: CutBinding,
    policy: PreparationPolicy,
    *,
    expires_at: int,
    max_disclosures: int = 1,
) -> DisclosurePlan:
    """Bind a complete minimal fixture to one exact actor, purpose, target and policy."""
    projection, binding, policy = checked(projection), checked(binding), checked(policy)
    validate_source(projection, policy)
    return DisclosurePlan(
        binding=binding,
        source_digest=digest(projection),
        source_manifest_digest=digest(manifest(projection)),
        influence_label_digest=digest(projection.influence()),
        restricted_local_ref=uuid4(),
        semantic_invariants_digest=digest(semantic_invariants(projection)),
        policy_digest=digest(policy),
        max_disclosures=max_disclosures,
        expires_at=expires_at,
    )


class CutAuthority:
    """One in-memory issuer/verifier; another process cannot accept its seals.

    The trusted host must retain this instance and the policy. A caller-created
    instance or a serialized Cut is never admission authority for an Egress Gate.
    """

    def __init__(self) -> None:
        """Create one unexported process-local seal key."""
        self._key = secrets.token_bytes(32)

    def seal(
        self,
        projection: WholeProjection,
        plan: DisclosurePlan,
        candidate: bytes,
        receipts: tuple[TransformationReceipt, ...],
        verification: CutVerification,
        policy: PreparationPolicy,
        *,
        now: int,
    ) -> PrivacyCut:
        """Recheck local evidence before authenticating one exact irreversible Cut."""
        verified = verify_candidate(projection, plan, candidate, receipts, policy, now=now)
        if checked(verification) != verified:
            msg = "verifier-disagreement"
            raise PrivacyRefused(msg)
        cut = PrivacyCut(
            cut_id=uuid4(),
            plan=checked(plan),
            verification=verified,
            candidate_digest=bytes_digest(candidate),
            receipt_chain_digest=digest(receipts),
            terminal_receipt_digest=digest(receipts[-1]),
            issued_at=now,
            expires_at=plan.expires_at,
            seal="0" * 64,
        )
        return cut.model_copy(update={"seal": self._signature(cut)})

    def verify_seal(
        self,
        cut: PrivacyCut,
        candidate: bytes,
        plan: DisclosurePlan,
        binding: CutBinding,
        policy: PreparationPolicy,
        *,
        now: int,
    ) -> None:
        """Refuse changed bytes, identity, policy, target, evidence or expired seals.

        A successful check supplies preparation evidence only. The security owner
        must still make a fresh byte-time decision and atomically consume use.
        """
        cut, plan, binding, policy = checked(cut), checked(plan), checked(binding), checked(policy)
        if not hmac.compare_digest(cut.seal, self._signature(cut)):
            msg = "cut-seal"
            raise PrivacyRefused(msg)
        if not cut.issued_at <= now < cut.expires_at:
            msg = "expiry"
            raise PrivacyRefused(msg)
        if (
            cut.plan != plan
            or plan.binding != binding
            or plan.policy_digest != digest(policy)
            or cut.candidate_digest != bytes_digest(candidate)
        ):
            msg = "cut-binding"
            raise PrivacyRefused(msg)

    def _signature(self, cut: PrivacyCut) -> str:
        payload = b"lychd:closed-diagnostic-cut-seal@1\x00" + canonical_bytes(cut.model_dump(exclude={"seal"}))
        return hmac.digest(self._key, payload, "sha256").hex()


def prepare_cut(
    projection: WholeProjection,
    plan: DisclosurePlan,
    policy: PreparationPolicy,
    authority: CutAuthority,
    *,
    now: int,
) -> PreparedCut:
    """Run deterministic Censor, independent verifier and exact-bound sealing."""
    projection, plan, policy = checked(projection), checked(plan), checked(policy)
    candidate, receipts = censor(projection, plan, policy, now=now)
    verification = verify_candidate(projection, plan, candidate, receipts, policy, now=now)
    cut = authority.seal(projection, plan, candidate, receipts, verification, policy, now=now)
    return PreparedCut(candidate=candidate, cut=cut, receipts=receipts, verification=verification)


def public_event() -> PublicPrivacyEvent:
    """Return an unlinkable event handle without exposing local evidence digests."""
    return PublicPrivacyEvent(event_id=uuid4())
