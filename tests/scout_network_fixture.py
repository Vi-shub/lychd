"""Bounded synthetic DNS/TLS fixture for opt-in isolated Scout network proofs.

Call ``require_namespace`` before starting the servers. The public-classified
address exists only on the isolated namespace's loopback interface. Generated
certificate keys remain in the caller's temporary test directory.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import socket
import ssl
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal, cast

import dns.flags
import dns.message
import dns.rdatatype
import dns.rrset
import pytest

from lychd.system.services import scout_dns

ADDRESS = "93.184.216.34"
HOSTNAME = "fixture.example.com"
BODY = b"<h1>Public page</h1><p>Exact fixture text.</p>"
_TOOL_SECONDS = 10


def require_namespace(opt_in_variable: str = "LYCHD_REQUIRE_SCOUT_FETCH_PROOF") -> None:
    """Refuse live acquisition unless the requested isolated namespace is observed."""
    if os.environ.get(opt_in_variable) != "1":
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
    assert any(item.get("local") == ADDRESS and item.get("prefixlen") == 32 for item in addresses)
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    assert all(line.split()[0] == "lo" and line.split()[2] == "00000000" for line in routes)


def certificate(directory: Path, stem: str, hostname: str, *, include_ip: bool) -> tuple[Path, Path]:
    certificate, key = directory / f"{stem}.pem", directory / f"{stem}.key"
    san = f"subjectAltName=DNS:{hostname}" + (f",IP:{ADDRESS}" if include_ip else "")
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


class ScoutNetworkFixture:
    def __init__(self, context: ssl.SSLContext, wrong_context: ssl.SSLContext) -> None:
        self.context = context
        self.wrong_context = wrong_context
        self.wrong_name = False
        self.on_sni: Callable[[], None] | None = None
        self.mixed_dns = False
        self.response = b""
        self.dns_connections = 0
        self.received_dns_bytes = 0
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
        if self.on_sni is not None:
            callback, self.on_sni = self.on_sni, None
            callback()
        if self.wrong_name:
            stream.context = self.wrong_context

    def _accept(self, kind: Literal["dns", "http"], reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if kind == "dns":
            self.dns_connections += 1
        task = asyncio.create_task(self._client(kind, reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _read_dns_message(self, reader: asyncio.StreamReader) -> bytes | None:
        try:
            prefix = await reader.readexactly(2)
        except asyncio.IncompleteReadError as error:
            self.received_dns_bytes += len(error.partial)
            if not error.partial:
                # Authority can refuse the first DNS write after connect.
                return None
            raise
        self.received_dns_bytes += len(prefix)
        size = int.from_bytes(prefix, "big")
        assert 12 <= size <= 16 * 1024
        try:
            query_wire = await reader.readexactly(size)
        except asyncio.IncompleteReadError as error:
            self.received_dns_bytes += len(error.partial)
            raise
        self.received_dns_bytes += len(query_wire)
        return query_wire

    async def _client(
        self, kind: Literal["dns", "http"], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writers.add(writer)
        try:
            async with asyncio.timeout(3):
                if kind == "dns":
                    query_wire = await self._read_dns_message(reader)
                    if query_wire is None:
                        return
                    query = dns.message.from_wire(query_wire)
                    assert len(query.question) == 1
                    question = query.question[0]
                    self.queries.append((question.name.to_text(), question.rdtype))
                    assert question.name.to_text() == HOSTNAME + "."
                    response = dns.message.make_response(query)
                    response.flags |= dns.flags.RA
                    if question.rdtype == dns.rdatatype.A:
                        addresses = (ADDRESS, "127.0.0.1") if self.mixed_dns else (ADDRESS,)
                        response.answer.append(dns.rrset.from_text(HOSTNAME + ".", 60, "IN", "A", *addresses))
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
                host=ADDRESS,
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


def response(kind: str) -> bytes:
    common = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
    if kind == "chunked":
        pieces = (BODY[:20], BODY[20:])
        chunks = b"".join(f"{len(piece):x}\r\n".encode() + piece + b"\r\n" for piece in pieces)
        return common + b"Transfer-Encoding: chunked\r\n\r\n" + chunks + b"0\r\n\r\n"
    if kind == "gzip":
        body = gzip.compress(BODY, mtime=0)
        return common + b"Content-Encoding: gzip\r\n" + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    if kind == "eof":
        return common + b"\r\n" + BODY
    return common + f"Content-Length: {len(BODY)}\r\n\r\n".encode() + BODY
