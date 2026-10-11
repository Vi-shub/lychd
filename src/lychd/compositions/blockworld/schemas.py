"""Closed wire contracts for one retained placement and its checkpoint.

Original scalar values are checked before Pydantic conversion. The owned state
and result serializers also revalidate live fields before invoking their handler.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationInfo,
    model_serializer,
    model_validator,
)

_DOCUMENT_LIMIT = 65536
_DIMENSIONS = 3
_MAP_LIMIT = 32


class PlacementInputError(ValueError):
    """An original placement document is outside its closed grammar."""


class PlacementConflictError(ValueError):
    """An immutable identity was presented with different contents."""


class PlacementIntegrityError(ValueError):
    """Retained placement custody is inconsistent."""


class PlacementEvidenceError(PlacementIntegrityError):
    """Incoming observations do not prove the retained placement postcondition."""


class PlacementUnavailableError(RuntimeError):
    """Retained custody cannot currently be established."""


class PlacementNotFoundError(PlacementUnavailableError):
    """A successful custody read confirmed that the requested record is absent."""


class PlacementCommitUnknownError(PlacementUnavailableError):
    """A mutation may have committed; resolve its identity before proceeding."""


def _text(value: object, maximum: int = 200) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        msg = "Invalid bounded text."
        raise PlacementInputError(msg)
    return value


def _uuid(value: object) -> str:
    value = _text(value)
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value) is None:
        msg = "UUID must use canonical lower-case spelling."
        raise PlacementInputError(msg)
    UUID(value)
    return value


def _digest(value: object) -> str:
    value = _text(value)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        msg = "Invalid SHA256."
        raise PlacementInputError(msg)
    return value


def _integer(value: object) -> int:
    if type(value) is not int or not -(2**63) <= value < 2**63:
        msg = "Expected an exact signed 64-bit integer."
        raise PlacementInputError(msg)
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        msg = "Expected an exact boolean."
        raise PlacementInputError(msg)
    return value


def _instant(value: object) -> str:
    value = _text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value) is None:
        msg = "Expected canonical UTC RFC3339 time."
        raise PlacementInputError(msg)
    datetime.fromisoformat(value)
    return value


def utc(value: str) -> datetime:
    """Parse a previously validated canonical UTC instant."""
    return datetime.fromisoformat(_instant(value))


def _coordinates(value: Any, info: ValidationInfo) -> tuple[int, int, int]:
    expected = (list, tuple) if info.context and info.context.get("live") else (list,)
    if type(value) not in expected or len(value) != _DIMENSIONS:
        msg = "Expected three integer coordinates."
        raise PlacementInputError(msg)
    for coordinate in value:
        if type(coordinate) is not int or not -(2**31) <= coordinate < 2**31:
            msg = "Coordinate must be an exact signed 32-bit integer."
            raise PlacementInputError(msg)
    return cast("tuple[int, int, int]", tuple(value))


def _strings(value: Any, info: ValidationInfo) -> tuple[str, ...]:
    expected = (list, tuple) if info.context and info.context.get("live") else (list,)
    if type(value) not in expected or len(value) > _MAP_LIMIT:
        msg = "Expected a bounded sorted unique string list."
        raise PlacementInputError(msg)
    for item in value:
        _text(item)
    if list(value) != sorted(set(value)):
        msg = "Strings must be sorted and unique."
        raise PlacementInputError(msg)
    return tuple(value)


def _properties(value: Any, info: ValidationInfo) -> Mapping[str, str]:
    expected = (dict, MappingProxyType) if info.context and info.context.get("live") else (dict,)
    if type(value) not in expected or len(value) > _MAP_LIMIT:
        msg = "Expected at most 32 block properties."
        raise PlacementInputError(msg)
    for key, item in value.items():
        _text(key, 100)
        _text(item, 100)
    return MappingProxyType(dict(value))


Identifier = Annotated[str, BeforeValidator(_text)]
UUIDString = Annotated[str, BeforeValidator(_uuid)]
SHA256 = Annotated[str, BeforeValidator(_digest)]
Integer = Annotated[int, BeforeValidator(_integer)]
Nonnegative = Annotated[Integer, Field(ge=0)]
Positive = Annotated[Integer, Field(ge=1)]
ExactBool = Annotated[bool, BeforeValidator(_bool)]
UtcInstant = Annotated[str, BeforeValidator(_instant)]
Coordinates = Annotated[tuple[int, int, int], BeforeValidator(_coordinates)]
Strings = Annotated[tuple[str, ...], BeforeValidator(_strings)]
Properties = Annotated[MappingProxyType[str, str], BeforeValidator(_properties), PlainSerializer(dict)]
One = Annotated[Literal[1], BeforeValidator(_integer)]
Zero = Annotated[Literal[0], BeforeValidator(_integer)]
MinusOne = Annotated[Literal[-1], BeforeValidator(_integer)]
ActionStatus = Literal["prepared", "pending", "not_issued", "unverified_effect", "verified"]
RefusalCode = Literal[
    "consent_denied",
    "consent_expired",
    "consent_cancelled",
    "authority_revoked",
    "policy_changed",
    "mission_changed",
    "identity_changed",
    "epoch_changed",
    "lease_invalid",
    "cursor_changed",
    "target_changed",
    "inventory_changed",
    "deadline_expired",
    "budget_exhausted",
    "proof_capability_missing",
    "required_record_missing",
]


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            msg = "Duplicate JSON field."
            raise PlacementInputError(msg)
        result[key] = value
    return result


def load_document(value: bytes) -> dict[str, Any]:
    """Decode bounded original JSON without accepting duplicate keys or NaN."""
    if type(value) is not bytes or len(value) > _DOCUMENT_LIMIT:
        msg = "Placement document exceeds 64 KiB."
        raise PlacementInputError(msg)
    try:
        result = json.loads(
            value,
            object_pairs_hook=_json_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(PlacementInputError("Non-finite JSON value.")),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        msg = "Invalid placement JSON."
        raise PlacementInputError(msg) from exc
    if type(result) is not dict:
        msg = "Expected a placement object."
        raise PlacementInputError(msg)
    return cast("dict[str, Any]", result)


class _PlacementDocument(BaseModel):
    """Common strict configuration for this finite set of placement records."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, arbitrary_types_allowed=True, revalidate_instances="always"
    )

    @model_validator(mode="before")
    @classmethod
    def closed_fields(cls, value: Any) -> Any:
        if type(value) is cls:
            value = value.__dict__
        if type(value) is not dict or value.keys() != cls.model_fields.keys():
            msg = "Missing or unknown placement fields."
            raise PlacementInputError(msg)
        document = cast("dict[str, Any]", value)
        if any(type(key) is not str for key in document):
            msg = "Placement field names must be exact strings."
            raise PlacementInputError(msg)
        for item in document.values():
            if isinstance(item, (str, int, float)) and type(item) not in (str, int, float, bool):
                msg = "Placement scalar subclasses are not wire values."
                raise PlacementInputError(msg)
        return cast("dict[str, Any]", value)

    @model_serializer(mode="wrap")
    def checked_serialization(self, handler: Any) -> Any:
        type(self).model_validate(dict(self.__dict__), context={"live": True})
        return handler(self)

    @classmethod
    def model_validate_json(cls, json_data: Any, **kwargs: Any) -> Any:
        raw = json_data.encode() if type(json_data) is str else json_data
        return cls.model_validate(load_document(raw), **kwargs)


