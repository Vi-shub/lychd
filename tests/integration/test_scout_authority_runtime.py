"""Current Scout network authority against real isolated DNS/TLS/HTTP.

The local credential owner, policy, Run fence, gate and transport are real.
Preparation and claim values are explicit fixtures, not database-issued authority.
This proof does not qualify durable admission, extraction authorization, workflow
settlement, a public website or model inference. No host network is reachable.
"""

from __future__ import annotations

import asyncio
import json
import platform
import ssl
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import dns.rdatatype
import pytest

from lychd.domain.codex import local_access
from lychd.domain.codex.local_access import LocalOperatorAccess
from lychd.domain.cortex import cancellation
from lychd.domain.cortex.cancellation import RunEffectFence
from lychd.domain.scout import attempts, authority, destination, transport
from lychd.domain.scout.attempts import (
    LocatorInfluence,
    LocatorSource,
    NewScoutClaim,
    PublicLocatorDeclaration,
    ScoutAllowance,
    ScoutPreparation,
    document_digest,
)
from lychd.domain.scout.authority import (
    CurrentNetworkGate,
    NetworkGateSnapshot,
    ScoutAction,
    ScoutHostAuthorizer,
    ScoutHostPolicy,
    ScoutNetworkAuthority,
)
from lychd.domain.scout.destination import preflight_destination
from lychd.domain.scout.transport import FetchCode, FetchError
from lychd.system.services import scout_dns, scout_fetch, scout_http, scout_socket
from lychd.system.services.scout_dns import ResolverEndpoint
from lychd.system.services.scout_fetch import fetch_static_page
from lychd.system.services.scout_http import serialize_get
from tests import scout_network_fixture as network_fixture

pytestmark = [pytest.mark.integration, pytest.mark.container]

_URL = f"https://{network_fixture.HOSTNAME}/authority?exact=1"
_BINDING = "a" * 64
_CUSTODY = "Synthetic resolver confined to this proof's loopback-only namespace."
_ALLOWANCE = ScoutAllowance(connects=3, transmissions=64, wire_bytes=64 * 1024)


@dataclass(frozen=True)
class _Passage:
    access: LocalOperatorAccess
    policy: ScoutHostPolicy
    authorizer: ScoutHostAuthorizer
    fence: RunEffectFence
    claim: NewScoutClaim
    gate: CurrentNetworkGate


def _passage(resolver: ResolverEndpoint, allowance: ScoutAllowance = _ALLOWANCE) -> _Passage:
    """Explicitly construct test admission data; never label it store-issued."""
    credential = "synthetic-authority-proof"
    access = LocalOperatorAccess(access_password=credential)
    authentication = access.authenticate(username=b"magus", password=credential.encode())
    assert authentication is not None
    now, source_id = datetime.now(UTC), uuid4()
    declaration = PublicLocatorDeclaration(
        url=_URL,
        subject=authentication.subject,
        authentication_generation=authentication.authentication_generation,
        purpose="isolated-network-proof",
        source_manifest=(LocatorSource(source_id=source_id, url=_URL),),
        influence_manifest=(LocatorInfluence(source_id=source_id),),
        policy_revision=1,
        declared_at=now,
    )
    preparation = ScoutPreparation(
        request_id=uuid4(),
        attempt_id=uuid4(),
        run_id=uuid4(),
        enqueue_seq=0,
        workflow_name="bridge_chat",
        sigil_name="fixture-not-authority",
        capability_key="fixture-not-a-model",
        pattern_digest=_BINDING,
        intent_digest=_BINDING,
        subject=authentication.subject,
        authentication_generation=authentication.authentication_generation,
        declaration=declaration,
        url=_URL,
        provider_id="native-static",
        profile="isolated-network-proof",
        binding_digest=_BINDING,
        configuration_digest=_BINDING,
        resolver_address=resolver.address,
        resolver_port=resolver.port,
        resolver_custody_statement=_CUSTODY,
        resolver_custody_digest=document_digest(_CUSTODY),
        purpose=declaration.purpose,
        policy_revision=1,
        revocation_generation=1,
        expires_at=now + timedelta(seconds=30),
        allowance=allowance,
    )
    policy = ScoutHostPolicy(
        policy_revision=1,
        revocation_generation=1,
        actions=frozenset(ScoutAction),
        purposes=frozenset({declaration.purpose}),
        categories=frozenset({declaration.category}),
        binding_digests=frozenset({_BINDING}),
        resolver_custody_digests=frozenset({preparation.resolver_custody_digest}),
    )
    authorizer = ScoutHostAuthorizer(access=access, policy=policy)
    fence = RunEffectFence()
    claim = NewScoutClaim(
        preparation=preparation, consent_id=uuid4(), enqueue_seq=1, claim_id=uuid4(), committed_at=now
    )
    gate = ScoutNetworkAuthority(authorizer=authorizer, fence=fence).activate(claim)
    return _Passage(access, policy, authorizer, fence, claim, gate)


