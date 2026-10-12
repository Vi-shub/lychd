"""Restricted local contracts for the closed diagnostic fixture profile.

This profile demonstrates irreversible preparation, not PII discovery or anonymity
of arbitrary documents. Plain digests below are local dictionary/linkage oracles;
only the opaque public event projection is suitable for broad retention.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, Field

from lychd.domain.privacy.errors import PrivacyRefused
from lychd.domain.privacy.labels import (
    HandlingConstraint,
    PrivacyCategory,
    PrivacyValue,
    PrivatizationLabel,
    join_labels,
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Epoch = Annotated[int, Field(strict=True, ge=0)]
PROFILE = "closed-diagnostic@1"
VERIFIER = "closed-diagnostic-verifier@1"
CENSOR = "closed-diagnostic-censor@1"
REDACTION = "[redacted]"
MAX_MATERIAL_CHARS = 16_384
MAX_PROJECTION_CHARS = 65_536
MAX_MATERIALS = 64
MAX_PROJECTION_REFERENCES = 256
MAX_CANDIDATE_BYTES = 1_048_576
LANES = ("blocks", "history", "continuation", "query", "tools", "artifacts")

# The closed vocabulary is part of the profile revision, not caller-supplied text.
type PublicToken = Literal[
    "Build ",
    " passed",
    " failed",
    " depends on ",
    "node-a",
    "node-b",
    "node-c",
    "; ",
    "\n",
    "owner: ",
    "token: ",
    "diagnostic: ",
    "E001",
    "E002",
    "citation: ",
    "[1]",
    "[2]",
    "query: ",
    "Explain the failure.",
]


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in cast("dict[str, Any]", value).items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in cast("tuple[Any, ...] | list[Any]", value)]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in cast("set[Any] | frozenset[Any]", value)), key=str)
    if isinstance(value, UUID):
        return str(value)
    return value


def canonical_bytes(value: Any) -> bytes:
    """Profile-local JSON encoding, with stable set ordering; not portable JCS."""
    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def bytes_digest(value: bytes) -> str:
    return sha256(value).hexdigest()


class SensitiveSpan(PrivacyValue):
    """Exact source character range; no raw value enters a receipt."""

    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, ge=1)]
    category: PrivacyCategory


class UtilitySpan(PrivacyValue):
    """An exact invariant from this revision's closed public vocabulary."""

    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, ge=1)]
    token: PublicToken


class TextMaterial(PrivacyValue):
    """Transient source: every character must belong to a declared exact span."""

    source_id: UUID
    text: Annotated[str, Field(min_length=1, max_length=MAX_MATERIAL_CHARS, repr=False)]
    label: PrivatizationLabel
    sensitive_spans: Annotated[tuple[SensitiveSpan, ...], Field(max_length=MAX_MATERIAL_CHARS)] = ()
    utility_spans: Annotated[tuple[UtilitySpan, ...], Field(max_length=MAX_MATERIAL_CHARS)] = ()


class WholeProjection(PrivacyValue):
    """The complete outbound schema; there is no free metadata or hidden history."""

    profile: Literal["closed-diagnostic@1"] = PROFILE
    blocks: Annotated[tuple[TextMaterial, ...], Field(max_length=MAX_MATERIALS)] = ()
    history: Annotated[tuple[TextMaterial, ...], Field(max_length=MAX_MATERIALS)] = ()
    continuation: Annotated[tuple[TextMaterial, ...], Field(max_length=MAX_MATERIALS)] = ()
    query: Annotated[tuple[TextMaterial, ...], Field(max_length=MAX_MATERIALS)] = ()
    tools: Annotated[tuple[TextMaterial, ...], Field(max_length=MAX_MATERIALS)] = ()
    artifacts: Annotated[tuple[TextMaterial, ...], Field(max_length=MAX_MATERIALS)] = ()

    def materials(self) -> tuple[TextMaterial, ...]:
        return tuple(material for lane in LANES for material in getattr(self, lane))

    def influence(self) -> PrivatizationLabel:
        return join_labels(
            tuple(
                material.label.model_copy(
                    update={"material_parents": material.label.material_parents | {material.source_id}}
                )
                for material in self.materials()
            )
        )


class WireProjection(PrivacyValue):
    """Closed fresh candidate branch, without lineage or provider cache identity."""

    profile: Literal["closed-diagnostic@1"] = PROFILE
    blocks: tuple[str, ...]
    history: tuple[str, ...]
    continuation: tuple[str, ...]
    query: tuple[str, ...]
    tools: tuple[str, ...]
    artifacts: tuple[str, ...]


