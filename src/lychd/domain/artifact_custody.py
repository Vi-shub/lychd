"""Immutable evidence for the explicit trusted-host artifact custody profile."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lychd.domain.artifacts import ArtifactRef

PROFILE = "same-host-trusted-bootstrap@1"
MAX_PAYLOAD_BYTES = 1_048_576
MAX_METADATA_BYTES = 16_384
MAX_HOLDS = 16
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Label = Annotated[str, Field(min_length=1, max_length=256)]


class CustodyValue(BaseModel):
    """Strict frozen records have a canonical bounded representation on disk."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FileToken(CustodyValue):
    device: int = Field(ge=0)
    inode: int = Field(gt=0)


class RootBinding(CustodyValue):
    profile: Literal["same-host-trusted-bootstrap@1"] = "same-host-trusted-bootstrap@1"
    schema_version: Literal[1] = 1
    store_id: UUID
    owner_uid: int = Field(ge=0)
    root: FileToken


class Subject(CustodyValue):
    """Identity supplied by trusted host composition, not an ingress header."""

    identity: Label


class ImportSpec(CustodyValue):
    request_id: UUID
    owner: Subject
    media_type: Literal["audio/wav"] = "audio/wav"
    classification: Literal["public", "internal", "private", "restricted"] = "private"
    producer_claim: Label | None = None
    source_claim: Label | None = None
    retention: Literal["manual"] = "manual"


class ArtifactRecord(CustodyValue):
    binding: RootBinding
    request_id: UUID
    reference: ArtifactRef
    owner: Subject
    importer: Subject
    producer_claim: Label | None
    source_claim: Label | None
    retention: Literal["manual"] = "manual"
    imported_at: datetime
    import_fingerprint: Digest
    envelope: FileToken

    @model_validator(mode="after")
    def exact_identity(self) -> ArtifactRecord:
        if self.reference.artifact_id != artifact_id(self.binding.store_id, self.request_id):
            raise CustodyRefused(CustodyCode.REFERENCE)
        if self.reference.media_type != "audio/wav" or self.reference.size > MAX_PAYLOAD_BYTES:
            raise CustodyRefused(CustodyCode.INPUT)
        return self


class Action(StrEnum):
    IMPORT = "import"
    INSPECT = "quarantine_inspection"
    ADMIT = "admission"
    READ = "read"
    HOLD = "hold"
    RELEASE = "hold_release"
    DELETE = "delete"