class BlockState(_PlacementDocument):
    name: Identifier
    properties: Properties


class InventoryChange(_PlacementDocument):
    slot: Identifier
    item: Identifier
    before_count: Positive
    delta: MinusOne
    after_count: Nonnegative

    @model_validator(mode="after")
    def conserve(self) -> InventoryChange:
        if self.after_count != self.before_count - 1:
            msg = "Inventory delta is inconsistent."
            raise PlacementInputError(msg)
        return self


class Principal(_PlacementDocument):
    name: Identifier
    scopes: Strings


class Revision(_PlacementDocument):
    key: Identifier
    revision: Identifier


class PatternRevision(Revision):
    manifest_digest: SHA256


class Plot(_PlacementDocument):
    id: UUIDString
    min: Coordinates
    max: Coordinates

    @model_validator(mode="after")
    def ordered(self) -> Plot:
        if any(low > high for low, high in zip(self.min, self.max, strict=True)):
            msg = "Invalid plot bounds."
            raise PlacementInputError(msg)
        return self


class Lease(_PlacementDocument):
    id: UUIDString
    generation: Nonnegative
    expires_at: UtcInstant


class AdmittedLease(Lease):
    bot_id: UUIDString
    plot_id: UUIDString
    effect: Literal["place_block"]


class MissionPolicy(_PlacementDocument):
    id: UUIDString
    generation: Nonnegative


