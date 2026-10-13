"""Opt-in real Scout DNS/TLS proof, exclusively in a pre-isolated network namespace.

The numeric public-classified address is assigned to the namespace's only loopback
interface. No destination or transport is mocked. The audit gate is a synthetic
fixture, not production durable authority. Ephemeral certificate keys stay in the
test directory and are excluded from the sanitized receipt.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import ssl
import time
from collections.abc import Callable
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import dns.message
import dns.rdatatype
import pytest

from lychd.domain.scout import destination, transport
from lychd.domain.scout.destination import preflight_destination
from lychd.domain.scout.transport import FetchCode, FetchError, NetworkHandoff
from lychd.system import scout_worker
from lychd.system.services import scout_dns, scout_extraction, scout_fetch, scout_http, scout_socket
from tests import scout_network_fixture
from tests.scout_network_fixture import (
    ADDRESS,
    BODY,
    HOSTNAME,
    ScoutNetworkFixture,
    require_namespace,
    response,
)
from tests.scout_network_fixture import (
    certificate as make_certificate,
)

pytestmark = [pytest.mark.integration, pytest.mark.container]


def _fingerprints() -> dict[str, str]:
    paths = (
        Path(destination.__file__),
        Path(transport.__file__),
        Path(scout_dns.__file__),
        Path(scout_socket.__file__),
        Path(scout_http.__file__),
        Path(scout_fetch.__file__),
        Path(scout_extraction.__file__),
        Path(scout_worker.__file__),
        Path(__file__),
        Path(scout_network_fixture.__file__),
    )
    root = Path(__file__).resolve().parents[2]
    return {path.resolve().relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest() for path in paths}


class _AuditGate:
    """Synthetic exact-target policy; does not claim durable authority or accounting."""

    def __init__(self, resolver: scout_dns.ResolverEndpoint, hostname: str, request: bytes) -> None:
        self.resolver = resolver
        self.hostname = hostname
        self.request = request
        self.revoke_http = False
        self.observed: list[tuple[str, int]] = []
        self.http_actions = 0

    def handoff(self, handoff: NetworkHandoff, action: Callable[[], int]) -> int:
        target = handoff.target
        assert target.hostname == self.hostname
        assert len(handoff.wire_bytes) <= scout_socket.MAX_CHUNK_BYTES
        if target.role == "resolver":
            assert (target.address, target.port) == (self.resolver.address, self.resolver.port)
            assert handoff.phase in {"connect", "dns"}
            if handoff.phase == "dns":
                application = handoff.application_bytes
                assert application is not None
                assert len(application) >= 2
                assert int.from_bytes(application[:2], "big") == len(application) - 2
                query = dns.message.from_wire(application[2:])
                assert len(query.question) == 1
                assert query.question[0].name.to_text() == self.hostname + "."
                assert query.question[0].rdtype in {dns.rdatatype.A, dns.rdatatype.AAAA}
                assert handoff.wire_bytes in application
        else:
            assert target.role == "origin"
            assert (target.address, target.port) == (ADDRESS, 443)
            assert handoff.phase in {"connect", "tls", "http"}
        if handoff.phase == "connect":
            assert handoff.wire_bytes == b""
            assert handoff.application_bytes is None
        elif handoff.phase == "tls":
            assert handoff.wire_bytes
            assert handoff.application_bytes is None
        elif handoff.phase == "http":
            assert handoff.wire_bytes
            assert handoff.application_bytes == self.request
            if self.revoke_http:
                raise FetchError(FetchCode.EGRESS)
            self.http_actions += 1
        count = action()
        self.observed.append((handoff.phase, max(count, 0)))
        assert len(self.observed) <= 128
        return count


def _extraction_runtime() -> scout_extraction.ScoutExtractionRuntime | None:
    configured = os.environ.get("LYCHD_SCOUT_EXTRACTION_PROFILE")
    if configured is None:
        return None
    path = Path(configured)
    assert path.is_absolute()
    assert path.stat().st_size <= 8192
    profile = json.loads(path.read_text())
    assert set(profile) == {"bwrap", "python_root", "libraries"}
    return scout_extraction.ScoutExtractionRuntime(
        Path(profile["bwrap"]),
        Path(profile["python_root"]),
        tuple(Path(value) for value in profile["libraries"]),
    )


async def _prove_response_framings(
    fixture: ScoutNetworkFixture, resolver: scout_dns.ResolverEndpoint, trust: Path, checks: list[str]
) -> None:
    for kind in ("length", "chunked", "gzip", "eof"):
        candidate = preflight_destination(f"https://{HOSTNAME}/{kind}?exact=1")
        request = scout_http.serialize_get(candidate)
        gate = _AuditGate(resolver, HOSTNAME, request)
        fixture.response = response(kind)
        before = len(fixture.queries)
        page = await scout_fetch.fetch_static_page(
            candidate,
            resolver=resolver,
            gate=gate,
            ca_file=trust,
            deadline=time.monotonic() + 5,
        )
        await fixture.settle()
        assert page.body == BODY
        assert page.media_type == "text/html"
        assert fixture.queries[before:] == [
            (HOSTNAME + ".", dns.rdatatype.A),
            (HOSTNAME + ".", dns.rdatatype.AAAA),
        ]
        assert fixture.requests[-1] == request
        assert fixture.sni[-1] == HOSTNAME
        assert b"Host: fixture.example.com\r\n" in request
        assert page.observations[0].addresses == (ADDRESS,)
        assert page.observations[0].peer == ADDRESS
        assert page.observations[0].http_response_digest == sha256(fixture.response).hexdigest()
        assert gate.http_actions > 0
        checks.append(f"real_dns_tls_{kind}")


async def _prove_certificate_refusals(
    fixture: ScoutNetworkFixture, resolver: scout_dns.ResolverEndpoint, trust: Path, wrong_cert: Path, checks: list[str]
) -> None:
    candidate = preflight_destination(f"https://{HOSTNAME}/negative")
    request = scout_http.serialize_get(candidate)
    for wrong_name, roots in ((True, trust), (False, wrong_cert)):
        fixture.wrong_name = wrong_name
        before = len(fixture.requests)
        with pytest.raises(FetchError) as error:
            await scout_fetch.fetch_static_page(
                candidate,
                resolver=resolver,
                gate=_AuditGate(resolver, HOSTNAME, request),
                ca_file=roots,
                deadline=time.monotonic() + 5,
            )
        assert error.value.code is FetchCode.TLS
        assert len(fixture.requests) == before
    checks.append("wrong_hostname_and_untrusted_certificate_refuse")
    fixture.wrong_name = False


async def _prove_destination_refusals(
    fixture: ScoutNetworkFixture, resolver: scout_dns.ResolverEndpoint, trust: Path, checks: list[str]
) -> None:
    candidate = preflight_destination(f"https://{HOSTNAME}/negative")
    request = scout_http.serialize_get(candidate)
    fixture.mixed_dns = True
    gate = _AuditGate(resolver, HOSTNAME, request)
    before = len(fixture.sni)
    with pytest.raises(FetchError) as error:
        await scout_fetch.fetch_static_page(
            candidate, resolver=resolver, gate=gate, ca_file=trust, deadline=time.monotonic() + 5
        )
    assert error.value.code is FetchCode.DESTINATION
    assert len(fixture.sni) == before
    assert all(phase in {"connect", "dns"} for phase, _ in gate.observed)
    fixture.mixed_dns = False
    checks.append("mixed_private_dns_prevents_origin_contact")

    fixture.response = b"HTTP/1.1 302 Found\r\nLocation: https://unlisted.example.com/next\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    before = len(fixture.queries)
    with pytest.raises(FetchError) as error:
        await scout_fetch.fetch_static_page(
            candidate,
            resolver=resolver,
            gate=_AuditGate(resolver, HOSTNAME, request),
            ca_file=trust,
            deadline=time.monotonic() + 5,
        )
    assert error.value.code is FetchCode.REDIRECT
    assert len(fixture.queries) - before == 2
    checks.append("unlisted_redirect_prevents_new_dns")


async def _prove_handshake_revocation_and_ip_identity(
    fixture: ScoutNetworkFixture, resolver: scout_dns.ResolverEndpoint, trust: Path, checks: list[str]
) -> None:
    candidate = preflight_destination(f"https://{HOSTNAME}/negative")
    request = scout_http.serialize_get(candidate)
    fixture.response = response("length")
    gate = _AuditGate(resolver, HOSTNAME, request)
    assert not gate.revoke_http

    def revoke_http() -> None:
        assert not gate.revoke_http
        gate.revoke_http = True

    fixture.on_sni = revoke_http
    before = fixture.received_http_bytes
    with pytest.raises(FetchError) as error:
        await scout_fetch.fetch_static_page(
            candidate, resolver=resolver, gate=gate, ca_file=trust, deadline=time.monotonic() + 5
        )
    assert error.value.code is FetchCode.EGRESS
    await fixture.settle()
    assert gate.revoke_http
    assert fixture.on_sni is None
    assert gate.http_actions == 0
    assert fixture.received_http_bytes == before
    checks.append("sni_time_revocation_prevents_actual_http_send")

    literal = preflight_destination(f"https://{ADDRESS}/ip")
    before = len(fixture.queries)
    page = await scout_fetch.fetch_static_page(
        literal,
        resolver=resolver,
        gate=_AuditGate(resolver, ADDRESS, scout_http.serialize_get(literal)),
        ca_file=trust,
        deadline=time.monotonic() + 5,
    )
    await fixture.settle()
    assert page.body == BODY
    assert len(fixture.queries) == before
    assert fixture.sni[-1] is None
    assert b"Host: 93.184.216.34\r\n" in fixture.requests[-1]
    checks.append("ip_certificate_identity_without_dns_or_ip_sni")


async def _prove_extraction(
    resolver: scout_dns.ResolverEndpoint, trust: Path, checks: list[str], receipt: dict[str, object]
) -> None:
    candidate = preflight_destination(f"https://{HOSTNAME}/negative")
    request = scout_http.serialize_get(candidate)
    runtime = _extraction_runtime()
    if runtime is not None:
        acquired = await scout_fetch.acquire_static_text(
            candidate,
            resolver=resolver,
            gate=_AuditGate(resolver, HOSTNAME, request),
            ca_file=trust,
            runtime=runtime,
            deadline=time.monotonic() + 10,
        )
        assert acquired.extraction.text == "Public page\nExact fixture text."
        assert acquired.extraction.source_digest == sha256(BODY).hexdigest()
        assert acquired.observations[0].peer == ADDRESS
        receipt["extraction"] = "passed_with_explicit_runtime_profile"
        checks.append("real_fetch_and_network_denied_extract")


@pytest.mark.asyncio
async def test_scout_fetch_isolated_dns_tls_passage(tmp_path: Path) -> None:
    require_namespace()
    fingerprint = _fingerprints()
    checks: list[str] = []
    receipt: dict[str, object] = {
        "schema": "scout-fetch-local-proof@1",
        "status": "failed",
        "kernel": platform.release(),
        "python": platform.python_version(),
        "openssl": ssl.OPENSSL_VERSION,
        "dependency_versions": {name: version(name) for name in ("h11", "dnspython")},
        "network": "isolated-loopback-only",
        "authority": "synthetic-audit-gate-not-durable-production-authority",
        "source_fingerprints": fingerprint,
        "checks": checks,
        "extraction": "not_requested",
    }
    fixture: ScoutNetworkFixture | None = None
    try:
        async with asyncio.timeout(60):
            certificate, key = await asyncio.to_thread(make_certificate, tmp_path, "fixture", HOSTNAME, include_ip=True)
            wrong_cert, wrong_key = await asyncio.to_thread(
                make_certificate, tmp_path, "wrong", "wrong.example.com", include_ip=False
            )
            trust = tmp_path / "trust.pem"
            trust.write_bytes(certificate.read_bytes() + wrong_cert.read_bytes())
            context, wrong_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            wrong_context.load_cert_chain(wrong_cert, wrong_key)
            context.set_alpn_protocols(["http/1.1"])
            wrong_context.set_alpn_protocols(["http/1.1"])
            fixture = ScoutNetworkFixture(context, wrong_context)
            resolver = await fixture.start()

            await _prove_response_framings(fixture, resolver, trust, checks)
            await _prove_certificate_refusals(fixture, resolver, trust, wrong_cert, checks)
            await _prove_destination_refusals(fixture, resolver, trust, checks)
            await _prove_handshake_revocation_and_ip_identity(fixture, resolver, trust, checks)
            await _prove_extraction(resolver, trust, checks, receipt)
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
