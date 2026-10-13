"""Opt-in real Scout DNS/TLS proof, exclusively in a pre-isolated network namespace.

The numeric public-classified address is assigned to the namespace's only loopback
interface. No destination or transport is mocked. The audit gate is a synthetic
fixture, not production durable authority. Ephemeral certificate keys stay in the
test directory and are excluded from the sanitized receipt.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import platform
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Literal, cast

import dns.flags
import dns.message
import dns.rdatatype
import dns.rrset
import pytest

from lychd.domain.scout import destination, transport
from lychd.domain.scout.destination import preflight_destination
from lychd.domain.scout.transport import FetchCode, FetchError, NetworkHandoff
from lychd.system import scout_worker
from lychd.system.services import scout_dns, scout_extraction, scout_fetch, scout_http, scout_socket

pytestmark = [pytest.mark.integration, pytest.mark.container]

_ADDRESS = "93.184.216.34"
_HOSTNAME = "fixture.example.com"
_BODY = b"<h1>Public page</h1><p>Exact fixture text.</p>"
_TOOL_SECONDS = 10


def _require_namespace() -> None:
    if os.environ.get("LYCHD_REQUIRE_SCOUT_FETCH_PROOF") != "1":
        pytest.skip("Explicit isolated Scout acquisition qualification is not requested.")
    assert sys.platform == "linux", "Qualification requires Linux network namespaces."
    outer = os.environ["LYCHD_SCOUT_HOST_NETNS"]
    assert outer.startswith("net:[")
    assert outer.endswith("]")
    assert str(Path("/proc/self/ns/net").readlink()) != outer, "Qualification must not use the host network namespace."
    assert [name for _, name in socket.if_nameindex()] == ["lo"], "Only isolated loopback may be present."
    observed = subprocess.run(
        ["/usr/bin/ip", "-j", "address", "show", "dev", "lo"],
        capture_output=True,
        check=True,
        timeout=_TOOL_SECONDS,
        env={},
    )
    assert len(observed.stdout) <= 16 * 1024
    interfaces = json.loads(observed.stdout)
    assert len(interfaces) == 1
    assert interfaces[0]["ifname"] == "lo"
    assert "UP" in interfaces[0]["flags"]
    addresses = interfaces[0]["addr_info"]
    assert len(addresses) <= 8
    assert any(item.get("local") == _ADDRESS and item.get("prefixlen") == 32 for item in addresses)
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    assert all(line.split()[0] == "lo" and line.split()[2] == "00000000" for line in routes)


def _certificate(directory: Path, stem: str, hostname: str, *, include_ip: bool) -> tuple[Path, Path]:
    certificate, key = directory / f"{stem}.pem", directory / f"{stem}.key"
    san = f"subjectAltName=DNS:{hostname}" + (f",IP:{_ADDRESS}" if include_ip else "")
    subprocess.run(  # noqa: S603 - Literal OpenSSL arguments create synthetic local certificates.
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-noenc",
            "-batch",
            "-days",
            "1",
            "-subj",
            f"/CN={hostname}",
            "-addext",
            san,
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=_TOOL_SECONDS,
        env={},
        cwd=directory,
    )
    key.chmod(0o600)
    return certificate, key


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
            assert (target.address, target.port) == (_ADDRESS, 443)
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


class _Fixture:
    def __init__(self, context: ssl.SSLContext, wrong_context: ssl.SSLContext) -> None:
        self.context = context
        self.wrong_context = wrong_context
        self.wrong_name = False
        self.revoke_at_sni: _AuditGate | None = None
        self.mixed_dns = False
        self.response = b""
        self.queries: list[tuple[str, int]] = []
        self.requests: list[bytes] = []
        self.received_http_bytes = 0
        self.sni: list[str | None] = []
        self.versions: set[str] = set()
        self.failures: list[str] = []
        self.tasks: set[asyncio.Task[None]] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.servers: list[asyncio.Server] = []
        self.context.set_servername_callback(self._sni)

    def _sni(self, stream: ssl.SSLSocket | ssl.SSLObject, name: str | None, context: object) -> None:
        _ = context
        self.sni.append(name)
        if self.revoke_at_sni is not None:
            assert not self.revoke_at_sni.revoke_http
            self.revoke_at_sni.revoke_http = True
            self.revoke_at_sni = None
        if self.wrong_name:
            stream.context = self.wrong_context

    def _accept(self, kind: Literal["dns", "http"], reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._client(kind, reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _client(
        self, kind: Literal["dns", "http"], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writers.add(writer)
        try:
            async with asyncio.timeout(3):
                if kind == "dns":
                    size = int.from_bytes(await reader.readexactly(2), "big")
                    assert 12 <= size <= 16 * 1024
                    query = dns.message.from_wire(await reader.readexactly(size))
                    assert len(query.question) == 1
                    question = query.question[0]
                    self.queries.append((question.name.to_text(), question.rdtype))
                    assert question.name.to_text() == _HOSTNAME + "."
                    response = dns.message.make_response(query)
                    response.flags |= dns.flags.RA
                    if question.rdtype == dns.rdatatype.A:
                        addresses = (_ADDRESS, "127.0.0.1") if self.mixed_dns else (_ADDRESS,)
                        response.answer.append(dns.rrset.from_text(_HOSTNAME + ".", 60, "IN", "A", *addresses))
                    else:
                        assert question.rdtype == dns.rdatatype.AAAA
                        response.authority.append(
                            dns.rrset.from_text(
                                "example.com.",
                                60,
                                "IN",
                                "SOA",
                                "ns.example.com. hostmaster.example.com. 1 3600 600 86400 60",
                            )
                        )
                    wire = response.to_wire()
                    writer.write(len(wire).to_bytes(2, "big") + wire)
                else:
                    tls = cast("ssl.SSLObject", writer.get_extra_info("ssl_object"))
                    version = tls.version()
                    assert version is not None
                    self.versions.add(version)
                    try:
                        request = await reader.readuntil(b"\r\n\r\n")
                    except asyncio.IncompleteReadError as error:
                        self.received_http_bytes += len(error.partial)
                        return
                    self.received_http_bytes += len(request)
                    assert len(request) <= 8192
                    self.requests.append(request)
                    writer.write(self.response)
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            # Certificate rejection and deliberate pre-HTTP revocation close early.
            pass
        except Exception as error:  # noqa: BLE001 - Fail the owning test on any synthetic server failure.
            self.failures.append(type(error).__name__)
        finally:
            writer.close()
            with suppress(TimeoutError, ConnectionError, ssl.SSLError):
                await asyncio.wait_for(writer.wait_closed(), 1)
            self.writers.discard(writer)

    async def start(self) -> scout_dns.ResolverEndpoint:
        dns_server = await asyncio.start_server(
            lambda reader, writer: self._accept("dns", reader, writer),
            host="127.0.0.1",
            port=0,
            family=socket.AF_INET,
            limit=16 * 1024,
        )
        self.servers.append(dns_server)
        self.servers.append(
            await asyncio.start_server(
                lambda reader, writer: self._accept("http", reader, writer),
                host=_ADDRESS,
                port=443,
                family=socket.AF_INET,
                ssl=self.context,
                ssl_handshake_timeout=2,
                ssl_shutdown_timeout=1,
                limit=8192,
            )
        )
        return scout_dns.ResolverEndpoint("127.0.0.1", dns_server.sockets[0].getsockname()[1])

    async def settle(self) -> None:
        await asyncio.sleep(0)
        async with asyncio.timeout(4):
            while self.tasks:
                await asyncio.gather(*tuple(self.tasks))
        assert not self.failures, "The bounded synthetic server reported a fixture failure."

    async def close(self) -> None:
        for server in self.servers:
            server.close()
        for writer in tuple(self.writers):
            writer.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        async with asyncio.timeout(4):
            await asyncio.gather(*(server.wait_closed() for server in self.servers))


def _response(kind: str) -> bytes:
    common = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
    if kind == "chunked":
        pieces = (_BODY[:20], _BODY[20:])
        chunks = b"".join(f"{len(piece):x}\r\n".encode() + piece + b"\r\n" for piece in pieces)
        return common + b"Transfer-Encoding: chunked\r\n\r\n" + chunks + b"0\r\n\r\n"
    if kind == "gzip":
        body = gzip.compress(_BODY, mtime=0)
        return common + b"Content-Encoding: gzip\r\n" + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    if kind == "eof":
        return common + b"\r\n" + _BODY
    return common + f"Content-Length: {len(_BODY)}\r\n\r\n".encode() + _BODY


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
    fixture: _Fixture, resolver: scout_dns.ResolverEndpoint, trust: Path, checks: list[str]
) -> None:
    for kind in ("length", "chunked", "gzip", "eof"):
        candidate = preflight_destination(f"https://{_HOSTNAME}/{kind}?exact=1")
        request = scout_http.serialize_get(candidate)
        gate = _AuditGate(resolver, _HOSTNAME, request)
        fixture.response = _response(kind)
        before = len(fixture.queries)
        page = await scout_fetch.fetch_static_page(
            candidate,
            resolver=resolver,
            gate=gate,
            ca_file=trust,
            deadline=time.monotonic() + 5,
        )
        await fixture.settle()
        assert page.body == _BODY
        assert page.media_type == "text/html"
        assert fixture.queries[before:] == [
            (_HOSTNAME + ".", dns.rdatatype.A),
            (_HOSTNAME + ".", dns.rdatatype.AAAA),
        ]
        assert fixture.requests[-1] == request
        assert fixture.sni[-1] == _HOSTNAME
        assert b"Host: fixture.example.com\r\n" in request
        assert page.observations[0].addresses == (_ADDRESS,)
        assert page.observations[0].peer == _ADDRESS
        assert page.observations[0].http_response_digest == sha256(fixture.response).hexdigest()
        assert gate.http_actions > 0
        checks.append(f"real_dns_tls_{kind}")


async def _prove_certificate_refusals(
    fixture: _Fixture, resolver: scout_dns.ResolverEndpoint, trust: Path, wrong_cert: Path, checks: list[str]
) -> None:
    candidate = preflight_destination(f"https://{_HOSTNAME}/negative")
    request = scout_http.serialize_get(candidate)
    for wrong_name, roots in ((True, trust), (False, wrong_cert)):
        fixture.wrong_name = wrong_name
        before = len(fixture.requests)
        with pytest.raises(FetchError) as error:
            await scout_fetch.fetch_static_page(
                candidate,
                resolver=resolver,
                gate=_AuditGate(resolver, _HOSTNAME, request),
                ca_file=roots,
                deadline=time.monotonic() + 5,
            )
        assert error.value.code is FetchCode.TLS
        assert len(fixture.requests) == before
    checks.append("wrong_hostname_and_untrusted_certificate_refuse")
    fixture.wrong_name = False


async def _prove_destination_refusals(
    fixture: _Fixture, resolver: scout_dns.ResolverEndpoint, trust: Path, checks: list[str]
) -> None:
    candidate = preflight_destination(f"https://{_HOSTNAME}/negative")
    request = scout_http.serialize_get(candidate)
    fixture.mixed_dns = True
    gate = _AuditGate(resolver, _HOSTNAME, request)
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
            gate=_AuditGate(resolver, _HOSTNAME, request),
            ca_file=trust,
            deadline=time.monotonic() + 5,
        )
    assert error.value.code is FetchCode.REDIRECT
    assert len(fixture.queries) - before == 2
    checks.append("unlisted_redirect_prevents_new_dns")


async def _prove_handshake_revocation_and_ip_identity(
    fixture: _Fixture, resolver: scout_dns.ResolverEndpoint, trust: Path, checks: list[str]
) -> None:
    candidate = preflight_destination(f"https://{_HOSTNAME}/negative")
    request = scout_http.serialize_get(candidate)
    fixture.response = _response("length")
    gate = _AuditGate(resolver, _HOSTNAME, request)
    assert not gate.revoke_http
    fixture.revoke_at_sni = gate
    before = fixture.received_http_bytes
    with pytest.raises(FetchError) as error:
        await scout_fetch.fetch_static_page(
            candidate, resolver=resolver, gate=gate, ca_file=trust, deadline=time.monotonic() + 5
        )
    assert error.value.code is FetchCode.EGRESS
    await fixture.settle()
    assert gate.revoke_http
    assert fixture.revoke_at_sni is None
    assert gate.http_actions == 0
    assert fixture.received_http_bytes == before
    checks.append("sni_time_revocation_prevents_actual_http_send")

    literal = preflight_destination(f"https://{_ADDRESS}/ip")
    before = len(fixture.queries)
    page = await scout_fetch.fetch_static_page(
        literal,
        resolver=resolver,
        gate=_AuditGate(resolver, _ADDRESS, scout_http.serialize_get(literal)),
        ca_file=trust,
        deadline=time.monotonic() + 5,
    )
    await fixture.settle()
    assert page.body == _BODY
    assert len(fixture.queries) == before
    assert fixture.sni[-1] is None
    assert b"Host: 93.184.216.34\r\n" in fixture.requests[-1]
    checks.append("ip_certificate_identity_without_dns_or_ip_sni")


async def _prove_extraction(
    resolver: scout_dns.ResolverEndpoint, trust: Path, checks: list[str], receipt: dict[str, object]
) -> None:
    candidate = preflight_destination(f"https://{_HOSTNAME}/negative")
    request = scout_http.serialize_get(candidate)
    runtime = _extraction_runtime()
    if runtime is not None:
        acquired = await scout_fetch.acquire_static_text(
            candidate,
            resolver=resolver,
            gate=_AuditGate(resolver, _HOSTNAME, request),
            ca_file=trust,
            runtime=runtime,
            deadline=time.monotonic() + 10,
        )
        assert acquired.extraction.text == "Public page\nExact fixture text."
        assert acquired.extraction.source_digest == sha256(_BODY).hexdigest()
        assert acquired.observations[0].peer == _ADDRESS
        receipt["extraction"] = "passed_with_explicit_runtime_profile"
        checks.append("real_fetch_and_network_denied_extract")


@pytest.mark.asyncio
async def test_scout_fetch_isolated_dns_tls_passage(tmp_path: Path) -> None:
    _require_namespace()
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
    fixture: _Fixture | None = None
    try:
        async with asyncio.timeout(60):
            certificate, key = await asyncio.to_thread(_certificate, tmp_path, "fixture", _HOSTNAME, include_ip=True)
            wrong_cert, wrong_key = await asyncio.to_thread(
                _certificate, tmp_path, "wrong", "wrong.example.com", include_ip=False
            )
            trust = tmp_path / "trust.pem"
            trust.write_bytes(certificate.read_bytes() + wrong_cert.read_bytes())
            context, wrong_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            wrong_context.load_cert_chain(wrong_cert, wrong_key)
            context.set_alpn_protocols(["http/1.1"])
            wrong_context.set_alpn_protocols(["http/1.1"])
            fixture = _Fixture(context, wrong_context)
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