class PlacementRequestV1(_PlacementDocument):
    schema_version: One
    request_id: UUIDString
    mission_id: UUIDString
    action_id: UUIDString
    expected_cursor: Zero
    target: Coordinates
    block_state: BlockState
    inventory_change: InventoryChange


class Postcondition(_PlacementDocument):
    target: Coordinates
    block_state: BlockState
    inventory_after: Nonnegative
    cursor: One
    proof: Literal["matching_response_and_authoritative_event"]


class PreparedPlacementV1(_PlacementDocument):
    schema_version: One
    operation: Literal["place_block"]
    request_id: UUIDString
    mission_id: UUIDString
    action_id: UUIDString
    run_id: UUIDString
    principal: Principal
    composition: Revision
    pattern: PatternRevision
    adapter: Revision
    mode: Literal["managed"]
    server_id: UUIDString
    world_id: UUIDString
    world_epoch: Nonnegative
    bot_id: UUIDString
    capability_revision: Identifier
    blueprint_id: UUIDString
    blueprint_digest: SHA256
    plot: Plot
    lease: Lease
    mission_policy: MissionPolicy
    authority_generation: Nonnegative
    target: Coordinates
    block_state: BlockState
    target_before_digest: SHA256
    inventory_generation: Nonnegative
    inventory_digest: SHA256
    inventory_change: InventoryChange
    expected_cursor: Zero
    deadline: UtcInstant
    placement_budget: One
    postcondition: Postcondition

    @model_validator(mode="after")
    def complete(self) -> PreparedPlacementV1:
        if (
            (self.composition.key, self.composition.revision) != ("blockworld.inhabitant", "2")
            or (self.pattern.key, self.pattern.revision) != ("blockworld.bounded_mission", "2")
            or (self.adapter.key, self.adapter.revision) != ("inert-placement", "1")
            or self.postcondition.target != self.target
            or self.postcondition.block_state != self.block_state
            or self.postcondition.inventory_after != self.inventory_change.after_count
            or any(not low <= x <= high for x, low, high in zip(self.target, self.plot.min, self.plot.max, strict=True))
        ):
            msg = "Prepared placement contract differs from admitted placement."
            raise PlacementInputError(msg)
        return self


class VerifiedResult(_PlacementDocument):
    kind: Literal["verified"]
    action_id: UUIDString
    receipt_id: UUIDString
    receipt_digest: SHA256
    cursor: One
    placements_remaining: Zero


class NotIssuedResult(_PlacementDocument):
    kind: Literal["not_issued"]
    action_id: UUIDString
    reason: RefusalCode
    cursor: Zero
    placements_remaining: One


class UnverifiedEffectResult(_PlacementDocument):
    kind: Literal["unverified_effect"]
    action_id: UUIDString
    reason: Literal["acknowledgement_unknown", "insufficient_attribution", "reconciliation_unavailable"]
    cursor: Zero
    placements_remaining: One


PlacementResult = Annotated[VerifiedResult | NotIssuedResult | UnverifiedEffectResult, Field(discriminator="kind")]


class PlacementStateV1(_PlacementDocument):
    model_config = ConfigDict(
        extra="forbid", frozen=False, strict=True, arbitrary_types_allowed=True, revalidate_instances="always"
    )
    schema_version: One
    run_id: UUIDString
    session_id: Identifier
    action_id: UUIDString | None
    preparation_digest: SHA256 | None
    pending_consent_id: UUIDString | None
    native_binding_digest: SHA256 | None
    result: PlacementResult | None

    @model_validator(mode="after")
    def references(self) -> PlacementStateV1:
        refs = (self.action_id, self.preparation_digest, self.pending_consent_id, self.native_binding_digest)
        if any(item is None for item in refs) and any(item is not None for item in refs):
            msg = "Prepared references must be complete."
            raise PlacementInputError(msg)
        if self.result is not None and self.action_id is not None and self.result.action_id != self.action_id:
            msg = "Result identity differs from prepared action."
            raise PlacementInputError(msg)
        if self.action_id is None and self.result is not None and self.result.kind != "not_issued":
            msg = "An effect result requires retained preparation."
            raise PlacementInputError(msg)
        return self


class EffectResponse(_PlacementDocument):
    action_id: UUIDString
    preparation_digest: SHA256
    server_id: UUIDString
    world_id: UUIDString
    world_epoch: Nonnegative
    bot_id: UUIDString
    response_id: Identifier
    accepted: Annotated[Literal[True], BeforeValidator(_bool)]


