"""Private assembly identities for existing v2 Python stations.

These values never enter Run or renderer snapshots. The internal-v0 digest
identifies local bindings, not portable contracts, signatures or Python source.
Pattern and Workflow validation retain ownership of graph shape and topology.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Literal

from lychd.agents.workflows.base import PatternManifest, PatternNodeKind


@dataclass(frozen=True)
class LegacyInlineContract:
    """One Scroll-private contract, scoped to its exact v2 station."""

    pattern_id: str
    pattern_revision: str
    manifest_digest: str
    station_id: str
    kind: Literal["legacy_inline"] = field(default="legacy_inline", init=False)


@dataclass(frozen=True)
class LegacyInlinePlacement:
    """Place the private contract at one semantic station."""

    contract: LegacyInlineContract
    station_id: str
    station_kind: PatternNodeKind


@dataclass(frozen=True)
class LegacyInlineImplementation:
    """Bind a private contract to the declared local Python implementation."""

    contract: LegacyInlineContract
    implementation_revision: str
    checkpoint_schema: str
    node_fingerprint: str


@dataclass(frozen=True)
class LegacyInlineBinding:
    """Resolve one private placement to its exact implementation."""

    placement: LegacyInlinePlacement
    implementation: LegacyInlineImplementation


@dataclass(frozen=True)
class LegacyInlineLock:
    """A boot-local binding lock; never a persisted or portable Resolution Lock."""

    bindings: tuple[LegacyInlineBinding, ...]
    version: Literal["internal-v0"] = field(default="internal-v0", init=False)

    @property
    def digest(self) -> str:
        """Hash the explicit internal format without claiming JCS canonicalization."""
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


def legacy_inline_lock(manifest: PatternManifest) -> LegacyInlineLock:
    """Adapt validated v2 stations; the declarative terminal remains Graph End."""
    manifest_digest = manifest.digest
    bindings: list[LegacyInlineBinding] = []
    for node in sorted(manifest.nodes, key=lambda node: node.key):
        implementation = node.implementation
        if implementation is None:
            continue
        contract = LegacyInlineContract(
            pattern_id=manifest.key,
            pattern_revision=manifest.revision,
            manifest_digest=manifest_digest,
            station_id=node.key,
        )
        bindings.append(
            LegacyInlineBinding(
                placement=LegacyInlinePlacement(contract, node.key, node.kind),
                implementation=LegacyInlineImplementation(
                    contract=contract,
                    implementation_revision=manifest.implementation_revision,
                    checkpoint_schema=manifest.checkpoint_schema,
                    node_fingerprint=f"{implementation.__module__}:{implementation.__qualname__}",
                ),
            )
        )
    return LegacyInlineLock(tuple(bindings))
