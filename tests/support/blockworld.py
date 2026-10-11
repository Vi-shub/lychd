"""Portable Blockworld storage and independently frozen placement literals.

The real RunEngine, Graph, native review/HTTP path and owned pure decisions execute.
Only storage, current external facts and the inert world port are substituted.
This does not prove PostgreSQL atomicity, process durability, or a live game.
Frozen r2 fixture: e73428dd4d4bef636cb50914a9a1b40329a0284228f38b1ca671333f2e460eb1.
"""

# pyright: reportPrivateUsage=false
# ruff: noqa: INP001 - bounded test support uses a namespace package
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import httpx
from litestar import Litestar
from litestar.connection import ASGIConnection
from litestar.datastructures import State
from litestar.middleware import DefineMiddleware
from litestar.middleware.authentication import AuthenticationResult
from pydantic import TypeAdapter

from lychd.agents.the_first_one import default_forge
from lychd.agents.workflows import BuiltinWorkflowRegistry
from lychd.domain.codex.ledger import InMemoryConsentLedger, _ConsentRow
from lychd.domain.codex.middleware import SigilAuthMiddleware
from lychd.domain.codex.schemas import NativeConsentBinding
from lychd.domain.codex.sigil import Sigil
from lychd.domain.cortex.context import ContextOrchestrator
from lychd.domain.cortex.engine import QueueRouter, RouteRule, RunEngine
from lychd.domain.cortex.events import InProcessEventBus, RunEvent
from lychd.domain.cortex.ledger import InMemoryRunLedger
from lychd.domain.cortex.runs import RunDeliveryRecord, RunRecord
from lychd.domain.cortex.stasis import InMemoryStasisStore
from lychd.domain.cortex.substrate import RunSubstrate
from lychd.domain.web.fragments import build_fragment_registry
from lychd.domain.web.projection import EventProjector
from lychd.domain.web.schemas import BridgeTurn
from lychd.domain.web.sessions import BridgeSessionStore, SessionRecord
from lychd.interface.web.bridge import BridgeController
from lychd.interface.web.deps import web_dependencies
from tests.agents.fakes import FakeDispatcher, FakeOrchestrator, FakeRegistry
from tests.web.conftest import TEST_ACCESS_PASSWORD, TEST_AUTHORIZATION

if TYPE_CHECKING:
    from lychd.compositions.blockworld.schemas import RefusalCode


