"""Same-loop current authority for the native, single-URL Scout passage.

An admitted preparation and a freshly issued store claim are mandatory inputs.
Their shape is not proof of issuance: only the trusted assembly may activate the
claim returned by its durable store. This module neither issues durable claims
nor recovers permits from receipts. It protects handoffs by the trusted native
transport, not against arbitrary code in the same Python process.
"""

from __future__ import annotations

import asyncio
import errno
import json
import math
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from ipaddress import ip_address
from typing import TYPE_CHECKING, Literal

from lychd.domain.codex.local_access import LocalOperatorAccess
from lychd.domain.cortex.cancellation import RunEffectFence
from lychd.domain.scout.attempts import NewScoutClaim, PublicLocatorDeclaration, ScoutAllowance, ScoutPreparation
from lychd.domain.scout.destination import DestinationError, classify_destination_addresses, preflight_destination
from lychd.domain.scout.transport import FetchCode, FetchError, NetworkHandoff, NetworkTarget

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from lychd.domain.scout.destination import DestinationCandidate

_MAX_HANDOFFS = 256
_MAX_CHUNK = 16 * 1024
_MAX_ORIGIN_SEND = 512 * 1024
_MAX_SELECTORS = 64
_MAX_SELECTOR_LENGTH = 256
_MAX_HOSTNAME = 253
_MAX_ADDRESS = 39
_DNS_FAMILIES = 2
_MAX_DNS_QUERY = 512
_HTTPS_PORT = 443
_CONNECTING = frozenset({0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR})


class ScoutAction(StrEnum):
    """Separate local policy decisions; none inherits another action's approval."""

    CLASSIFY_LOCATOR = "classify_locator"
    REVIEW = "review"
    JUDGE = "judge"
    FETCH = "fetch"
    EXTRACT = "extract"
    CONSUME = "consume"


@dataclass(frozen=True, slots=True)
class ScoutAuthorizationDecision:
    """Current, exactly bound local judgment, not a bearer or retained permit."""

    action: ScoutAction
    preparation_digest: str = field(repr=False)
    allowed: bool


@dataclass(frozen=True, slots=True)
class ScoutHostPolicy:
    """Explicit narrow host policy; empty sets deny and no scope bag is consulted."""

    policy_revision: int
    revocation_generation: int
    actions: frozenset[ScoutAction]
    purposes: frozenset[str] = field(repr=False)
    categories: frozenset[str] = field(repr=False)
    binding_digests: frozenset[str] = field(repr=False)
    resolver_custody_digests: frozenset[str] = field(repr=False)

    def __post_init__(self) -> None:
        """Reject mutable policy inputs and unbounded or ambiguous selectors."""
        if any(type(value) is not int or value < 0 for value in (self.policy_revision, self.revocation_generation)):
            raise FetchError(FetchCode.CONFIG)
        if type(self.actions) is not frozenset or any(type(action) is not ScoutAction for action in self.actions):
            raise FetchError(FetchCode.CONFIG)
        for values in (self.purposes, self.categories, self.binding_digests, self.resolver_custody_digests):
            if type(values) is not frozenset or len(values) > _MAX_SELECTORS:
                raise FetchError(FetchCode.CONFIG)
            if any(type(value) is not str or not 0 < len(value) <= _MAX_SELECTOR_LENGTH for value in values):
                raise FetchError(FetchCode.CONFIG)


