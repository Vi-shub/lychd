"""Complete plain-text review from immutable command bytes and current eligibility."""

from __future__ import annotations

import json
from typing import Literal

from lychd.compositions.blockworld.ports import PlacementAuthorityPort, PlacementStore
from lychd.compositions.blockworld.schemas import (
    AdmittedPlacementV1,
    PlacementIntegrityError,
    PreparedPlacementV1,
    canonical_bytes,
    placement_digest,
)
from lychd.domain.codex.schemas import ConsentView, NativeConsentBinding
from lychd.domain.codex.sigil import Sigil
from lychd.domain.web.consent_reviews import (
    ConsentReviewForbiddenError,
    ConsentReviewUnavailableError,
    NativeReviewDocument,
    NativeReviewResolution,
    ReviewFact,
)


def render_review(command: PreparedPlacementV1) -> NativeReviewDocument:
    canonical_bytes(command)

    def compact(value: tuple[int, int, int]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    c = command
    properties = (
        "\n".join(f"{key}={value}" for key, value in sorted(c.block_state.properties.items())) or "(no properties)"
    )
    values = (
        ("mission", "Mission", c.mission_id),
        ("action", "Action", c.action_id),
        ("principal", "Principal", f"{c.principal.name}\nScopes: {', '.join(c.principal.scopes)}"),
        ("pattern", "Pattern", f"{c.pattern.key}@{c.pattern.revision}\nManifest: {c.pattern.manifest_digest}"),
        ("adapter", "Adapter", f"{c.adapter.key}@{c.adapter.revision}"),
        ("server", "Server", f"{c.server_id}\nMode: {c.mode}"),
        ("world", "World", f"{c.world_id}\nEpoch: {c.world_epoch}"),
        ("bot", "Bot", c.bot_id),
        ("blueprint", "Blueprint", f"{c.blueprint_id}\nDigest: {c.blueprint_digest}"),
        ("plot", "Plot", f"{c.plot.id}\nInclusive bounds: {compact(c.plot.min)} to {compact(c.plot.max)}"),
        ("target", "Target", compact(c.target)),
        ("block_state", "Full block state", f"{c.block_state.name}\n{properties}"),
        (
            "inventory",
            "Inventory",
            f"{c.inventory_change.slot}: {c.inventory_change.item}\n{c.inventory_change.before_count} -> {c.inventory_change.after_count} (delta {c.inventory_change.delta})\nGeneration: {c.inventory_generation}",
        ),
        ("lease", "Lease", f"{c.lease.id}\nGeneration: {c.lease.generation}\nExpires: {c.lease.expires_at}"),
        (
            "policy",
            "Policy",
            f"{c.mission_policy.id}\nMission generation: {c.mission_policy.generation}\nAuthority generation: {c.authority_generation}",
        ),
        ("cursor", "Cursor", f"{c.expected_cursor} -> {c.postcondition.cursor}"),
        ("limits", "Limits", f"Deadline: {c.deadline}\nPlacements: {c.placement_budget}"),
        (
            "proof",
            "Required completion proof",
            "Matching response and authoritative event for the same action, preparation, server, world epoch, bot, target, block state, inventory and cursor.",
        ),
    )
    return NativeReviewDocument(
        title="Place one block on the admitted plot",
        facts=tuple(ReviewFact(key=key, label=label, value=value) for key, label, value in values),
    )


def prepare_binding(
    command: PreparedPlacementV1, admission: AdmittedPlacementV1
) -> tuple[NativeConsentBinding, NativeReviewDocument]:
    review = render_review(command)
    return NativeConsentBinding(
        owner="blockworld.inhabitant",
        action_id=command.action_id,
        preparation_digest=placement_digest(canonical_bytes(command)),
        review_profile="blockworld.placement",
        review_profile_revision="2",
        review_producer="blockworld.placement-review",
        review_producer_revision="1",
        audience_digest=admission.audience_digest,
        review_digest=review.digest,
    ), review


class BlockworldConsentReviewProducer:
    def __init__(self, store: PlacementStore, authority: PlacementAuthorityPort) -> None:
        """Bind retained custody and read-only current reviewer facts."""
        self.store, self.authority = store, authority

    async def resolve(
        self, *, consent: ConsentView, reviewer: Sigil, purpose: Literal["view", "decide"]
    ) -> NativeReviewResolution:
        from lychd.compositions.blockworld.services import validate_action

        if consent.native_binding is None:
            msg = "Placement review binding is unavailable."
            raise ConsentReviewUnavailableError(msg)
        action = await self.store.read_action(consent.native_binding.action_id)
        mission = await self.store.load_for_run(action.run_id)
        command = validate_action(action, mission)
        if (
            consent.id != action.consent_id
            or consent.run_id != action.run_id
            or consent.native_binding != action.native_binding
        ):
            msg = "Placement review binding differs from retained custody."
            raise ConsentReviewUnavailableError(msg)
        review = render_review(command)
        if review.canonical_bytes() != action.review_bytes:
            msg = "Retained review differs from frozen command."
            raise PlacementIntegrityError(msg)
        access = await self.authority.review_access(
            action.action_id, action.native_binding.audience_digest, reviewer, purpose
        )
        canonical_bytes(access)
        if (
            not access.disclose_complete
            or access.audience_digest != action.native_binding.audience_digest
            or (purpose == "decide" and not access.judge)
        ):
            msg = "Current placement disclosure or judgment is unavailable."
            raise ConsentReviewForbiddenError(msg)
        return NativeReviewResolution(binding=action.native_binding, document=review, decision_allowed=access.judge)
