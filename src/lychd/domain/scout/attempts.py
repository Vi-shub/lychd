"""Immutable Scout custody facts; no serialized value is execution authority."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lychd.domain.codex.schemas import NativeConsentBinding
from lychd.domain.scout.destination import preflight_destination

if TYPE_CHECKING:
    from lychd.domain.scout.authority import ScoutAction, ScoutAuthorizationDecision

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(min_length=1, max_length=200)]
Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
AttemptStatus = Literal["prepared", "claimed", "unknown_after_crash", "succeeded", "refused"]


def document_digest(value: object) -> str:
    """Hash canonical restricted local evidence, never a public telemetry value."""
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class LocatorSource(_Frozen):
    """One explicit authenticated operator command, asserted by the host issuer."""

    source_id: UUID
    kind: Literal["authenticated_operator_command"] = "authenticated_operator_command"
    url: str = Field(min_length=1, max_length=4096, repr=False)


class LocatorInfluence(_Frozen):
    """Complete public influence assertion for an identified command source."""

    source_id: UUID
    classification: Literal["public"] = "public"
    category: Literal["public-web-locator"] = "public-web-locator"


class PublicLocatorDeclaration(_Frozen):
    """Attributed classification assertion; authentication and policy remain external."""

    url: str = Field(min_length=1, max_length=4096, repr=False)
    subject: Identity = Field(repr=False)
    authentication_generation: UUID = Field(repr=False)
    purpose: Identity
    classification: Literal["public"] = "public"
    category: Literal["public-web-locator"] = "public-web-locator"
    source_kind: Literal["authenticated_operator_command"] = "authenticated_operator_command"
    source_manifest: tuple[LocatorSource, ...] = Field(min_length=1, max_length=16, repr=False)
    influence_manifest: tuple[LocatorInfluence, ...] = Field(min_length=1, max_length=16, repr=False)
    policy_revision: Counter
    declared_at: datetime

    @model_validator(mode="after")
    def exact_sources(self) -> Self:
        """Require complete one-to-one influence attribution for the exact locator."""
        sources = tuple(item.source_id for item in self.source_manifest)
        if (
            len(set(sources)) != len(sources)
            or tuple(item.source_id for item in self.influence_manifest) != sources
            or any(item.url != self.url for item in self.source_manifest)
            or self.declared_at.tzinfo is None
        ):
            message = "Scout declaration source binding is invalid."
            raise ValueError(message)
        preflight_destination(self.url)
        return self


class ScoutAllowance(_Frozen):
    """Finite connect, actual send and wire-byte counters."""

    connects: Counter
    transmissions: Counter
    wire_bytes: Counter

    def within(self, other: ScoutAllowance) -> bool:
        """Require every dimension to fit the retained ceiling."""
        return all(getattr(self, name) <= getattr(other, name) for name in type(self).model_fields)

    def add(self, other: ScoutAllowance) -> ScoutAllowance:
        """Add dimensions with checked signed-64-bit arithmetic."""
        return ScoutAllowance(**{name: getattr(self, name) + getattr(other, name) for name in type(self).model_fields})

    def subtract(self, other: ScoutAllowance) -> ScoutAllowance:
        """Subtract dimensions, refusing underflow."""
        return ScoutAllowance(**{name: getattr(self, name) - getattr(other, name) for name in type(self).model_fields})


class ScoutBudgetLimits(_Frozen):
    """One immutable local profile, with shared host and binding ceilings."""

    host: ScoutAllowance
    binding: ScoutAllowance

    @property
    def digest(self) -> str:
        """Bind configuration across restarts; renaming cannot reset retained usage."""
        return document_digest(self.model_dump(mode="json"))


class ScoutPreparation(_Frozen):
    """Exact authenticated admission and worst-case allowance retained before contact."""

    request_id: UUID
    attempt_id: UUID
    run_id: UUID
    enqueue_seq: Counter
    workflow_name: Identity
    sigil_name: Identity
    capability_key: Identity
    pattern_digest: Digest = Field(repr=False)
    intent_digest: Digest = Field(repr=False)
    subject: Identity = Field(repr=False)
    authentication_generation: UUID = Field(repr=False)
    declaration: PublicLocatorDeclaration = Field(repr=False)
    url: str = Field(min_length=1, max_length=4096, repr=False)
    provider_id: Identity
    profile: Identity
    binding_digest: Digest = Field(repr=False)
    configuration_digest: Digest = Field(repr=False)
    resolver_address: str = Field(min_length=1, max_length=39, repr=False)
    resolver_port: int = Field(strict=True, ge=1, le=65535)
    resolver_custody_digest: Digest = Field(repr=False)
    resolver_custody_statement: str = Field(min_length=1, max_length=4000, repr=False)
    purpose: Identity
    policy_revision: Counter
    revocation_generation: Counter
    expires_at: datetime
    allowance: ScoutAllowance

    @model_validator(mode="after")
    def exact_declaration(self) -> Self:
        """Keep declaration, prepared URL and current host identity inseparable."""
        declaration = self.declaration
        if (
            (
                declaration.url,
                declaration.subject,
                declaration.authentication_generation,
                declaration.purpose,
                declaration.policy_revision,
            )
            != (self.url, self.subject, self.authentication_generation, self.purpose, self.policy_revision)
            or not self.resolver_custody_statement.strip()
            or document_digest(self.resolver_custody_statement) != self.resolver_custody_digest
            or self.expires_at.tzinfo is None
            or declaration.declared_at >= self.expires_at
        ):
            message = "Scout preparation declaration differs."
            raise ValueError(message)
        return self

    def canonical_bytes(self) -> bytes:
        """Serialize restricted immutable preparation custody."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()

    @property
    def digest(self) -> str:
        """Bind the complete retained preparation."""
        return sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ScoutRunBinding:
    """Read-only actual Run facts; prepare and claim still re-read under lock."""

    run_id: UUID
    enqueue_seq: int
    sigil_name: str
    workflow_name: str
    pattern_digest: str = field(repr=False)
    intent_digest: str = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class ScoutAttemptSnapshot:
    """Retained status without recovered transient material or an execution permit."""

    preparation: ScoutPreparation = field(repr=False)
    consent_id: UUID
    native_binding: NativeConsentBinding = field(repr=False)
    status: AttemptStatus
    claim_id: UUID | None = None
    execution_enqueue_seq: int | None = None
    receipt_id: UUID | None = None
    receipt_digest: str | None = field(default=None, repr=False)