class ScoutHostAuthorizer:
    """Actual local authentication plus explicit policy, serialized on one loop."""

    def __init__(self, *, access: LocalOperatorAccess, policy: ScoutHostPolicy) -> None:
        """Bind the real startup authority; a callback or Sigil is not accepted."""
        if type(access) is not LocalOperatorAccess or type(policy) is not ScoutHostPolicy:
            raise FetchError(FetchCode.CONFIG)
        self._loop = asyncio.get_running_loop()
        access.bind_control_loop()
        self._access = access
        self._policy = policy

    def _require_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise FetchError(FetchCode.EGRESS)

    def replace_policy(self, policy: ScoutHostPolicy) -> None:
        """Invalidate prior preparations; a replacement cannot reuse a generation."""
        self._require_loop()
        if type(policy) is not ScoutHostPolicy or policy.revocation_generation <= self._policy.revocation_generation:
            raise FetchError(FetchCode.CONFIG)
        self._policy = policy

    def authorize(self, action: ScoutAction, preparation: ScoutPreparation) -> ScoutAuthorizationDecision:
        """Judge one exact retained public declaration under current host policy."""
        self._require_loop()
        if type(action) is not ScoutAction or type(preparation) is not ScoutPreparation:
            raise FetchError(FetchCode.EGRESS)
        policy = self._policy
        allowed = (
            self._declaration_allowed(
                preparation.declaration,
                binding_digest=preparation.binding_digest,
                resolver_custody_digest=preparation.resolver_custody_digest,
                revocation_generation=preparation.revocation_generation,
            )
            and action in policy.actions
            and preparation.expires_at > datetime.now(UTC)
        )
        return ScoutAuthorizationDecision(action, preparation.digest, allowed)

    def _declaration_allowed(
        self,
        declaration: PublicLocatorDeclaration,
        *,
        binding_digest: str,
        resolver_custody_digest: str,
        revocation_generation: int,
    ) -> bool:
        policy = self._policy
        return (
            self._access.current_binding(declaration.subject, declaration.authentication_generation)
            and declaration.policy_revision == policy.policy_revision
            and revocation_generation == policy.revocation_generation
            and ScoutAction.CLASSIFY_LOCATOR in policy.actions
            and declaration.purpose in policy.purposes
            and declaration.category in policy.categories
            and declaration.classification == "public"
            and binding_digest in policy.binding_digests
            and resolver_custody_digest in policy.resolver_custody_digests
            and declaration.declared_at <= datetime.now(UTC)
        )

    def require_declaration(
        self,
        declaration: PublicLocatorDeclaration,
        *,
        binding_digest: str,
        resolver_custody_digest: str,
        revocation_generation: int,
    ) -> None:
        """Classify an explicit command before Run issuance, without issuing a grant.

        The caller first verifies the actual authentication marker and binds the
        parsed command exactly. These retained fields cannot authenticate a request.
        """
        self._require_loop()
        if type(declaration) is not PublicLocatorDeclaration or not self._declaration_allowed(
            declaration,
            binding_digest=binding_digest,
            resolver_custody_digest=resolver_custody_digest,
            revocation_generation=revocation_generation,
        ):
            raise FetchError(FetchCode.EGRESS)

    def require_current(self, action: ScoutAction, preparation: ScoutPreparation) -> ScoutAuthorizationDecision:
        """Refuse without echoing a locator, subject, policy selector or source."""
        decision = self.authorize(action, preparation)
        if not decision.allowed:
            raise FetchError(FetchCode.EGRESS)
        return decision


@dataclass(frozen=True, slots=True)
class NetworkDecisionEvidence:
    """Bounded restricted local evidence, never a public projection or permission."""

    ordinal: int
    target: NetworkTarget | None = field(repr=False)
    phase: str
    wire_digest: str = field(repr=False)
    application_digest: str | None = field(repr=False)
    offered_bytes: int
    accepted_bytes: int | None
    outcome: Literal["pending", "accepted", "blocked", "refused", "uncertain"]
    decision: ScoutAuthorizationDecision | None = field(repr=False)
    refusal: FetchCode | None = None