LITERAL: dict[str, Any] = {
    "status": "candidate_until_v2_schema_comparison",
    "ids": {
        "request": "43000000-0000-4000-8000-000000000001",
        "mission": "43000000-0000-4000-8000-000000000002",
        "action": "43000000-0000-4000-8000-000000000003",
        "server": "43000000-0000-4000-8000-000000000004",
        "world": "43000000-0000-4000-8000-000000000005",
        "bot": "43000000-0000-4000-8000-000000000006",
        "blueprint": "43000000-0000-4000-8000-000000000007",
        "plot": "43000000-0000-4000-8000-000000000008",
        "lease": "43000000-0000-4000-8000-000000000009",
        "mission_policy": "43000000-0000-4000-8000-00000000000a",
        "policy": "43000000-0000-4000-8000-00000000000b",
        "consent": "43000000-0000-4000-8000-00000000000c",
        "receipt": "43000000-0000-4000-8000-00000000000d",
        "claim": "43000000-0000-4000-8000-00000000000e",
        "session": "43000000-0000-4000-8000-00000000000f",
    },
    "sample_only_run_id": "43999999-0000-4000-8000-000000000001",
    "advisory_run_id": "43999999-0000-4000-8000-000000000099",
    "clock_fixture_now": "2035-01-01T12:00:00Z",
    "external_fixture_materials": {
        "blueprint": {
            "fixture": "one-placement-grid",
            "target": [2, 64, -1],
            "block_state": {"name": "minecraft:oak_log", "properties": {"axis": "y"}},
        },
        "target_before": {"name": "minecraft:air", "properties": {}},
        "inventory": {"generation": 9, "slot": "hotbar.0", "item": "minecraft:oak_log", "count": 3},
        "audience": {"fixture": "complete-review-audience", "principals": ["oracle:reviewer"]},
    },
    "attested_digests": {
        "blueprint": "2ee66ac8cc364666326406d726615b00afa02709b0aa3bb39330943bfd829062",
        "target_before": "ca7476f6bdc5ef7f52c04d6bc9453817dda4f0d463033a6479edfd38ccc892d3",
        "inventory": "ef5196fed27b981c6388ee311a0f8937a9e11ebcba9715a91a3b4237a52f9b1b",
        "audience": "4d1c2f923d9b107e0b0d58f47e538f286a970a6f34406649a94f559a8270d517",
    },
    "candidate_manifest": {
        "schema_version": 2,
        "key": "blockworld.bounded_mission",
        "revision": "2",
        "implementation_revision": "py.1",
        "checkpoint_schema": "blockworld-placement-state-v1",
        "entry_node": "prepare",
        "nodes": [
            {"key": "prepare", "label": "Prepare placement", "kind": "step"},
            {"key": "consent", "label": "Await placement consent", "kind": "gate"},
            {"key": "execute", "label": "Execute placement", "kind": "step"},
            {"key": "project", "label": "Project placement result", "kind": "step"},
            {"key": "end", "label": "End", "kind": "terminal"},
        ],
        "edges": [
            {"key": "prepare-to-consent", "source": "prepare", "target": "consent", "relation": "permits"},
            {"key": "prepare-to-project", "source": "prepare", "target": "project", "relation": "permits"},
            {"key": "consent-waits", "source": "consent", "target": "consent", "relation": "permits"},
            {"key": "consent-to-execute", "source": "consent", "target": "execute", "relation": "permits"},
            {"key": "consent-to-project", "source": "consent", "target": "project", "relation": "permits"},
            {"key": "execute-to-project", "source": "execute", "target": "project", "relation": "permits"},
            {"key": "project-to-end", "source": "project", "target": "end", "relation": "permits"},
        ],
        "digest": "c9530832e79f72e0f4675b1daf62711f71416e25cfc44fc9ef4e8625b4b708c4",
    },
    "request": {
        "schema_version": 1,
        "request_id": "43000000-0000-4000-8000-000000000001",
        "mission_id": "43000000-0000-4000-8000-000000000002",
        "action_id": "43000000-0000-4000-8000-000000000003",
        "expected_cursor": 0,
        "target": [2, 64, -1],
        "block_state": {"name": "minecraft:oak_log", "properties": {"axis": "y"}},
        "inventory_change": {
            "slot": "hotbar.0",
            "item": "minecraft:oak_log",
            "before_count": 3,
            "delta": -1,
            "after_count": 2,
        },
    },
    "command_template": {
        "schema_version": 1,
        "operation": "place_block",
        "request_id": "43000000-0000-4000-8000-000000000001",
        "mission_id": "43000000-0000-4000-8000-000000000002",
        "action_id": "43000000-0000-4000-8000-000000000003",
        "run_id": "OBSERVED_CANONICAL_RUN_ID",
        "principal": {"name": "oracle:builder", "scopes": ["blockworld:place"]},
        "composition": {"key": "blockworld.inhabitant", "revision": "2"},
        "pattern": {
            "key": "blockworld.bounded_mission",
            "revision": "2",
            "manifest_digest": "c9530832e79f72e0f4675b1daf62711f71416e25cfc44fc9ef4e8625b4b708c4",
        },
        "adapter": {"key": "inert-placement", "revision": "1"},
        "mode": "managed",
        "server_id": "43000000-0000-4000-8000-000000000004",
        "world_id": "43000000-0000-4000-8000-000000000005",
        "world_epoch": 7,
        "bot_id": "43000000-0000-4000-8000-000000000006",
        "capability_revision": "oracle-place-event-v1",
        "blueprint_id": "43000000-0000-4000-8000-000000000007",
        "blueprint_digest": "2ee66ac8cc364666326406d726615b00afa02709b0aa3bb39330943bfd829062",
        "plot": {"id": "43000000-0000-4000-8000-000000000008", "min": [-3, 63, -3], "max": [3, 67, 3]},
        "lease": {"id": "43000000-0000-4000-8000-000000000009", "generation": 4, "expires_at": "2035-01-01T12:02:00Z"},
        "mission_policy": {"id": "43000000-0000-4000-8000-00000000000a", "generation": 3},
        "authority_generation": 11,
        "target": [2, 64, -1],
        "block_state": {"name": "minecraft:oak_log", "properties": {"axis": "y"}},
        "target_before_digest": "ca7476f6bdc5ef7f52c04d6bc9453817dda4f0d463033a6479edfd38ccc892d3",
        "inventory_generation": 9,
        "inventory_digest": "ef5196fed27b981c6388ee311a0f8937a9e11ebcba9715a91a3b4237a52f9b1b",
        "inventory_change": {
            "slot": "hotbar.0",
            "item": "minecraft:oak_log",
            "before_count": 3,
            "delta": -1,
            "after_count": 2,
        },
        "expected_cursor": 0,
        "deadline": "2035-01-01T12:01:00Z",
        "placement_budget": 1,
        "postcondition": {
            "target": [2, 64, -1],
            "block_state": {"name": "minecraft:oak_log", "properties": {"axis": "y"}},
            "inventory_after": 2,
            "cursor": 1,
            "proof": "matching_response_and_authoritative_event",
        },
    },
    "review": {
        "schema_version": 1,
        "title": "Place one block on the admitted plot",
        "facts": [
            {"key": "mission", "label": "Mission", "value": "43000000-0000-4000-8000-000000000002"},
            {"key": "action", "label": "Action", "value": "43000000-0000-4000-8000-000000000003"},
            {"key": "principal", "label": "Principal", "value": "oracle:builder\nScopes: blockworld:place"},
            {
                "key": "pattern",
                "label": "Pattern",
                "value": "blockworld.bounded_mission@2\n"
                "Manifest: "
                "c9530832e79f72e0f4675b1daf62711f71416e25cfc44fc9ef4e8625b4b708c4",
            },
            {"key": "adapter", "label": "Adapter", "value": "inert-placement@1"},
            {"key": "server", "label": "Server", "value": "43000000-0000-4000-8000-000000000004\nMode: managed"},
            {"key": "world", "label": "World", "value": "43000000-0000-4000-8000-000000000005\nEpoch: 7"},
            {"key": "bot", "label": "Bot", "value": "43000000-0000-4000-8000-000000000006"},
            {
                "key": "blueprint",
                "label": "Blueprint",
                "value": "43000000-0000-4000-8000-000000000007\n"
                "Digest: "
                "2ee66ac8cc364666326406d726615b00afa02709b0aa3bb39330943bfd829062",
            },
            {
                "key": "plot",
                "label": "Plot",
                "value": "43000000-0000-4000-8000-000000000008\nInclusive bounds: [-3,63,-3] to [3,67,3]",
            },
            {"key": "target", "label": "Target", "value": "[2,64,-1]"},
            {"key": "block_state", "label": "Full block state", "value": "minecraft:oak_log\naxis=y"},
            {
                "key": "inventory",
                "label": "Inventory",
                "value": "hotbar.0: minecraft:oak_log\n3 -> 2 (delta -1)\nGeneration: 9",
            },
            {
                "key": "lease",
                "label": "Lease",
                "value": "43000000-0000-4000-8000-000000000009\nGeneration: 4\nExpires: 2035-01-01T12:02:00Z",
            },
            {
                "key": "policy",
                "label": "Policy",
                "value": "43000000-0000-4000-8000-00000000000a\nMission generation: 3\nAuthority generation: 11",
            },
            {"key": "cursor", "label": "Cursor", "value": "0 -> 1"},
            {"key": "limits", "label": "Limits", "value": "Deadline: 2035-01-01T12:01:00Z\nPlacements: 1"},
            {
                "key": "proof",
                "label": "Required completion proof",
                "value": "Matching response and authoritative event for the same action, "
                "preparation, server, world epoch, bot, target, block state, inventory "
                "and cursor.",
            },
        ],
    },
    "binding_template": {
        "schema_version": 1,
        "owner": "blockworld.inhabitant",
        "action_id": "43000000-0000-4000-8000-000000000003",
        "preparation_digest": "DERIVED_FROM_OBSERVED_RUN",
        "review_profile": "blockworld.placement",
        "review_profile_revision": "2",
        "review_producer": "blockworld.placement-review",
        "review_producer_revision": "1",
        "audience_digest": "4d1c2f923d9b107e0b0d58f47e538f286a970a6f34406649a94f559a8270d517",
        "review_digest": "DERIVED_FROM_LITERAL_REVIEW",
    },
    "observed_template": {
        "kind": "observed",
        "response": {
            "action_id": "43000000-0000-4000-8000-000000000003",
            "preparation_digest": "DERIVED_FROM_OBSERVED_RUN",
            "server_id": "43000000-0000-4000-8000-000000000004",
            "world_id": "43000000-0000-4000-8000-000000000005",
            "world_epoch": 7,
            "bot_id": "43000000-0000-4000-8000-000000000006",
            "response_id": "oracle-response-17",
            "accepted": True,
        },
        "event": {
            "action_id": "43000000-0000-4000-8000-000000000003",
            "preparation_digest": "DERIVED_FROM_OBSERVED_RUN",
            "server_id": "43000000-0000-4000-8000-000000000004",
            "world_id": "43000000-0000-4000-8000-000000000005",
            "world_epoch": 7,
            "bot_id": "43000000-0000-4000-8000-000000000006",
            "event_id": "oracle-sentinel-17",
            "event_sequence": 17,
            "target": [2, 64, -1],
            "block_state": {"name": "minecraft:oak_log", "properties": {"axis": "y"}},
            "inventory_after": 2,
            "cursor_before": 0,
            "cursor_after": 1,
        },
    },
    "receipt_template": {
        "schema_version": 1,
        "receipt_id": "43000000-0000-4000-8000-00000000000d",
        "action_id": "43000000-0000-4000-8000-000000000003",
        "preparation_digest": "DERIVED_FROM_OBSERVED_RUN",
        "server_id": "43000000-0000-4000-8000-000000000004",
        "world_id": "43000000-0000-4000-8000-000000000005",
        "world_epoch": 7,
        "bot_id": "43000000-0000-4000-8000-000000000006",
        "adapter_revision": "1",
        "response_id": "oracle-response-17",
        "authoritative_event_id": "oracle-sentinel-17",
        "event_sequence": 17,
        "target": [2, 64, -1],
        "block_state": {"name": "minecraft:oak_log", "properties": {"axis": "y"}},
        "inventory_after": 2,
        "cursor_before": 0,
        "cursor_after": 1,
        "placement_cost": 1,
        "evidence_digest": "DERIVED_FROM_LITERAL_EVIDENCE",
    },
    "policy_authorization_template": {
        "slug": "oracle-blockworld-one-placement",
        "priority": 10,
        "klass": "standard",
        "sigil_pattern": "oracle:builder",
        "tool_pattern": "blockworld.place_block",
        "constraints": "DERIVED_FROM_OBSERVED_RUN",
        "expires_at": "2035-01-01T12:01:00+00:00",
        "max_uses": 1,
        "granted_by": "codex:rune",
    },
    "current_facts": {
        "principal_name": "oracle:builder",
        "effect_allowed": True,
        "authority_generation": 11,
        "mission_id": "43000000-0000-4000-8000-000000000002",
        "mission_active": True,
        "mission_policy_id": "43000000-0000-4000-8000-00000000000a",
        "mission_policy_generation": 3,
        "server_id": "43000000-0000-4000-8000-000000000004",
        "world_id": "43000000-0000-4000-8000-000000000005",
        "world_epoch": 7,
        "bot_id": "43000000-0000-4000-8000-000000000006",
        "capability_revision": "oracle-place-event-v1",
        "can_place": True,
        "can_prove": True,
        "lease_id": "43000000-0000-4000-8000-000000000009",
        "lease_generation": 4,
        "lease_active": True,
        "lease_expires_at": "2035-01-01T12:02:00Z",
        "target_before_digest": "ca7476f6bdc5ef7f52c04d6bc9453817dda4f0d463033a6479edfd38ccc892d3",
        "inventory_generation": 9,
        "inventory_digest": "ef5196fed27b981c6388ee311a0f8937a9e11ebcba9715a91a3b4237a52f9b1b",
        "inventory_count": 3,
        "cursor": 0,
        "placements_remaining": 1,
    },
    "review_eligibility": {
        "disclose_complete": True,
        "judge": True,
        "audience_digest": "4d1c2f923d9b107e0b0d58f47e538f286a970a6f34406649a94f559a8270d517",
    },
}