class InspectionEvidence(CustodyValue):
    revision: Label
    facts: tuple[tuple[Label, int], ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_facts(self) -> InspectionEvidence:
        if len({name for name, _ in self.facts}) != len(self.facts):
            raise CustodyRefused(CustodyCode.INPUT)
        return self


type Inspector = Callable[[ArtifactRef, bytes], InspectionEvidence]


class AdmissionTarget(CustodyValue):
    kind: Literal["admission"] = "admission"
    evidence: InspectionEvidence


class HoldTarget(CustodyValue):
    kind: Literal["hold"] = "hold"
    hold_id: UUID
    reason: Label


class RetainedHold(CustodyValue):
    """Exact policy-visible hold facts without recursively copying its artifact."""

    hold_id: UUID
    reason: Label
    subject: Subject
    purpose: Label
    created_at: datetime
    identity: FileToken
    record_digest: Digest
    digest: Digest


class ReleaseTarget(CustodyValue):
    kind: Literal["release"] = "release"
    hold: RetainedHold
    hold_digest: Digest


class HoldReleaseBinding(CustodyValue):
    hold_id: UUID
    hold_digest: Digest
    release_digest: Digest


class DeletionTarget(CustodyValue):
    kind: Literal["delete"] = "delete"
    deletion_id: UUID
    basis: tuple[HoldReleaseBinding, ...] = Field(max_length=MAX_HOLDS)


type MutationTarget = Annotated[
    AdmissionTarget | HoldTarget | ReleaseTarget | DeletionTarget, Field(discriminator="kind")
]


class AuthorizationRequest(CustodyValue):
    """A fresh nonce binds the decision to this consultation, object and purpose."""

    nonce: UUID
    subject: Subject
    binding: RootBinding
    reference: ArtifactRef
    owner: Subject
    phase: Literal["proposal", "retained"] = "retained"
    record: ArtifactRecord | None
    proposal: ImportSpec | None = None
    importer: Subject
    import_fingerprint: Digest
    record_digest: Digest | None
    action: Action
    purpose: Label
    target: MutationTarget | None = None

    @model_validator(mode="after")
    def exact_phase(self) -> AuthorizationRequest:
        if self.phase == "proposal":
            if (
                self.action is not Action.IMPORT
                or self.record is not None
                or self.record_digest is not None
                or self.proposal is None
            ):
                raise CustodyRefused(CustodyCode.INPUT)
        elif self.record is None or self.record_digest != record_digest(self.record) or self.proposal is not None:
            raise CustodyRefused(CustodyCode.INPUT)
        targets: dict[Action, type[CustodyValue]] = {
            Action.ADMIT: AdmissionTarget,
            Action.HOLD: HoldTarget,
            Action.RELEASE: ReleaseTarget,
            Action.DELETE: DeletionTarget,
        }
        if self.action in targets:
            if type(self.target) is not targets[self.action]:
                raise CustodyRefused(CustodyCode.INPUT)
        elif self.target is not None:
            raise CustodyRefused(CustodyCode.INPUT)
        return self


class Decision(CustodyValue):
    request: AuthorizationRequest
    allowed: bool


class Authorizer(Protocol):
    """Mandatory synchronous current policy consulted at each protected boundary."""

    def authorize(self, request: AuthorizationRequest) -> Decision: ...


class DecisionContext(CustodyValue):
    """Persist the decision binding once, beside the complete retained record."""

    nonce: UUID
    subject: Subject
    action: Action
    purpose: Label
    record_digest: Digest
    target: MutationTarget


class DecisionBinding(CustodyValue):
    request: DecisionContext
    allowed: Literal[True] = True


def bind_decision(decision: Decision) -> DecisionBinding:
    query = decision.request
    if not decision.allowed or query.record_digest is None or query.target is None:
        raise CustodyRefused(CustodyCode.DENIED)
    return DecisionBinding(
        request=DecisionContext(
            nonce=query.nonce,
            subject=query.subject,
            action=query.action,
            purpose=query.purpose,
            record_digest=query.record_digest,
            target=query.target,
        )
    )


class Control(CustodyValue):
    """Common binding retained by every immutable control receipt."""

    record: ArtifactRecord
    identity: FileToken
    decision: DecisionBinding
    created_at: datetime


class Admission(Control):
    kind: Literal["admission"] = "admission"
    evidence: InspectionEvidence


class Hold(Control):
    kind: Literal["hold"] = "hold"
    hold_id: UUID
    reason: Label


class Release(Control):
    kind: Literal["release"] = "release"
    hold_id: UUID
    hold_digest: Digest


class Tombstone(Control):
    kind: Literal["tombstone"] = "tombstone"
    deletion_id: UUID
    release_digests: tuple[Digest, ...] = Field(max_length=MAX_HOLDS)


class CustodyState(StrEnum):
    QUARANTINED = "quarantined"
    ADMITTED = "admitted"
    DELETED = "deleted"


class CustodyView(CustodyValue):
    record: ArtifactRecord
    state: CustodyState
    admission: Admission | None = None
    holds: tuple[Hold, ...] = ()
    releases: tuple[Release, ...] = ()
    tombstone: Tombstone | None = None

    @property
    def active_holds(self) -> tuple[Hold, ...]:
        released = {release.hold_id for release in self.releases}
        return tuple(hold for hold in self.holds if hold.hold_id not in released)


class RetirementState(StrEnum):
    PAYLOAD_ABSENT = "payload_absent"
    CLEANUP_PENDING = "cleanup_pending"
    RECOVERY_REQUIRED = "recovery_required"


class DeletionResult(CustodyValue):
    tombstone: Tombstone
    retirement: RetirementState


class Continuity(StrEnum):
    CONTINUOUS = "continuous"
    LOST = "lost"
    RESTORED = "restored"
    UNCERTAIN = "uncertain"


class CustodyCode(StrEnum):
    INPUT = "input"
    REFERENCE = "reference"
    DENIED = "denied"
    CONFLICT = "conflict"
    DELETED = "deleted"
    QUARANTINED = "quarantined"
    HELD = "held"
    HOLD_LIMIT = "hold_limit"
    LOCKED = "locked"
    CLOSED = "closed"


class CustodyRefused(ValueError):  # noqa: N818 - named domain refusal contract
    """A fixed, content-free refusal discloses no retained object metadata."""

    def __init__(self, code: CustodyCode) -> None:
        """Retain the fixed refusal category without object material."""
        self.code = code
        super().__init__(f"Artifact custody refused: {code.value}.")


class PublicationOutcome(StrEnum):
    UNCOMMITTED = "uncommitted"
    COMMITTED = "committed"
    RECOVERY_REQUIRED = "recovery_required"


class RecoveryRequired(RuntimeError):  # noqa: N818 - named recovery outcome
    """Preserved filesystem evidence needs explicit recovery outside this profile."""

    def __init__(
        self,
        *,
        outcome: PublicationOutcome = PublicationOutcome.RECOVERY_REQUIRED,
        failures: tuple[BaseException, ...] = (),
    ) -> None:
        """Retain publication truth separately from recovery availability."""
        self.outcome = outcome
        self.failures = failures
        super().__init__("Artifact custody requires recovery; preserved material is unavailable.")


class CustodyPort(Protocol):
    """Trusted extension seam; inspectors are host code, never producer evidence."""

    def admit(
        self,
        record: ArtifactRecord,
        *,
        subject: Subject,
        purpose: str,
        inspector: Inspector,
    ) -> Admission: ...

    def read(self, record: ArtifactRecord, *, subject: Subject, purpose: str) -> bytes: ...


def artifact_id(store_id: UUID, request_id: UUID) -> str:
    return f"urn:lychd:artifact:{store_id}:{request_id}"


def canonical_bytes(value: CustodyValue) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def record_digest(value: CustodyValue) -> str:
    return digest_bytes(canonical_bytes(value))