@dataclass(frozen=True, slots=True)
class NetworkGateSnapshot:
    """A sealed gate's usage; closure is not socket or child containment proof.

    ``usage.wire_bytes`` includes uncertain offered bytes held against the
    reservation. ``accepted_wire_bytes`` counts only synchronous send returns.
    TCP/IP overhead is outside these payload counters; connects are separate.
    The service must verify this exact issued snapshot, not reconstructed fields.
    """

    claim_id: UUID = field(repr=False)
    preparation_digest: str = field(repr=False)
    usage: ScoutAllowance
    accepted_wire_bytes: int
    evidence: tuple[NetworkDecisionEvidence, ...] = field(repr=False)

    @property
    def evidence_bytes(self) -> bytes:
        """Serialize restricted decision evidence, never raw wire/application data."""
        rows = [
            {
                "ordinal": item.ordinal,
                "target": (
                    {
                        "address": item.target.address,
                        "port": item.target.port,
                        "hostname": item.target.hostname,
                        "role": item.target.role,
                    }
                    if item.target is not None
                    else None
                ),
                "phase": item.phase,
                "wire_digest": item.wire_digest,
                "application_digest": item.application_digest,
                "offered_bytes": item.offered_bytes,
                "accepted_bytes": item.accepted_bytes,
                "outcome": item.outcome,
                "authorized": item.decision is not None and item.decision.allowed,
                "refusal": item.refusal,
            }
            for item in self.evidence
        ]
        return json.dumps(
            {"claim_id": str(self.claim_id), "preparation_digest": self.preparation_digest, "handoffs": rows},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(slots=True, repr=False)
class _GateActivation:
    claim: NewScoutClaim
    authorizer: ScoutHostAuthorizer
    fence: RunEffectFence
    consumed: bool = False


class ScoutNetworkAuthority:
    """Consume fresh issued claims once in this process, without issuing one."""

    def __init__(self, *, authorizer: ScoutHostAuthorizer, fence: RunEffectFence) -> None:
        """Share the current authorizer and Cortex's actual monotonic fence."""
        if type(authorizer) is not ScoutHostAuthorizer or type(fence) is not RunEffectFence:
            raise FetchError(FetchCode.CONFIG)
        self._loop = asyncio.get_running_loop()
        self._authorizer = authorizer
        self._fence = fence
        self._activated: set[UUID] = set()

    def activate(self, claim: NewScoutClaim) -> CurrentNetworkGate:
        """Accept only the trusted store's fresh claim, never a replayed snapshot.

        Duplicate activation and any failed activation remain consumed locally.
        The store separately owns durable uniqueness, reservation and crash law.
        """
        if asyncio.get_running_loop() is not self._loop or type(claim) is not NewScoutClaim:
            raise FetchError(FetchCode.EGRESS)
        if claim.claim_id in self._activated:
            raise FetchError(FetchCode.EGRESS)
        self._activated.add(claim.claim_id)
        self._authorizer.require_current(ScoutAction.FETCH, claim.preparation)
        if self._fence.reason(str(claim.preparation.run_id), claim.enqueue_seq) is not None:
            raise FetchError(FetchCode.EGRESS)
        return CurrentNetworkGate(_GateActivation(claim, self._authorizer, self._fence))


def _http_request(candidate: DestinationCandidate) -> bytes:
    """Compile the fixed native GET independently checked against its serializer."""
    authority = f"[{candidate.hostname}]" if ":" in candidate.hostname else candidate.hostname
    target = candidate.normalized_url[len("https://") + len(authority) :]
    if not target.startswith("/"):
        target = "/" + target
    return (
        f"GET {target} HTTP/1.1\r\nHost: {authority}\r\nAccept: text/html,text/plain\r\n"
        "Accept-Encoding: gzip, identity\r\nConnection: close\r\n\r\n"
    ).encode("ascii")


class CurrentNetworkGate:
    """One exact URL's finite contact sequence under a fresh retained claim.

    Use ``ScoutNetworkAuthority.activate``. The trusted DNS/TLS mechanism owns
    answer completeness, selected-peer provenance and hostname-to-SNI/certificate
    binding. This gate independently binds their supplied identities, pins the
    selected public peer, and validates DNS/HTTP disclosure bytes. It cannot infer
    SNI from encrypted records or prove that arbitrary Python callbacks are syscalls.
    """

    def __init__(self, activation: _GateActivation) -> None:
        """Initialize bounded volatile enforcement, never a durable permit."""
        if type(activation) is not _GateActivation or activation.consumed:
            raise FetchError(FetchCode.EGRESS)
        activation.consumed = True
        claim = activation.claim
        self._loop = asyncio.get_running_loop()
        self._claim = claim
        self._authorizer = activation.authorizer
        self._fence = activation.fence
        self._candidate = preflight_destination(claim.preparation.url)
        try:
            if str(ip_address(claim.preparation.resolver_address)) != claim.preparation.resolver_address:
                raise FetchError(FetchCode.CONFIG)
        except ValueError:
            raise FetchError(FetchCode.CONFIG) from None
        self._http = _http_request(self._candidate)
        if claim.committed_at.tzinfo is None:
            raise FetchError(FetchCode.EGRESS)
        expiry = min(claim.preparation.expires_at, claim.committed_at + timedelta(seconds=30))
        remaining = (expiry - datetime.now(UTC)).total_seconds()
        if not math.isfinite(remaining) or remaining <= 0:
            raise FetchError(FetchCode.TIMEOUT)
        self._deadline = time.monotonic() + min(remaining, 30.0)
        self._handoffs = self._connects = self._transmissions = self._charged = self._accepted = self._origin_sent = 0
        self._resolver_started = self._resolver_finished = self._dns_offset = 0
        self._dns_message: bytes | None = None
        self._origin: NetworkTarget | None = None
        self._pending: tuple[str, bytes, bytes | None] | None = None
        self._evidence: list[NetworkDecisionEvidence] = []
        self._closed = self._busy = False
        self._snapshot: NetworkGateSnapshot | None = None

    @property
    def deadline(self) -> float:
        """Expose the remaining immutable monotonic deadline to native transport."""
        return self._deadline

    def _require_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise FetchError(FetchCode.EGRESS)

    def seal(self) -> NetworkGateSnapshot:
        """Permanently end handoffs and return this gate's stable issued snapshot."""
        self._require_loop()
        if self._busy:
            raise FetchError(FetchCode.EGRESS)
        self._closed = True
        if self._snapshot is None:
            self._snapshot = NetworkGateSnapshot(
                self._claim.claim_id,
                self._claim.preparation.digest,
                ScoutAllowance(connects=self._connects, transmissions=self._transmissions, wire_bytes=self._charged),
                self._accepted,
                tuple(self._evidence),
            )
        return self._snapshot

    def _check(self, handoff: NetworkHandoff) -> ScoutAuthorizationDecision:
        preparation = self._claim.preparation
        if time.monotonic() >= self.deadline:
            raise FetchError(FetchCode.TIMEOUT)
        if self._fence.reason(str(preparation.run_id), self._claim.enqueue_seq) is not None:
            raise FetchError(FetchCode.EGRESS)
        decision = self._authorizer.require_current(ScoutAction.FETCH, preparation)
        self._check_shape(handoff)
        if handoff.phase == "connect":
            self._check_connect(handoff)
        elif handoff.phase == "dns":
            self._check_dns(handoff)
        else:
            self._check_origin(handoff)
        allowance = preparation.allowance
        if (
            self._handoffs >= _MAX_HANDOFFS
            or self._transmissions + int(handoff.phase != "connect") > allowance.transmissions
            or self._connects + int(handoff.phase == "connect") > allowance.connects
            or self._charged + len(handoff.wire_bytes) > allowance.wire_bytes
        ):
            raise FetchError(FetchCode.LIMIT)
        return decision

    @staticmethod
    def _check_shape(handoff: NetworkHandoff) -> None:
        if (
            type(handoff) is not NetworkHandoff
            or type(handoff.target) is not NetworkTarget
            or type(handoff.wire_bytes) is not bytes
            or len(handoff.wire_bytes) > _MAX_CHUNK
            or type(handoff.phase) is not str
            or handoff.phase not in {"connect", "dns", "tls", "http"}
            or (handoff.application_bytes is not None and type(handoff.application_bytes) is not bytes)
            or (handoff.application_bytes is not None and len(handoff.application_bytes) > _MAX_CHUNK)
        ):
            raise FetchError(FetchCode.EGRESS)
        target = handoff.target
        if (
            type(target.address) is not str
            or not 0 < len(target.address) <= _MAX_ADDRESS
            or type(target.hostname) is not str
            or not 0 < len(target.hostname) <= _MAX_HOSTNAME
            or not target.hostname.isascii()
            or type(target.port) is not int
            or type(target.role) is not str
            or target.role not in {"resolver", "origin"}
        ):
            raise FetchError(FetchCode.EGRESS)
        if handoff.phase == "connect":
            if handoff.wire_bytes or handoff.application_bytes is not None:
                raise FetchError(FetchCode.EGRESS)
        elif not handoff.wire_bytes:
            raise FetchError(FetchCode.EGRESS)

    def _resolver_target(self) -> NetworkTarget:
        preparation = self._claim.preparation
        return NetworkTarget(
            preparation.resolver_address, preparation.resolver_port, self._candidate.hostname, "resolver"
        )

    def _check_connect(self, handoff: NetworkHandoff) -> None:
        if self._pending is not None:
            raise FetchError(FetchCode.EGRESS)
        if handoff.target.role == "resolver":
            if (
                handoff.target != self._resolver_target()
                or self._candidate.ip_literal is not None
                or self._origin is not None
                or self._resolver_started != self._resolver_finished
                or self._resolver_started >= _DNS_FAMILIES
            ):
                raise FetchError(FetchCode.EGRESS)
            return
        if (
            handoff.target.role != "origin"
            or self._origin is not None
            or handoff.target.port != _HTTPS_PORT
            or handoff.target.hostname != self._candidate.hostname
            or (self._candidate.ip_literal is None and self._resolver_finished != _DNS_FAMILIES)
        ):
            raise FetchError(FetchCode.EGRESS)
        try:
            classify_destination_addresses(
                self._candidate, hostname=handoff.target.hostname, addresses=(handoff.target.address,)
            )
        except DestinationError:
            raise FetchError(FetchCode.EGRESS) from None

    def _check_dns(self, handoff: NetworkHandoff) -> None:
        message = handoff.application_bytes
        if (
            handoff.target != self._resolver_target()
            or self._origin is not None
            or self._resolver_started != self._resolver_finished + 1
            or message is None
            or len(message) > _MAX_DNS_QUERY
        ):
            raise FetchError(FetchCode.EGRESS)
        qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in self._candidate.hostname.split("."))
        question = qname + b"\x00" + (1 if self._resolver_finished == 0 else 28).to_bytes(2, "big") + b"\x00\x01"
        expected_tail = b"\x01\x00\x00\x01" + b"\x00" * 6 + question
        if (
            len(message) != len(expected_tail) + 4
            or int.from_bytes(message[:2], "big") != len(message) - 2
            or message[4:] != expected_tail
            or (self._dns_message is not None and message != self._dns_message)
            or handoff.wire_bytes != message[self._dns_offset : self._dns_offset + _MAX_CHUNK]
        ):
            raise FetchError(FetchCode.EGRESS)

    def _check_origin(self, handoff: NetworkHandoff) -> None:
        if self._origin is None or handoff.target != self._origin:
            raise FetchError(FetchCode.EGRESS)
        if handoff.phase == "tls" and handoff.application_bytes is not None:
            raise FetchError(FetchCode.EGRESS)
        if handoff.phase == "http" and handoff.application_bytes != self._http:
            raise FetchError(FetchCode.EGRESS)
        if self._pending is not None and self._pending != (
            handoff.phase,
            handoff.wire_bytes,
            handoff.application_bytes,
        ):
            raise FetchError(FetchCode.EGRESS)
        if self._origin_sent + len(handoff.wire_bytes) > _MAX_ORIGIN_SEND:
            raise FetchError(FetchCode.LIMIT)

    def handoff(self, handoff: NetworkHandoff, action: Callable[[], int]) -> int:
        """Check, pre-debit and execute once without yielding to another owner."""
        self._require_loop()
        if self._closed or self._busy:
            raise FetchError(FetchCode.EGRESS)
        self._busy = True
        evidence_before = len(self._evidence)
        try:
            decision = self._check(handoff)
            self._debit(handoff, decision)
            return self._execute(handoff, action)
        except FetchError as exc:
            self._closed = True
            if len(self._evidence) == evidence_before:
                self._record_refusal(handoff, exc.code)
            raise
        finally:
            self._busy = False

    def _record_refusal(self, handoff: NetworkHandoff, code: FetchCode) -> None:
        try:
            self._check_shape(handoff)
        except FetchError:
            self._evidence.append(
                NetworkDecisionEvidence(self._handoffs + 1, None, "unknown", "", None, 0, 0, "refused", None, code)
            )
            return
        self._evidence.append(
            NetworkDecisionEvidence(
                self._handoffs + 1,
                handoff.target,
                handoff.phase,
                sha256(handoff.wire_bytes).hexdigest(),
                sha256(handoff.application_bytes).hexdigest() if handoff.application_bytes is not None else None,
                len(handoff.wire_bytes),
                0,
                "refused",
                None,
                code,
            )
        )

    def _debit(self, handoff: NetworkHandoff, decision: ScoutAuthorizationDecision) -> None:
        self._handoffs += 1
        self._transmissions += int(handoff.phase != "connect")
        self._charged += len(handoff.wire_bytes)
        if handoff.phase == "connect":
            self._connects += 1
            if handoff.target.role == "resolver":
                self._resolver_started += 1
            else:
                self._origin = handoff.target
        elif handoff.phase == "dns":
            self._dns_message = handoff.application_bytes
        if handoff.target.role == "origin":
            self._origin_sent += len(handoff.wire_bytes)
        self._evidence.append(
            NetworkDecisionEvidence(
                self._handoffs,
                handoff.target,
                handoff.phase,
                sha256(handoff.wire_bytes).hexdigest(),
                sha256(handoff.application_bytes).hexdigest() if handoff.application_bytes is not None else None,
                len(handoff.wire_bytes),
                None,
                "pending",
                decision,
            )
        )

    def _execute(self, handoff: NetworkHandoff, action: Callable[[], int]) -> int:
        try:
            count = action()
        except (BlockingIOError, InterruptedError) as exc:
            if handoff.phase == "connect":
                self._uncertain()
                raise FetchError(FetchCode.CONNECT) from None
            self._accepted_send(handoff, 0, outcome="blocked")
            if isinstance(exc, BlockingIOError):
                raise BlockingIOError from None
            raise InterruptedError from None
        except Exception:  # noqa: BLE001 - foreign syscall diagnostics must not escape
            self._uncertain()
            raise FetchError(FetchCode.TRANSPORT) from None
        except BaseException:
            self._uncertain()
            raise
        if type(count) is not int or (handoff.phase != "connect" and not 0 <= count <= len(handoff.wire_bytes)):
            self._uncertain()
            raise FetchError(FetchCode.TRANSPORT)
        if handoff.phase == "connect":
            self._evidence[-1] = replace(self._evidence[-1], accepted_bytes=0, outcome="accepted")
            if count not in _CONNECTING:
                self._closed = True
        else:
            self._accepted_send(handoff, count, outcome="accepted")
            if count == 0:
                self._closed = True
        return count

    def _uncertain(self) -> None:
        self._closed = True
        self._evidence[-1] = replace(self._evidence[-1], outcome="uncertain")

    def _accepted_send(self, handoff: NetworkHandoff, count: int, *, outcome: Literal["accepted", "blocked"]) -> None:
        refund = len(handoff.wire_bytes) - count
        self._charged -= refund
        self._accepted += count
        if handoff.target.role == "origin":
            self._origin_sent -= refund
        suffix = handoff.wire_bytes[count:]
        self._pending = (handoff.phase, suffix, handoff.application_bytes) if suffix else None
        if handoff.phase == "dns":
            self._dns_offset += count
            if self._dns_message is not None and self._dns_offset == len(self._dns_message):
                self._resolver_finished += 1
                self._dns_offset = 0
                self._dns_message = None
        self._evidence[-1] = replace(self._evidence[-1], accepted_bytes=count, outcome=outcome)