class CutBinding(PrivacyValue):
    """Trusted host identities and exact target digest, never a caller grant."""

    consumer_kind: Literal["portal", "a2a", "delegated-runtime", "export"]
    consumer_id: UUID
    principal_id: UUID
    sigil_digest: Digest
    run_id: UUID
    occurrence_id: UUID
    attempt_id: UUID
    purpose_id: UUID
    target_digest: Digest


class PreparationPolicy(PrivacyValue):
    """Pinned policy for a finite fixture, not authority to send it."""

    revision: Literal["closed-diagnostic-policy@1"] = "closed-diagnostic-policy@1"
    prohibited_categories: frozenset[PrivacyCategory] = frozenset({PrivacyCategory.SECRET})
    max_lifetime_seconds: Annotated[int, Field(strict=True, ge=1, le=3600)] = 600
    max_disclosures: Annotated[int, Field(strict=True, ge=1, le=16)] = 1


class DisclosurePlan(PrivacyValue):
    schema_version: Literal["DisclosurePlan@1"] = "DisclosurePlan@1"
    profile: Literal["closed-diagnostic@1"] = PROFILE
    binding: CutBinding
    source_digest: Digest
    source_manifest_digest: Digest
    influence_label_digest: Digest
    restricted_local_ref: UUID
    semantic_invariants_digest: Digest
    threat_model: Literal["declared-span-removal-closed-vocabulary@1"] = "declared-span-removal-closed-vocabulary@1"
    retention_profile: Literal["no-retention-no-training@1"] = "no-retention-no-training@1"
    policy_digest: Digest
    max_disclosures: Annotated[int, Field(strict=True, ge=1, le=16)] = 1
    expires_at: Epoch


class OperationCount(PrivacyValue):
    category: PrivacyCategory
    count: Annotated[int, Field(strict=True, ge=1)]


class TransformationReceipt(PrivacyValue):
    """Immutable non-authorizing evidence, retained only in restricted custody."""

    revision: Literal["closed-diagnostic-censor@1"] = CENSOR
    sequence: Annotated[int, Field(strict=True, ge=0)]
    operation: Literal["validate-closed-grammar", "replace-sensitive-spans"]
    source_digest: Digest
    plan_digest: Digest
    policy_digest: Digest
    influence_label_digest: Digest
    input_digest: Digest
    output_digest: Digest
    parent_receipt_digests: tuple[Digest, ...]
    operations: tuple[OperationCount, ...]


class CutVerification(PrivacyValue):
    schema_version: Literal["CutVerification@1"] = "CutVerification@1"
    verifier_revision: Literal["closed-diagnostic-verifier@1"] = VERIFIER
    source_digest: Digest
    source_manifest_digest: Digest
    plan_digest: Digest
    influence_label_digest: Digest
    candidate_digest: Digest
    receipt_chain_digest: Digest
    terminal_receipt_digest: Digest
    policy_digest: Digest
    privacy_verdict: Literal["declared-spans-absent"] = "declared-spans-absent"
    utility_verdict: Literal["exact-closed-grammar-preserved"] = "exact-closed-grammar-preserved"
    verified_at: Epoch
    expires_at: Epoch


class PrivacyCut(PrivacyValue):
    """Process-local authenticated preparation evidence; never a bearer permit."""

    schema_version: Literal["PrivacyCut@1"] = "PrivacyCut@1"
    profile: Literal["closed-diagnostic@1"] = PROFILE
    cut_id: UUID
    plan: DisclosurePlan
    verification: CutVerification
    candidate_digest: Digest
    receipt_chain_digest: Digest
    terminal_receipt_digest: Digest
    issued_at: Epoch
    expires_at: Epoch
    seal: Digest


class PublicPrivacyEvent(PrivacyValue):
    """The sole broad-retention projection: no source/content hashes or metadata."""

    event_id: UUID


class PreparedCut(PrivacyValue):
    candidate: bytes = Field(repr=False)
    cut: PrivacyCut
    receipts: tuple[TransformationReceipt, ...]
    verification: CutVerification


def manifest(projection: WholeProjection) -> dict[str, Any]:
    return {
        lane: tuple(
            {"source_id": material.source_id, "source_digest": digest(material)}
            for material in getattr(projection, lane)
        )
        for lane in LANES
    }


def semantic_invariants(projection: WholeProjection) -> dict[str, Any]:
    return {lane: tuple(tuple(material.utility_spans) for material in getattr(projection, lane)) for lane in LANES}


def operation_counts(projection: WholeProjection) -> tuple[OperationCount, ...]:
    counts: dict[PrivacyCategory, int] = {}
    for material in projection.materials():
        for span in material.sensitive_spans:
            counts[span.category] = counts.get(span.category, 0) + 1
    return tuple(OperationCount(category=category, count=counts[category]) for category in sorted(counts))