IDS = LITERAL["ids"]
NOW = datetime(2035, 1, 1, 12, tzinfo=UTC)
BUILDER = Sigil(name="oracle:builder", scopes=frozenset({"blockworld:place"}))
REVIEWER = Sigil(name="oracle:reviewer", scopes=frozenset({"altar:read", "runs:approve"}))


def canonical(value: Any) -> bytes:
    """Independent stdlib encoding; never asks the writer for expected bytes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(prefix: str, value: Any) -> str:
    return hashlib.sha256(prefix.encode() + canonical(value)).hexdigest()


def expected(run_id: str) -> dict[str, Any]:
    """Substitute only the canonical ID observed from actual engine admission."""
    assert str(UUID(run_id)) == run_id
    command = copy.deepcopy(LITERAL["command_template"])
    command["run_id"] = run_id
    prep = digest("lychd.blockworld.placement.v1\n", command)
    review = copy.deepcopy(LITERAL["review"])
    binding = copy.deepcopy(LITERAL["binding_template"])
    binding.update(preparation_digest=prep, review_digest=digest("lychd.consent-review.v1\n", review))
    observed = copy.deepcopy(LITERAL["observed_template"])
    observed["response"]["preparation_digest"] = prep
    observed["event"]["preparation_digest"] = prep
    receipt = copy.deepcopy(LITERAL["receipt_template"])
    receipt.update(preparation_digest=prep, evidence_digest=digest("lychd.blockworld.evidence.v1\n", observed))
    constraints = {
        "args": {
            key: [value]
            for key, value in {
                "action_id": IDS["action"],
                "mission_id": IDS["mission"],
                "preparation_digest": prep,
            }.items()
        }
    }
    policy = copy.deepcopy(LITERAL["policy_authorization_template"])
    policy["constraints"] = constraints
    return {
        "command": command,
        "command_bytes": canonical(command),
        "preparation_digest": prep,
        "review": review,
        "review_bytes": canonical(review),
        "binding": binding,
        "binding_digest": digest("lychd.native-consent-binding.v1\n", binding),
        "observed": observed,
        "receipt": receipt,
        "receipt_bytes": canonical(receipt),
        "receipt_digest": digest("lychd.blockworld.receipt.v1\n", receipt),
        "policy": policy,
        "policy_digest": hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }


def admitted_document(mode: str = "live") -> dict[str, Any]:
    """Seed trusted mission authority only; never seed a Core Run or action."""
    c = LITERAL["command_template"]
    names = (
        "principal",
        "server_id",
        "world_id",
        "world_epoch",
        "bot_id",
        "capability_revision",
        "blueprint_id",
        "blueprint_digest",
        "plot",
        "lease",
        "mission_policy",
        "authority_generation",
        "deadline",
        "target_before_digest",
        "inventory_generation",
        "inventory_digest",
    )
    return {
        "schema_version": 1,
        "request": copy.deepcopy(LITERAL["request"]),
        "pattern_snapshot": copy.deepcopy(LITERAL["candidate_manifest"]),
        **{name: copy.deepcopy(c[name]) for name in names},
        "capabilities": ["place_block"],
        "lease": {
            **copy.deepcopy(c["lease"]),
            "bot_id": c["bot_id"],
            "plot_id": c["plot"]["id"],
            "effect": "place_block",
        },
        "audience": ["oracle:reviewer"],
        "audience_digest": LITERAL["attested_digests"]["audience"],
        "judgment_mode": mode,
        "policy_id": IDS["policy"] if mode != "live" else None,
    }


@dataclass
class CapturedQueue:
    """Never consumes a job: policy fixtures install only after actual submit."""

    attempts: list[dict[str, Any]] = field(default_factory=list)
    jobs: dict[str, Any] = field(default_factory=dict)

    async def enqueue(self, job_or_func: str, /, **kwargs: Any) -> Any:
        self.attempts.append({"func": job_or_func, **kwargs})
        return self.jobs.setdefault(kwargs["key"], SimpleNamespace(key=kwargs["key"], status="queued"))

    async def job(self, job_key: str, /) -> Any:
        return self.jobs.get(job_key)

    async def abort(self, job: Any, error: str, /, ttl: float = 5) -> None:
        del error, ttl
        job.status = "aborted"


class SharedConsents(InMemoryConsentLedger):
    """Existing Core/web consent behavior over the composite's exact row map."""

    def __init__(self, lock: asyncio.Lock) -> None:
        super().__init__()
        self.lock = lock
        self.store: Any = None

    async def verdict(self, consent_id: str) -> bool | None:
        from lychd.compositions.blockworld.schemas import decode_command
        from lychd.compositions.blockworld.services import exact_policy_eligible

        verdict = await super().verdict(consent_id)
        row = self._rows.get(consent_id)
        if verdict is not True or row is None or row.decided_by != "codex:preauth":
            return verdict
        assert row.native_binding is not None
        action = await self.store.read_action(row.native_binding.action_id)
        retained = self.store.consent_payloads.get(consent_id, {}).get("preauth_digest")
        return retained is not None and exact_policy_eligible(
            self.store.policy,
            decode_command(action.command_bytes),
            self.store.now,
            consumed=True,
            authorization_digest=retained,
        )

    async def decide(self, consent_id: str, **kwargs: Any) -> Any:
        async with self.lock:
            return await super().decide(consent_id, **kwargs)

    async def park(self, **_kwargs: Any) -> Any:
        message = "Native preparation must insert into the shared transaction, not legacy park"
        raise AssertionError(message)