class AuthoritativeEvent(_PlacementDocument):
    action_id: UUIDString
    preparation_digest: SHA256
    server_id: UUIDString
    world_id: UUIDString
    world_epoch: Nonnegative
    bot_id: UUIDString
    event_id: Identifier
    event_sequence: Nonnegative
    target: Coordinates
    block_state: BlockState
    inventory_after: Nonnegative
    cursor_before: Zero
    cursor_after: One


class PlacementEvidence(_PlacementDocument):
    kind: Literal["observed"]
    response: EffectResponse
    event: AuthoritativeEvent


class UnknownEvidence(_PlacementDocument):
    kind: Literal["unknown"]
    action_id: UUIDString
    preparation_digest: SHA256


class PlacementReceiptV1(_PlacementDocument):
    schema_version: One
    receipt_id: UUIDString
    action_id: UUIDString
    preparation_digest: SHA256
    server_id: UUIDString
    world_id: UUIDString
    world_epoch: Nonnegative
    bot_id: UUIDString
    adapter_revision: Literal["1"]
    response_id: Identifier
    authoritative_event_id: Identifier
    event_sequence: Nonnegative
    target: Coordinates
    block_state: BlockState
    inventory_after: Nonnegative
    cursor_before: Zero
    cursor_after: One
    placement_cost: One
    evidence_digest: SHA256


class CurrentPlacementFacts(_PlacementDocument):
    principal_name: Identifier
    effect_allowed: ExactBool
    authority_generation: Nonnegative
    mission_id: UUIDString
    mission_active: ExactBool
    mission_policy_id: UUIDString
    mission_policy_generation: Nonnegative
    server_id: UUIDString
    world_id: UUIDString
    world_epoch: Nonnegative
    bot_id: UUIDString
    capability_revision: Identifier
    can_place: ExactBool
    can_prove: ExactBool
    lease_id: UUIDString
    lease_generation: Nonnegative
    lease_active: ExactBool
    lease_expires_at: UtcInstant
    target_before_digest: SHA256
    inventory_generation: Nonnegative
    inventory_digest: SHA256
    inventory_count: Nonnegative
    cursor: Nonnegative
    placements_remaining: Nonnegative


class ReviewEligibility(_PlacementDocument):
    disclose_complete: ExactBool
    judge: ExactBool
    audience_digest: SHA256


def _manifest_document(value: MappingProxyType[str, Any]) -> dict[str, Any]:
    return {
        **dict(value),
        "nodes": [dict(node) for node in value["nodes"]],
        "edges": [dict(edge) for edge in value["edges"]],
    }


def _manifest(value: Any, info: ValidationInfo) -> MappingProxyType[str, Any]:
    from lychd.agents.workflows.base import pattern_snapshot_is_valid

    if type(value) is MappingProxyType and info.context and info.context.get("live"):
        value = _manifest_document(cast("MappingProxyType[str, Any]", value))
    document = cast("dict[str, Any]", value)
    if (
        type(cast("Any", value)) is not dict
        or type(document.get("schema_version")) is not int
        or not pattern_snapshot_is_valid(document)
    ):
        msg = "Invalid immutable Pattern snapshot."
        raise PlacementInputError(msg)
    return MappingProxyType(
        {
            **document,
            "nodes": tuple(MappingProxyType(dict(node)) for node in document["nodes"]),
            "edges": tuple(MappingProxyType(dict(edge)) for edge in document["edges"]),
        }
    )


FrozenManifest = Annotated[MappingProxyType[str, Any], BeforeValidator(_manifest), PlainSerializer(_manifest_document)]


class AdmittedPlacementV1(_PlacementDocument):
    schema_version: One
    request: PlacementRequestV1
    principal: Principal
    pattern_snapshot: FrozenManifest
    server_id: UUIDString
    world_id: UUIDString
    world_epoch: Nonnegative
    bot_id: UUIDString
    capability_revision: Identifier
    capabilities: Strings
    blueprint_id: UUIDString
    blueprint_digest: SHA256
    plot: Plot
    lease: AdmittedLease
    mission_policy: MissionPolicy
    authority_generation: Nonnegative
    audience: Strings
    audience_digest: SHA256
    deadline: UtcInstant
    target_before_digest: SHA256
    inventory_generation: Nonnegative
    inventory_digest: SHA256
    judgment_mode: Literal["live", "exact_preauth", "exact_preauth_or_live"]
    policy_id: UUIDString | None

    @model_validator(mode="after")
    def fixture(self) -> AdmittedPlacementV1:
        from lychd.agents.workflows.base import pattern_snapshot_is_valid

        if (
            not pattern_snapshot_is_valid(_manifest_document(self.pattern_snapshot))
            or self.pattern_snapshot["key"] != "blockworld.bounded_mission"
            or self.pattern_snapshot["revision"] != "2"
            or (self.judgment_mode != "live" and self.policy_id is None)
            or self.capabilities != ("place_block",)
            or self.lease.bot_id != self.bot_id
            or self.lease.plot_id != self.plot.id
        ):
            msg = "Invalid admitted Pattern or exact policy identity."
            raise PlacementInputError(msg)
        return self