def _accounted(passage: _Passage) -> NetworkGateSnapshot:
    snapshot = passage.gate.seal()
    assert snapshot.claim_id == passage.claim.claim_id
    assert snapshot.preparation_digest == passage.claim.preparation.digest
    assert snapshot.usage.within(passage.claim.preparation.allowance)
    assert snapshot.usage.wire_bytes == snapshot.accepted_wire_bytes
    assert snapshot.accepted_wire_bytes == sum(item.accepted_bytes or 0 for item in snapshot.evidence)
    admitted = [item for item in snapshot.evidence if item.outcome in {"accepted", "blocked"}]
    assert snapshot.usage.connects == sum(item.phase == "connect" for item in admitted)
    assert snapshot.usage.transmissions == sum(item.phase != "connect" for item in admitted)
    for item in snapshot.evidence:
        if item.outcome in {"accepted", "blocked"}:
            assert item.decision is not None
            assert item.decision.allowed
            assert item.decision.action is ScoutAction.FETCH
            assert item.decision.preparation_digest == passage.claim.preparation.digest
    return snapshot


async def _fetch(passage: _Passage, resolver: ResolverEndpoint, trust: Path, *, url: str = _URL) -> bytes:
    page = await fetch_static_page(
        preflight_destination(url), resolver=resolver, gate=passage.gate, ca_file=trust, deadline=passage.gate.deadline
    )
    assert page.observations[0].peer == network_fixture.ADDRESS
    return page.body


async def _positive(
    fixture: network_fixture.ScoutNetworkFixture, resolver: ResolverEndpoint, trust: Path
) -> dict[str, int]:
    passage = _passage(resolver)
    fixture.response = network_fixture.response("length")
    assert await _fetch(passage, resolver, trust) == network_fixture.BODY
    await fixture.settle()
    expected = serialize_get(preflight_destination(_URL))
    assert fixture.requests == [expected]
    assert fixture.queries == [
        (network_fixture.HOSTNAME + ".", dns.rdatatype.A),
        (network_fixture.HOSTNAME + ".", dns.rdatatype.AAAA),
    ]
    assert fixture.sni == [network_fixture.HOSTNAME]
    snapshot = _accounted(passage)
    assert snapshot.usage.connects == 3
    http = [item for item in snapshot.evidence if item.phase == "http"]
    assert http
    assert sum(item.accepted_bytes or 0 for item in http) > 0
    assert {item.application_digest for item in http} == {sha256(expected).hexdigest()}
    assert all(item.outcome in {"accepted", "blocked"} for item in snapshot.evidence)
    return {
        "connects": snapshot.usage.connects,
        "accepted_wire_bytes": snapshot.accepted_wire_bytes,
        "dns_wire_bytes": sum(item.accepted_bytes or 0 for item in snapshot.evidence if item.phase == "dns"),
    }


async def _substituted_request(
    fixture: network_fixture.ScoutNetworkFixture, resolver: ResolverEndpoint, trust: Path, checks: list[str]
) -> None:
    for suffix in ("/different?exact=1", "/authority?exact=2"):
        passage = _passage(resolver)
        before_http, before_sni = fixture.received_http_bytes, len(fixture.sni)
        with pytest.raises(FetchError) as caught:
            await _fetch(passage, resolver, trust, url=f"https://{network_fixture.HOSTNAME}{suffix}")
        assert caught.value.code is FetchCode.EGRESS
        await fixture.settle()
        assert fixture.sni[before_sni:] == [network_fixture.HOSTNAME]
        assert fixture.received_http_bytes == before_http
        snapshot = _accounted(passage)
        assert snapshot.evidence[-1].phase == "http"
        assert snapshot.evidence[-1].outcome == "refused"
        assert snapshot.evidence[-1].accepted_bytes == 0
    checks.append("same_host_path_and_query_substitution_prevents_http_bytes")


def _revocation(passage: _Passage, kind: str) -> Callable[[], None]:
    run_id = str(passage.claim.preparation.run_id)
    if kind == "authentication":
        return passage.access.revoke
    if kind == "policy":
        return lambda: passage.authorizer.replace_policy(replace(passage.policy, revocation_generation=2))
    if kind == "claim":
        return lambda: passage.fence.fence_claim(run_id, passage.claim.enqueue_seq, "worker_interrupted")
    if kind == "run":
        return lambda: passage.fence.fence(run_id, "cancelled")
    assert kind == "shutdown"
    return lambda: passage.fence.fence_all("shutdown")


async def _revoked_during_tls(
    fixture: network_fixture.ScoutNetworkFixture, resolver: ResolverEndpoint, trust: Path, checks: list[str]
) -> None:
    for kind in ("authentication", "policy", "claim", "run", "shutdown"):
        passage = _passage(resolver)
        before_http, before_sni = fixture.received_http_bytes, len(fixture.sni)
        fixture.on_sni = _revocation(passage, kind)
        with pytest.raises(FetchError) as caught:
            await _fetch(passage, resolver, trust)
        assert caught.value.code is FetchCode.EGRESS
        await fixture.settle()
        assert fixture.on_sni is None
        assert fixture.sni[before_sni:] == [network_fixture.HOSTNAME]
        assert fixture.received_http_bytes == before_http
        snapshot = _accounted(passage)
        assert snapshot.evidence[-1].outcome == "refused"
        assert snapshot.evidence[-1].accepted_bytes == 0
        assert not any(item.phase == "http" and item.accepted_bytes for item in snapshot.evidence)
        checks.append(f"{kind}_revoked_at_sni_prevents_http_bytes")


