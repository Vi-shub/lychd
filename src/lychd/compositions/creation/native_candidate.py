"""Bounded inactive candidate declarations; no execution, factual or effect authority."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from lychd.domain.artifact_custody import (
    Admission,
    ArtifactRecord,
    CustodyValue,
    Digest,
    Label,
    canonical_bytes,
    digest_bytes,
    record_digest,
)
from lychd.domain.evolution.native_candidate import NativeCandidateBundle, decode_native_candidate

PLAN_BYTES = 12_288
BINDING_BYTES = 16_384
REPORT_BYTES = 8192

NATIVE_PROFILE = {"composition": "creation.native-candidate", "profile": "native-candidate-inactive@1", "revision": "1"}


class NativePlan(CustodyValue):
    candidate_id: UUID
    proposer: UUID
    verifier: UUID
    decision_owner: UUID
    policy_generations: tuple[tuple[UUID, int], ...] = Field(min_length=2, max_length=3)
    bundle_digest: Digest
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    selected_paths: tuple[str, ...] = Field(min_length=1, max_length=128)
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=128)
    closure_digest: Digest
    policy_digest: Digest
    history_digest: Digest
    recovery_digest: Digest
    criteria: tuple[Label, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def closed_plan(self) -> NativePlan:
        if (
            self.proposer == self.verifier
            or len(set(self.criteria)) != len(self.criteria)
            or len(dict(self.policy_generations)) != len(self.policy_generations)
            or set(dict(self.policy_generations)) != {self.proposer, self.verifier, self.decision_owner}
            or any(type(value) is not int or value < 1 for _, value in self.policy_generations)
            or len(canonical_bytes(self)) > PLAN_BYTES
        ):
            message = "Native plan is not closed or independently assigned."
            raise ValueError(message)
        return self

    def inspect(self, record: ArtifactRecord, content: bytes) -> NativeCandidateBundle:
        """Compare literal retained bytes to the known host plan; base remains a declaration."""
        bundle = decode_native_candidate(record.reference, content)
        if (
            record.request_id != self.candidate_id
            or record.reference.digest != self.bundle_digest
            or bundle.base_commit != self.base_commit
            or bundle.selected_paths != self.selected_paths
            or bundle.allowed_paths != self.allowed_paths
            or record_digest(bundle.closure) != self.closure_digest
        ):
            message = "Native candidate differs from its known plan."
            raise ValueError(message)
        return bundle


class NativeRequest(CustodyValue):
    case_id: UUID
    admission_id: UUID
    plan: NativePlan
    deadline: datetime


class NativeBinding(CustodyValue):
    request: NativeRequest
    record: ArtifactRecord
    admission: Admission

    @model_validator(mode="after")
    def exact_binding(self) -> NativeBinding:
        if (
            self.record != self.admission.record
            or self.record.request_id != self.request.plan.candidate_id
            or self.record.reference.digest != self.request.plan.bundle_digest
            or len(canonical_bytes(self)) > BINDING_BYTES
        ):
            message = "Native binding is not exact or bounded."
            raise ValueError(message)
        return self

    @property
    def digest(self) -> str:
        return digest_bytes(canonical_bytes(self))


class NativeObservation(CustodyValue):
    criterion: Label
    outcome: Literal["pass", "fail", "unknown"]
    receipt_digest: Digest | None


class NativeAssessment(CustodyValue):
    assessment_id: UUID
    target_digest: Digest
    report: str = Field(min_length=1, max_length=8192)
    exposure: Literal["attributed-external-report"] = "attributed-external-report"
    limitations: tuple[Label, ...] = Field(min_length=1, max_length=16)
    observations: tuple[NativeObservation, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def bounded_report(self) -> NativeAssessment:
        if len(self.report.encode()) > REPORT_BYTES or len(canonical_bytes(self)) > BINDING_BYTES:
            message = "Native assessment exceeds custody bounds."
            raise ValueError(message)
        return self


class NativeDecision(CustodyValue):
    decision_id: UUID
    target_digest: Digest
    assessment_id: UUID
    assessment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["hold", "reject"]
    reason: str = Field(min_length=1, max_length=1024)


class NativeEntry(CustodyValue):
    identity: UUID
    sequence: int
    body_digest: str
    body: NativeAssessment | NativeDecision
    attribution: tuple[tuple[str, str], ...]


class NativeView(CustodyValue):
    binding: NativeBinding
    entries: tuple[NativeEntry, ...]
    stage: Literal["inactive"] = "inactive"
    authority: Literal["hold-reject-only"] = "hold-reject-only"