class AdmittedResult(_PlacementDocument):
    kind: Literal["admitted"]
    request_id: UUIDString
    mission_id: UUIDString
    run_id: UUIDString


class AdmissionNoncompletion(_PlacementDocument):
    kind: Literal["admission_noncompletion"]
    request_id: UUIDString
    mission_id: UUIDString
    run_id: UUIDString
    binding: Literal["unbound", "bound"]
    reason: Literal["retention_failed", "run_terminal"]


class AdmissionUnresolved(_PlacementDocument):
    kind: Literal["admission_unresolved"]
    request_id: UUIDString
    mission_id: UUIDString
    run_id: UUIDString | None
    reason: Literal["custody_unavailable"]


class RetainedActionResult(_PlacementDocument):
    kind: Literal["retained_action"]
    request_id: UUIDString
    mission_id: UUIDString
    run_id: UUIDString
    action_id: UUIDString
    status: ActionStatus


class MissionNoncompletion(_PlacementDocument):
    kind: Literal["mission_noncompletion"]
    request_id: UUIDString
    mission_id: UUIDString
    run_id: UUIDString | None
    result: NotIssuedResult


PlacementAdmissionResult = Annotated[
    AdmittedResult | AdmissionNoncompletion | AdmissionUnresolved | RetainedActionResult | MissionNoncompletion,
    Field(discriminator="kind"),
]


def canonical_bytes(value: BaseModel) -> bytes:
    """Encode checked owned values using the placement canonical JSON rules."""
    data = value.model_dump(mode="json")
    result = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(result) > _DOCUMENT_LIMIT:
        msg = "Placement document exceeds 64 KiB."
        raise PlacementInputError(msg)
    return result


def placement_digest(value: bytes) -> str:
    return sha256(b"lychd.blockworld.placement.v1\n" + value).hexdigest()


def evidence_digest(value: bytes) -> str:
    return sha256(b"lychd.blockworld.evidence.v1\n" + value).hexdigest()


def receipt_digest(value: bytes) -> str:
    return sha256(b"lychd.blockworld.receipt.v1\n" + value).hexdigest()


def decode_request(value: bytes | dict[str, Any]) -> PlacementRequestV1:
    try:
        if type(value) is dict:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > _DOCUMENT_LIMIT:
                msg = "Placement document exceeds 64 KiB."
                raise PlacementInputError(msg)
            document = value
        else:
            document = load_document(cast("bytes", value))
        return PlacementRequestV1.model_validate(document)
    except (ValueError, TypeError) as exc:
        msg = "Invalid placement request."
        raise PlacementInputError(msg) from exc


def decode_command(value: bytes) -> PreparedPlacementV1:
    try:
        command = PreparedPlacementV1.model_validate(load_document(value))
        if canonical_bytes(command) != value:
            msg = "Retained command is not canonical."
            raise PlacementIntegrityError(msg)
    except (ValueError, TypeError) as exc:
        msg = "Invalid retained command."
        raise PlacementIntegrityError(msg) from exc
    else:
        return command


def validate_judgment_document(value: Mapping[str, Any]) -> None:
    """Check the complete retained first-decision record before semantic use."""
    keys = {
        "consent_id",
        "binding_digest",
        "verdict",
        "decided_by",
        "decided_at",
        "policy_id",
        "policy_slug",
        "policy_digest",
    }
    if set(value) != keys or value["verdict"] != "granted":
        msg = "Invalid retained judgment document."
        raise PlacementIntegrityError(msg)
    _uuid(value["consent_id"])
    _digest(value["binding_digest"])
    _text(value["decided_by"])
    _instant(value["decided_at"])
    for key, validator in (("policy_id", _uuid), ("policy_slug", _text), ("policy_digest", _digest)):
        if value[key] is not None:
            validator(value[key])