@dataclass(frozen=True, kw_only=True)
class NewScoutClaim:
    """Acknowledged fresh claim; only the issuing host may activate it once."""

    preparation: ScoutPreparation = field(repr=False)
    consent_id: UUID
    enqueue_seq: int
    claim_id: UUID
    committed_at: datetime


class ScoutStoreAuthorizer(Protocol):
    """Mandatory current host authority consulted under the transaction boundary."""

    def authorize(self, action: ScoutAction, preparation: ScoutPreparation) -> ScoutAuthorizationDecision:
        """Refuse or return the exact decision from authenticated host state."""
        ...


class ScoutSettlement(_Frozen):
    """Owned completion evidence, accepted only by the issuing service's verifier."""

    receipt_id: UUID
    outcome: Literal["succeeded", "refused"]
    usage: ScoutAllowance
    handoff_evidence: bytes = Field(max_length=256 * 1024, repr=False)
    sockets: Literal["closed"]
    child: Literal["not_started", "reaped"]


class ScoutSettlementVerifier(Protocol):
    """Validate owned completion identity and measured counters, never caller flags."""

    def verify(self, claim: NewScoutClaim, settlement: ScoutSettlement) -> None:
        """Require exact issued claim, closed ownership and retained gate counters."""
        ...


class ScoutStoreError(RuntimeError):
    """Content-free durable Scout refusal."""


class ScoutConflictError(ScoutStoreError):
    """Exact retained custody differs from the supplied operation."""


class ScoutCommitUnknownError(ScoutStoreError):
    """Commit may have succeeded; never retry issuance or release a reservation."""