def validate_source(projection: WholeProjection, policy: PreparationPolicy) -> None:
    """Refuse unknown influence and uncovered text before applying a transformation."""
    materials = projection.materials()
    if not materials or len(materials) > MAX_MATERIALS:
        msg = "material-count"
        raise PrivacyRefused(msg)
    if sum(len(material.text) for material in materials) > MAX_PROJECTION_CHARS:
        msg = "projection-bound"
        raise PrivacyRefused(msg)
    if (
        sum(1 + len(material.label.subjects) + len(material.label.material_parents) for material in materials)
        > MAX_PROJECTION_REFERENCES
    ):
        msg = "projection-reference-bound"
        raise PrivacyRefused(msg)
    source_ids = tuple(material.source_id for material in materials)
    if len(set(source_ids)) != len(source_ids):
        msg = "duplicate-source"
        raise PrivacyRefused(msg)
    for material in materials:
        _validate_material(material, policy)


def _validate_material(material: TextMaterial, policy: PreparationPolicy) -> None:
    if not material.label.lineage_known:
        msg = "unknown-lineage"
        raise PrivacyRefused(msg)
    if material.label.categories & policy.prohibited_categories:
        msg = "prohibited-category"
        raise PrivacyRefused(msg)
    if HandlingConstraint.LOCAL_ONLY in material.label.constraints:
        msg = "local-only"
        raise PrivacyRefused(msg)
    declared = frozenset(span.category for span in material.sensitive_spans)
    if declared != material.label.categories:
        msg = "category-coverage"
        raise PrivacyRefused(msg)
    spans = sorted((*material.sensitive_spans, *material.utility_spans), key=lambda span: span.start)
    cursor = 0
    for span in spans:
        if span.start != cursor or span.end <= span.start or span.end > len(material.text):
            msg = "span-partition"
            raise PrivacyRefused(msg)
        if isinstance(span, UtilitySpan) and material.text[span.start : span.end] != span.token:
            msg = "utility-token"
            raise PrivacyRefused(msg)
        cursor = span.end
    if cursor != len(material.text):
        msg = "span-partition"
        raise PrivacyRefused(msg)
    _validate_utility(material)
    try:
        material.text.encode("utf-8")
    except UnicodeError:
        msg = "source-encoding"
        raise PrivacyRefused(msg) from None


DIAGNOSTIC_TOKEN_COUNT = 13
DIAGNOSTIC_OWNER_SLOT = 7


def _validate_utility(material: TextMaterial) -> None:
    """Pin the physical grammar; a sensitive modifier cannot replace task meaning."""
    spans = sorted(material.utility_spans, key=lambda span: span.start)
    tokens = tuple(span.token for span in spans)
    nodes = {"node-a", "node-b", "node-c"}
    if (
        len(tokens) != DIAGNOSTIC_TOKEN_COUNT
        or len(material.sensitive_spans) != 1
        or tokens[0] != "Build "
        or tokens[1] not in nodes
        or tokens[2] != " depends on "
        or tokens[3] not in nodes
        or tokens[1] == tokens[3]
        or tokens[4] not in {" passed", " failed"}
        or tokens[5:9] != ("; ", "owner: ", "; ", "diagnostic: ")
        or tokens[9] not in {"E001", "E002"}
        or tokens[10:12] != ("; ", "citation: ")
        or tokens[12] not in {"[1]", "[2]"}
    ):
        msg = "required-diagnostic-utility"
        raise PrivacyRefused(msg)
    cursor = 0
    sensitive = material.sensitive_spans[0]
    for index, span in enumerate(spans):
        if index == DIAGNOSTIC_OWNER_SLOT:
            if sensitive.start != cursor:
                msg = "diagnostic-sensitive-slot"
                raise PrivacyRefused(msg)
            cursor = sensitive.end
        if span.start != cursor:
            msg = "diagnostic-physical-grammar"
            raise PrivacyRefused(msg)
        cursor = span.end


def validate_plan(projection: WholeProjection, plan: DisclosurePlan, policy: PreparationPolicy, now: int) -> None:
    validate_source(projection, policy)
    if not 0 <= now < plan.expires_at <= now + policy.max_lifetime_seconds:
        msg = "expiry"
        raise PrivacyRefused(msg)
    if plan.max_disclosures > policy.max_disclosures:
        msg = "disclosure-limit"
        raise PrivacyRefused(msg)
    if (
        plan.source_digest != digest(projection)
        or plan.source_manifest_digest != digest(manifest(projection))
        or plan.influence_label_digest != digest(projection.influence())
        or plan.semantic_invariants_digest != digest(semantic_invariants(projection))
        or plan.policy_digest != digest(policy)
    ):
        msg = "plan-binding"
        raise PrivacyRefused(msg)