class ExternalAuthority:
    """Controlled external facts; no local policy eligibility logic."""

    def __init__(self) -> None:
        self.facts = copy.deepcopy(LITERAL["current_facts"])
        self.reviewers = {"oracle:reviewer"}
        self.reads = 0

    async def current(self, command_bytes: bytes, principal: Sigil) -> Any:
        from lychd.compositions.blockworld.schemas import CurrentPlacementFacts

        del command_bytes, principal
        self.reads += 1
        return CurrentPlacementFacts.model_validate(copy.deepcopy(self.facts))

    async def review_access(self, action_id: str, audience_digest: str, reviewer: Sigil, purpose: str) -> Any:
        from lychd.compositions.blockworld.schemas import ReviewEligibility

        del action_id, purpose
        eligible = reviewer.name in self.reviewers
        return ReviewEligibility.model_validate(
            {
                "disclose_complete": eligible,
                "judge": eligible,
                "audience_digest": audience_digest,
            }
        )


class WorldJournal:
    """Independent world truth survives reconstruction of every local service."""

    def __init__(self) -> None:
        self.invokes: list[bytes] = []
        self.effects = 0
        self.status_calls = 0
        self.observed: dict[str, Any] | None = None
        self.lose_ack = False
        self.status_unknown = False
        self.target: dict[str, Any] = {"name": "minecraft:air", "properties": {}}
        self.inventory = 3
        self.cursor = 0
        self.vector: dict[str, Any] | None = None

    async def invoke(self, command_bytes: bytes) -> Any:
        from lychd.compositions.blockworld.schemas import PlacementEvidence, UnknownEvidence

        assert self.vector is not None
        self.invokes.append(command_bytes)
        assert command_bytes == self.vector["command_bytes"]
        assert self.effects == 0, "The issuer invoked the same placement twice"
        self.effects += 1
        self.target = {"name": "minecraft:oak_log", "properties": {"axis": "y"}}
        self.inventory, self.cursor = 2, 1
        self.observed = copy.deepcopy(self.vector["observed"])
        if self.lose_ack:
            return UnknownEvidence(
                kind="unknown", action_id=IDS["action"], preparation_digest=self.vector["preparation_digest"]
            )
        return PlacementEvidence.model_validate(self.observed)

    async def status(self, command_bytes: bytes) -> Any:
        from lychd.compositions.blockworld.schemas import PlacementEvidence, UnknownEvidence

        assert self.vector is not None
        assert command_bytes == self.vector["command_bytes"]
        self.status_calls += 1
        if self.observed is None or self.status_unknown:
            return UnknownEvidence(
                kind="unknown", action_id=IDS["action"], preparation_digest=self.vector["preparation_digest"]
            )
        return PlacementEvidence.model_validate(copy.deepcopy(self.observed))