async def _budget_refusals(
    fixture: network_fixture.ScoutNetworkFixture,
    resolver: ResolverEndpoint,
    trust: Path,
    checks: list[str],
    dns_wire_bytes: int,
) -> None:
    for field, limit in (
        ("connects", 0),
        ("transmissions", 0),
        ("wire_bytes", 0),
        ("connects", 2),
        ("wire_bytes", dns_wire_bytes),
    ):
        allowance = _ALLOWANCE.model_copy(update={field: limit})
        passage = _passage(resolver, allowance)
        before_dns = fixture.received_dns_bytes
        before_connects, before_sni = fixture.dns_connections, len(fixture.sni)
        before_http = fixture.received_http_bytes
        with pytest.raises(FetchError) as caught:
            await _fetch(passage, resolver, trust)
        assert caught.value.code is FetchCode.LIMIT
        await fixture.settle()
        assert len(fixture.sni) == before_sni
        assert fixture.received_http_bytes == before_http
        snapshot = _accounted(passage)
        assert snapshot.evidence[-1].outcome == "refused"
        assert snapshot.evidence[-1].refusal is FetchCode.LIMIT
        if limit == 0:
            assert fixture.received_dns_bytes == before_dns
            assert fixture.dns_connections - before_connects == (0 if field == "connects" else 1)
        else:
            assert fixture.dns_connections - before_connects == 2
            assert snapshot.usage.connects == (2 if field == "connects" else 3)
            assert fixture.received_dns_bytes - before_dns == dns_wire_bytes
        if field == "wire_bytes" and limit:
            assert snapshot.evidence[-1].phase == "tls"
            assert snapshot.usage.wire_bytes == dns_wire_bytes
            checks.append("wire_bytes_budget_after_dns_prevents_client_hello")
        else:
            checks.append(f"{field}_budget_{limit}_prevents_next_network_effect")


def _fingerprints() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    paths = (
        Path(local_access.__file__),
        Path(cancellation.__file__),
        Path(attempts.__file__),
        Path(authority.__file__),
        Path(destination.__file__),
        Path(transport.__file__),
        Path(scout_dns.__file__),
        Path(scout_socket.__file__),
        Path(scout_http.__file__),
        Path(scout_fetch.__file__),
        Path(network_fixture.__file__),
        Path(__file__),
    )
    return {path.resolve().relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest() for path in paths}


@pytest.mark.asyncio
async def test_current_authority_isolated_network_passage(tmp_path: Path) -> None:
    network_fixture.require_namespace("LYCHD_REQUIRE_SCOUT_AUTHORITY_PROOF")
    fingerprint = _fingerprints()
    checks: list[str] = []
    receipt: dict[str, object] = {
        "schema": "scout-network-authority-local-proof@1",
        "status": "failed",
        "kernel": platform.release(),
        "python": platform.python_version(),
        "openssl": ssl.OPENSSL_VERSION,
        "dependency_versions": {name: version(name) for name in ("h11", "dnspython", "pydantic")},
        "network": "isolated-loopback-only",
        "authority": "production-current-network-gate-with-fixture-preparation-and-claim",
        "durable_admission": "not_exercised",
        "extraction_and_inference": "not_exercised",
        "source_fingerprints": fingerprint,
        "checks": checks,
    }
    fixture: network_fixture.ScoutNetworkFixture | None = None
    try:
        async with asyncio.timeout(60):
            certificate, key = await asyncio.to_thread(
                network_fixture.certificate, tmp_path, "authority", network_fixture.HOSTNAME, include_ip=False
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            context.set_alpn_protocols(["http/1.1"])
            fixture = network_fixture.ScoutNetworkFixture(context, context)
            resolver = await fixture.start()
            positive = await _positive(fixture, resolver, certificate)
            receipt["successful_passage"] = positive
            checks.append("real_current_authority_dns_tls_http_exact_request_and_accounting")
            await _substituted_request(fixture, resolver, certificate, checks)
            await _revoked_during_tls(fixture, resolver, certificate, checks)
            await _budget_refusals(fixture, resolver, certificate, checks, positive["dns_wire_bytes"])
            await fixture.settle()
            assert _fingerprints() == fingerprint
            receipt["tls_versions"] = sorted(fixture.versions)
            checks.append("source_fingerprints_unchanged")
            receipt["status"] = "passed"
    finally:
        try:
            if fixture is not None:
                await fixture.close()
        except BaseException:
            receipt["status"] = "failed"
            raise
        finally:
            (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
