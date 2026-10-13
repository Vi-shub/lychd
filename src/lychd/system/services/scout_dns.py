"""Two explicit TCP DNS questions through Scout's mandatory handoff gate.

This module never reads ambient resolver configuration, searches suffixes,
follows a referral, retries a contact, or caches an address observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rdata
import dns.rdataclass
import dns.rdatatype

from lychd.domain.scout.destination import (
    DestinationAddresses,
    DestinationCandidate,
    DestinationError,
    classify_destination_addresses,
    preflight_destination,
)
from lychd.domain.scout.transport import FetchCode, FetchError, NetworkTarget
from lychd.system.services.scout_socket import open_pinned_tcp

if TYPE_CHECKING:
    from collections.abc import Iterable

    from lychd.domain.scout.transport import NetworkGate
    from lychd.system.services.scout_socket import PinnedTcpStream

_MAX_MESSAGE = 16 * 1024
_DNS_HEADER_BYTES = 12
_MAX_RECORDS = 64
_MAX_ALIASES = 8


@dataclass(frozen=True, slots=True)
class ResolverEndpoint:
    """Host-selected numeric resolver endpoint, never implicit custody authority."""

    address: str = field(repr=False)
    port: int = 53


async def _read_exact(stream: PinnedTcpStream, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        part = await stream.receive(min(size - len(output), 16 * 1024))
        if not part or len(part) > size - len(output):
            raise FetchError(FetchCode.DNS)
        output.extend(part)
    return bytes(output)


def _addresses(query: dns.message.QueryMessage, wire: bytes) -> tuple[dns.name.Name, tuple[str, ...]]:
    """Accept one exact complete answer, including an explicit negative family."""
    try:
        response = dns.message.from_wire(wire, raise_on_truncation=True)
        if (
            not isinstance(response, dns.message.QueryMessage)
            or not query.is_response(response)
            or response.question != query.question
            or response.rcode() != dns.rcode.NOERROR
            or not response.flags & dns.flags.RA
            or response.edns != -1
            or response.had_tsig
            or sum(len(rrset) for section in response.sections for rrset in section) > _MAX_RECORDS
        ):
            raise FetchError(FetchCode.DNS)
        chain = response.resolve_chaining()
        if len(chain.cnames) > _MAX_ALIASES:
            raise FetchError(FetchCode.DNS)
        used = [*chain.cnames]
        if chain.answer is not None:
            used.append(chain.answer)
        if len(used) != len(response.answer) or any(rrset not in used for rrset in response.answer):
            raise FetchError(FetchCode.DNS)
        if any(len(rrset) != 1 for rrset in chain.cnames):
            raise FetchError(FetchCode.DNS)
        if chain.answer is None:
            # An empty answer alone cannot establish a complete NODATA family.
            negative = any(
                rrset.rdtype == dns.rdatatype.SOA
                and rrset.rdclass == dns.rdataclass.IN
                and len(rrset) == 1
                and chain.canonical_name.is_subdomain(rrset.name)
                for rrset in response.authority
            )
            if not negative:
                raise FetchError(FetchCode.DNS)
            return chain.canonical_name, ()
        records = cast("Iterable[dns.rdata.Rdata]", chain.answer)
        return chain.canonical_name, tuple(record.to_text() for record in records)
    except dns.exception.DNSException:
        raise FetchError(FetchCode.DNS) from None


async def _question(
    candidate: DestinationCandidate,
    rdtype: dns.rdatatype.RdataType,
    *,
    resolver: ResolverEndpoint,
    gate: NetworkGate,
    deadline: float,
) -> tuple[dns.name.Name, tuple[str, ...]]:
    query = dns.message.make_query(f"{candidate.hostname}.", rdtype, use_edns=False)
    wire = query.to_wire()
    target = NetworkTarget(resolver.address, resolver.port, candidate.hostname, "resolver")
    stream = await open_pinned_tcp(target, gate=gate, deadline=deadline)
    try:
        await stream.send(len(wire).to_bytes(2, "big") + wire)
        size = int.from_bytes(await _read_exact(stream, 2), "big")
        if not _DNS_HEADER_BYTES <= size <= _MAX_MESSAGE:
            raise FetchError(FetchCode.DNS)
        return _addresses(query, await _read_exact(stream, size))
    finally:
        stream.close()


async def resolve_destination(
    candidate: DestinationCandidate, *, resolver: ResolverEndpoint, gate: NetworkGate, deadline: float
) -> DestinationAddresses:
    """Observe both families from the selected resolver, then reject mixed scope.

    DNS messages are trusted only as observations from that configured resolver;
    this is not DNSSEC validation. HTTPS still checks the original hostname.
    A refusal in either family prevents all origin contact.
    """
    try:
        if type(candidate) is not DestinationCandidate or candidate != preflight_destination(candidate.original_url):
            raise FetchError(FetchCode.DESTINATION)
        if candidate.ip_literal is not None:
            return classify_destination_addresses(
                candidate, hostname=candidate.hostname, addresses=(candidate.ip_literal,)
            )
        canonical_a, addresses_a = await _question(
            candidate, dns.rdatatype.A, resolver=resolver, gate=gate, deadline=deadline
        )
        canonical_aaaa, addresses_aaaa = await _question(
            candidate, dns.rdatatype.AAAA, resolver=resolver, gate=gate, deadline=deadline
        )
        if canonical_a != canonical_aaaa:
            raise FetchError(FetchCode.DNS)
        return classify_destination_addresses(
            candidate, hostname=candidate.hostname, addresses=addresses_a + addresses_aaaa
        )
    except DestinationError:
        raise FetchError(FetchCode.DESTINATION) from None