class WorldPort:
    """A fresh process handle to the external journal, which owns no local state."""

    def __init__(self, journal: WorldJournal) -> None:
        self.journal = journal

    async def invoke(self, command_bytes: bytes) -> Any:
        return await self.journal.invoke(command_bytes)

    async def status(self, command_bytes: bytes) -> Any:
        return await self.journal.status(command_bytes)


class SimulatedProcessLoss(BaseException):
    """Stop a test at a real HELD row without running graceful compensation."""


class Boot:
    """Fresh real engine/worker/web assembly around one portable committed store."""

    def __init__(self, *, mode: str = "live", world: WorldJournal | None = None, image: bytes | None = None) -> None:
        from lychd.compositions.blockworld.review import BlockworldConsentReviewProducer
        from lychd.compositions.blockworld.services import PlacementService
        from lychd.compositions.blockworld.workflow import placement_workflow

        self.ledger = InMemoryRunLedger()
        self.lock = asyncio.Lock()
        self.consents = SharedConsents(self.lock)
        self.checkpoints = InMemoryStasisStore()
        self.sessions = BridgeSessionStore()
        self.authority = ExternalAuthority()
        self.world = world or WorldJournal()
        self.store = PortableStore(self, admitted_document(mode))
        self.consents.store = self.store
        self.effect = WorldPort(self.world)
        self.service = PlacementService(self.store, self.authority, self.effect)
        self.workflow = placement_workflow(self.service)
        self.workflows = BuiltinWorkflowRegistry(workflows=(self.workflow,))
        self.queue = CapturedQueue()
        self.bus = InProcessEventBus(ledger=self.ledger)
        self.dispatcher = FakeDispatcher(model=None)
        self.orchestrator = FakeOrchestrator()
        self.fragments = build_fragment_registry()
        self.substrate = RunSubstrate(
            ledger=self.ledger,
            bus=self.bus,
            workflows=self.workflows,
            orchestrator=self.orchestrator,
            dispatcher=self.dispatcher,
            context=ContextOrchestrator(registry=FakeRegistry()),
            fragments=self.fragments,
            turns=self.sessions,
            consents=self.consents,
            forge=default_forge(),
            stasis_store=self.checkpoints,
            queues={"runs": self.queue},
        )
        self.engine = RunEngine(
            ledger=self.ledger,
            bus=self.bus,
            workflows=self.workflows,
            queue_router=QueueRouter({"default": RouteRule(queue="runs", priority=70)}),
            queues=self.substrate.queues,
            stasis_store=self.checkpoints,
            consents=self.consents,
            release_context=self.substrate.context.release,
        )
        self.producer = BlockworldConsentReviewProducer(self.store, self.authority)
        self.projector = EventProjector(
            fragments=self.fragments,
            sessions=self.sessions,
            consents=self.consents,
            consent_reviews=self.producer,
        )
        self.session_id: str | None = None
        self.run_id: str | None = None
        self.reviewers = [REVIEWER]
        if image is not None:
            self.restore(image)

    async def submit(self) -> Any:
        if self.session_id is None:
            self.session_id = (await self.sessions.create_session(title="Independent placement oracle")).id
        result = await self.service.submit(
            copy.deepcopy(LITERAL["request"]),
            engine=self.engine,
            ledger=self.ledger,
            session_id=self.session_id,
            principal=BUILDER,
        )
        if result["run_id"] is not None:
            self.run_id = result["run_id"]
            assert self.run_id is not None
            self.world.vector = expected(self.run_id)
        return result

    def install_policy(self) -> None:
        """Trusted fixture setup AFTER admission, before the inert queue is driven."""
        assert self.run_id is not None
        data = expected(self.run_id)["policy"]
        self.store.policy = SimpleNamespace(
            **{key: value for key, value in data.items() if key != "expires_at"},
            id=UUID(IDS["policy"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            uses=0,
            source_present=True,
            enabled=True,
        )
        self.store.broad_policy = SimpleNamespace(
            **{
                **vars(self.store.policy),
                "id": UUID("43000000-0000-4000-8000-000000000010"),
                "slug": "broad-competing-grant",
                "priority": 999,
                "constraints": {},
            },
        )

    async def drive(self) -> dict[str, Any]:
        from lychd.ghouls.runs import perform_run

        assert self.run_id is not None
        run = await self.ledger.get(self.run_id)
        assert run is not None
        result = await perform_run({"run_substrate": self.substrate}, run_id=self.run_id, enqueue_seq=run.enqueue_seq)
        await self.bus.wait_persisted(self.run_id)
        return result

    def client(self) -> httpx.AsyncClient:
        reviewers = self.reviewers

        class ReviewerMiddleware(SigilAuthMiddleware):
            async def authenticate_request(
                self, connection: ASGIConnection[Any, Any, Any, Any]
            ) -> AuthenticationResult:
                await super().authenticate_request(connection)
                return AuthenticationResult(user=reviewers[0], auth=None)

        services = SimpleNamespace(
            ledger=self.ledger,
            consents=self.consents,
            run_engine=self.engine,
            projector=self.projector,
            bridge_sessions=self.sessions,
            bus=self.bus,
            workflows=self.workflows,
            fragments=self.fragments,
        )
        app = Litestar(
            route_handlers=[BridgeController],
            dependencies=web_dependencies,
            middleware=[DefineMiddleware(ReviewerMiddleware, access_password=TEST_ACCESS_PASSWORD)],
            state=State({"services": services}),
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cast("Any", app)),
            base_url="http://testserver.local",
            headers={"authorization": TEST_AUTHORIZATION},
        )

    async def image(self) -> bytes:
        """JSON-only reconstruction cut: no tasks, locks, services or world state."""
        assert self.run_id is not None
        await self.bus.wait_persisted(self.run_id)
        value = {
            "runs": TypeAdapter(list[RunRecord]).dump_python(list(self.ledger._runs.values()), mode="json"),
            "keys": self.ledger._idempotency_keys,
            "deliveries": TypeAdapter(list[RunDeliveryRecord]).dump_python(
                list(self.ledger._deliveries.values()), mode="json"
            ),
            "events": TypeAdapter(dict[str, list[RunEvent]]).dump_python(self.ledger._events, mode="json"),
            "consents": TypeAdapter(list[_ConsentRow]).dump_python(list(self.consents._rows.values()), mode="json"),
            "checkpoints": self.checkpoints._documents,
            "sessions": [
                {
                    "id": s.id,
                    "title": s.title,
                    "created_at": s.created_at.isoformat(),
                    "sigil_name": s.sigil_name,
                    "turns": TypeAdapter(list[BridgeTurn]).dump_python(s.turns, mode="json"),
                    "message_history": s.message_history,
                }
                for s in await self.sessions.list_sessions()
            ],
            "store": self.store.image(),
            "run_id": self.run_id,
            "session_id": self.session_id,
        }
        return canonical(value)

    def restore(self, image: bytes) -> None:
        value = json.loads(image)
        self.ledger._runs = {r.run_id: r for r in TypeAdapter(list[RunRecord]).validate_python(value["runs"])}
        self.ledger._idempotency_keys = value["keys"]
        self.ledger._deliveries = {
            (r.run_id, r.enqueue_seq): r
            for r in TypeAdapter(list[RunDeliveryRecord]).validate_python(value["deliveries"])
        }
        self.ledger._events = TypeAdapter(dict[str, list[RunEvent]]).validate_python(value["events"])
        self.consents._rows = {r.id: r for r in TypeAdapter(list[_ConsentRow]).validate_python(value["consents"])}
        self.checkpoints._documents = value["checkpoints"]
        self.sessions._sessions = {
            r["id"]: SessionRecord(
                id=r["id"],
                title=r["title"],
                created_at=datetime.fromisoformat(r["created_at"]),
                sigil_name=r["sigil_name"],
                turns=TypeAdapter(list[BridgeTurn]).validate_python(r["turns"]),
                message_history=r["message_history"],
            )
            for r in value["sessions"]
        }
        self.sessions._run_to_session = {
            t.run_id: s.id for s in self.sessions._sessions.values() for t in s.turns if t.run_id is not None
        }
        self.store.restore(value["store"])
        self.run_id, self.session_id = value["run_id"], value["session_id"]
        assert self.run_id is not None
        self.world.vector = expected(self.run_id)


class PortableStore:
    """One staging lock; owned production functions decide every admission/effect.

    Conditional writes below model storage predicates only. They are not SQL
    transaction proof. No expected-byte builder participates in these decisions.
    """

    def __init__(self, boot: Boot, admission: dict[str, Any]) -> None:
        from lychd.compositions.blockworld.ports import MissionSnapshot
        from lychd.compositions.blockworld.schemas import AdmittedPlacementV1

        self.boot = boot
        self.mission = MissionSnapshot(admission=AdmittedPlacementV1.model_validate(admission), run_id=None)
        self.action: Any = None
        self.policy: Any = None
        self.broad_policy: Any = None
        self.consent_payloads: dict[str, dict[str, Any]] = {}
        self.failure: str | None = None
        self.now = NOW
        self.before_final: tuple[asyncio.Event, asyncio.Event] | None = None
        self.retention_deliveries: list[Any] = []

    def trip(self, point: str) -> None:
        from lychd.compositions.blockworld.schemas import PlacementCommitUnknownError

        if self.failure == point:
            self.failure = None
            raise PlacementCommitUnknownError(point)

    async def read_mission(self, request_id: str) -> Any:
        from lychd.compositions.blockworld.services import validate_mission

        assert request_id == self.mission.admission.request.request_id
        validate_mission(self.mission)
        return self.mission

    async def retain_run(self, request_id: str, canonical_run_id: str) -> None:
        from lychd.compositions.blockworld.schemas import PlacementConflictError
        from lychd.compositions.blockworld.services import validate_admitted_run

        async with self.boot.lock:
            await self.read_mission(request_id)
            delivery = await self.boot.ledger.get_delivery(canonical_run_id, enqueue_seq=0)
            assert delivery is not None
            self.retention_deliveries.append(delivery.state)
            if self.mission.run_id not in (None, canonical_run_id):
                message = "mission already bound"
                raise PlacementConflictError(message)
            if self.failure == "retain_process_loss":
                raise SimulatedProcessLoss
            if self.failure == "retain_before_commit":
                message = "retention failed before commit"
                raise RuntimeError(message)
            staged = replace(self.mission, run_id=canonical_run_id)
            validate_admitted_run(staged, await self.boot.ledger.get(canonical_run_id))
            self.mission = staged
            if self.failure == "retain_bound_process_loss":
                raise SimulatedProcessLoss
            if self.failure == "retain_after_commit":
                message = "retention acknowledgement failed after commit"
                raise RuntimeError(message)
            self.trip("retain_after_unknown")

    async def load_for_run(self, run_id: str) -> Any:
        from lychd.compositions.blockworld.schemas import PlacementUnavailableError

        if self.mission.run_id != run_id:
            message = "mission not bound to this Run"
            raise PlacementUnavailableError(message)
        return await self.read_mission(self.mission.admission.request.request_id)

    async def read_action(self, action_id: str) -> Any:
        from lychd.compositions.blockworld.schemas import PlacementNotFoundError
        from lychd.compositions.blockworld.services import validate_action

        if self.action is None or self.action.action_id != action_id:
            message = "no retained action"
            raise PlacementNotFoundError(message)
        validate_action(self.action, self.mission)
        return self.action

    async def prepare_and_admit(self, run_id: str) -> Any:
        from lychd.compositions.blockworld.ports import ActionSnapshot, ExistingDisposition, PreparedAction, Refused
        from lychd.compositions.blockworld.review import prepare_binding
        from lychd.compositions.blockworld.schemas import canonical_bytes, placement_digest
        from lychd.compositions.blockworld.services import (
            exact_policy_eligible,
            not_issued,
            prepare_command,
            validate_admitted_run,
        )
        from lychd.domain.codex.services import preauth_authorization_digest

        async with self.boot.lock:
            mission = await self.load_for_run(run_id)
            if mission.result is not None and self.action is None:
                return Refused(result=mission.result)
            if self.action is not None:
                action = await self.read_action(self.action.action_id)
                if action.status != "prepared":
                    return ExistingDisposition(action_id=action.action_id, status=action.status)
                return PreparedAction(
                    action_id=action.action_id,
                    consent_id=action.consent_id,
                    preparation_digest=action.preparation_digest,
                    native_binding=action.native_binding,
                    status=action.status,
                )
            validate_admitted_run(mission, await self.boot.ledger.get(run_id))
            command = prepare_command(mission.admission, run_id)
            binding, review = prepare_binding(command, mission.admission)
            selected = (
                self.policy if mission.admission.policy_id == (str(self.policy.id) if self.policy else None) else None
            )
            eligible = mission.admission.judgment_mode != "live" and exact_policy_eligible(selected, command, self.now)
            if not eligible and mission.admission.judgment_mode == "exact_preauth":
                result = not_issued(command.action_id, "policy_changed")
                self.trip("mission_result_before_commit")
                self.mission = replace(mission, result=result)
                self.trip("mission_result_after_commit")
                return Refused(result=result)
            staged_policy = copy.copy(self.policy)
            payload: dict[str, Any] = {}
            if eligible:
                staged_policy.uses += 1
                payload["preauth_digest"] = preauth_authorization_digest(staged_policy)
            self.trip("prepare_after_policy")
            row = _ConsentRow(
                id=IDS["consent"],
                run_id=run_id,
                tool_name="blockworld.place_block",
                args={
                    "action_id": command.action_id,
                    "mission_id": command.mission_id,
                    "preparation_digest": binding.preparation_digest,
                },
                call_ids=(),
                status="granted" if eligible else "pending",
                decided_by="codex:preauth" if eligible else None,
                decided_at=self.now if eligible else None,
                preauth_slug=staged_policy.slug if eligible else None,
                native_review_required=True,
                native_binding=binding,
            )
            self.trip("prepare_after_consent")
            encoded = canonical_bytes(command)
            action = ActionSnapshot(
                action_id=command.action_id,
                mission_id=command.mission_id,
                run_id=run_id,
                consent_id=row.id,
                command_bytes=encoded,
                preparation_digest=placement_digest(encoded),
                review_bytes=review.canonical_bytes(),
                native_binding=binding,
                status="prepared",
            )
            self.trip("prepare_before_commit")
            self.action, self.policy = action, staged_policy
            self.boot.consents._rows[row.id] = row
            self.consent_payloads[row.id] = payload
            self.trip("prepare_after_commit")
            return PreparedAction(
                action_id=action.action_id,
                consent_id=action.consent_id,
                preparation_digest=action.preparation_digest,
                native_binding=binding,
                status="prepared",
            )

    async def _refusal(self, action: Any, principal: Sigil) -> Any:
        from lychd.compositions.blockworld.services import (
            authority_refusal,
            consent_refusal,
            validate_action,
            validate_admitted_run,
        )

        command = validate_action(action, self.mission)
        run = await self.boot.ledger.get(action.run_id)
        consent = await self.boot.consents.get(action.consent_id)
        if run is not None:
            validate_admitted_run(self.mission, run)
        reason = consent_refusal(
            action,
            consent,
            run,
            self.policy,
            self.now,
            authorization_digest=self.consent_payloads.get(action.consent_id, {}).get("preauth_digest"),
        )
        if reason is None:
            facts = await self.boot.authority.current(action.command_bytes, principal)
            reason = authority_refusal(command, facts, self.mission, self.now, principal)
        return reason

    def _not_issued(self, reason: RefusalCode) -> Any:
        from lychd.compositions.blockworld.ports import Refused
        from lychd.compositions.blockworld.services import not_issued

        result = not_issued(self.action.action_id, reason)
        self.action = replace(self.action, status="not_issued", reason=reason)
        self.mission = replace(self.mission, pending_action_id=None, result=result)
        return Refused(result=result)

    async def claim_issue(self, action_id: str, expected_binding: NativeConsentBinding, principal: Sigil) -> Any:
        from lychd.compositions.blockworld.ports import ExistingDisposition, NewClaim
        from lychd.compositions.blockworld.schemas import PlacementConflictError

        async with self.boot.lock:
            action = await self.read_action(action_id)
            if action.native_binding != expected_binding:
                message = "binding changed"
                raise PlacementConflictError(message)
            if action.status != "prepared":
                return ExistingDisposition(action_id=action_id, status=action.status)
            reason = await self._refusal(action, principal)
            if reason is not None:
                return self._not_issued(reason)
            consent = await self.boot.consents.get(action.consent_id)
            assert consent is not None
            assert consent.decided_at is not None
            self.action = replace(
                action,
                status="pending",
                claim_id=IDS["claim"],
                claimed_at=self.now,
                issuance_judgment={
                    "consent_id": action.consent_id,
                    "binding_digest": action.native_binding.digest,
                    "verdict": "granted",
                    "decided_by": consent.decided_by,
                    "decided_at": consent.decided_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "policy_id": str(self.policy.id) if consent.preauth_slug is not None else None,
                    "policy_slug": consent.preauth_slug,
                    "policy_digest": self.consent_payloads[action.consent_id].get("preauth_digest"),
                },
            )
            self.mission = replace(self.mission, pending_action_id=action_id)
            self.trip("claim_after_commit")
            return NewClaim(action_id=action_id, claim_id=IDS["claim"], command_bytes=action.command_bytes)

    async def validate_live_claim_before_invoke(self, claim: Any, principal: Sigil) -> Any:
        from lychd.compositions.blockworld.ports import ExistingDisposition

        if self.before_final is not None:
            reached, release = self.before_final
            reached.set()
            await release.wait()
        async with self.boot.lock:
            action = await self.read_action(claim.action_id)
            if action.status != "pending" or action.claim_id != claim.claim_id:
                return ExistingDisposition(action_id=action.action_id, status=action.status)
            reason = await self._refusal(action, principal)
            if reason is not None:
                return self._not_issued(reason)
            return "ready"

    async def record_not_issued_before_invoke(self, action_id: str, claim_id: str, reason: RefusalCode) -> Any:
        from lychd.compositions.blockworld.ports import ExistingDisposition

        async with self.boot.lock:
            action = await self.read_action(action_id)
            if action.status != "pending" or action.claim_id != claim_id:
                return ExistingDisposition(action_id=action_id, status=action.status)
            return self._not_issued(reason)

    async def refuse_prepared(self, action_id: str, reason: RefusalCode) -> Any:
        from lychd.compositions.blockworld.ports import ExistingDisposition

        async with self.boot.lock:
            action = await self.read_action(action_id)
            if action.status != "prepared":
                return ExistingDisposition(action_id=action_id, status=action.status)
            return self._not_issued(reason)

    async def record_unknown(self, action_id: str, reason: str) -> Any:
        from lychd.compositions.blockworld.services import result_for

        async with self.boot.lock:
            action = await self.read_action(action_id)
            if action.status in {"verified", "not_issued"}:
                return result_for(action, self.mission)
            assert action.status in {"pending", "unverified_effect"}
            staged = replace(action, status="unverified_effect", reason=reason)
            mission = replace(self.mission, result=None)
            result = result_for(staged, mission)
            self.action, self.mission = staged, replace(mission, result=result)
            return result

    async def settle_verified(self, action_id: str, evidence: Any) -> Any:
        from lychd.compositions.blockworld.schemas import (
            PlacementIntegrityError,
            PlacementReceiptV1,
            canonical_bytes,
            receipt_digest,
        )
        from lychd.compositions.blockworld.services import receipt_for, result_for, validate_action

        async with self.boot.lock:
            action = await self.read_action(action_id)
            if action.status == "verified":
                prior = PlacementReceiptV1.model_validate_json(action.receipt_bytes)
                supplied = canonical_bytes(
                    receipt_for(validate_action(action, self.mission), evidence, prior.receipt_id)
                )
                if supplied != action.receipt_bytes:
                    message = "settled evidence changed"
                    raise PlacementIntegrityError(message)
                return result_for(action, self.mission)
            if action.status == "not_issued":
                message = "positive evidence contradicts retained nonissuance"
                raise PlacementIntegrityError(message)
            assert action.status in {"pending", "unverified_effect"}
            command = validate_action(action, self.mission)
            receipt = canonical_bytes(receipt_for(command, evidence, IDS["receipt"]))
            staged = replace(
                action, status="verified", receipt_bytes=receipt, receipt_digest=receipt_digest(receipt), reason=None
            )
            mission = replace(self.mission, cursor=1, placements_remaining=0, pending_action_id=None, result=None)
            result = result_for(staged, mission)
            self.trip("settle_before_commit")
            self.action, self.mission = staged, replace(mission, result=result)
            self.trip("settle_after_commit")
            return result

    def image(self) -> dict[str, Any]:
        from lychd.compositions.blockworld.ports import MissionSnapshot

        def policy(value: Any) -> dict[str, Any] | None:
            if value is None:
                return None
            document = cast("dict[str, Any]", vars(value))
            return {**document, "id": str(value.id), "expires_at": value.expires_at.isoformat()}

        action = None
        if self.action is not None:
            action = {item.name: getattr(self.action, item.name) for item in fields(self.action)}
            action["native_binding"] = asdict(self.action.native_binding)
            action["issuance_judgment"] = (
                dict(self.action.issuance_judgment) if self.action.issuance_judgment is not None else None
            )
            action["claimed_at"] = self.action.claimed_at.isoformat() if self.action.claimed_at is not None else None
            for key in ("command_bytes", "review_bytes", "receipt_bytes"):
                action[key] = action[key].decode("utf-8") if action[key] is not None else None
        return {
            "mission": TypeAdapter(MissionSnapshot).dump_python(self.mission, mode="json"),
            "action": action,
            "policy": policy(self.policy),
            "broad": policy(self.broad_policy),
            "payloads": self.consent_payloads,
        }

    def restore(self, value: dict[str, Any]) -> None:
        from lychd.compositions.blockworld.ports import ActionSnapshot, MissionSnapshot

        self.mission = TypeAdapter(MissionSnapshot).validate_json(canonical(value["mission"]))
        self.action = TypeAdapter(ActionSnapshot | None).validate_json(canonical(value["action"]))
        for key, attr in (("policy", "policy"), ("broad", "broad_policy")):
            data = value[key]
            if data is not None:
                setattr(
                    self,
                    attr,
                    SimpleNamespace(
                        **{**data, "id": UUID(data["id"]), "expires_at": datetime.fromisoformat(data["expires_at"])}
                    ),
                )
        self.consent_payloads = value["payloads"]
