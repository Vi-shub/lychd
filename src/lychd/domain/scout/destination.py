"""Pure public-https-destination@1 checks, without resolution or contact authority."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from typing import Literal

_MAX_URL_BYTES = 4096
_MAX_ANSWERS = 64
_MAX_HOST_LENGTH = 253
_MAX_ADDRESS_LENGTH = 39
_URL = re.compile(r"https://([^/?#]+)([^#]*)")
_AUTHORITY = re.compile(r"(\[[0-9a-f:]+\]|[A-Za-z0-9.-]+)(?::443)?")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NUMERIC_LABEL = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")

# Conservative pinned policy, including globally reachable special-purpose entries.
# https://www.iana.org/assignments/iana-ipv4-special-registry/
# https://www.iana.org/assignments/iana-ipv6-special-registry/
# No mutable ipaddress.is_global/is_private classification tables are consulted.
_IPV4_EXCLUDED = tuple(
    IPv4Network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.31.196.0/24",
        "192.52.193.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "192.175.48.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "168.63.129.16/32",
    )
)
_IPV6_GLOBAL = IPv6Network("2000::/3")
_IPV6_EXCLUDED = tuple(
    IPv6Network(value)
    for value in ("2001::/23", "2001:db8::/32", "2002::/16", "2620:4f:8000::/48", "3ffe::/16", "3fff::/20")
)


class DestinationCode(StrEnum):
    """Closed, content-free preflight refusal reasons."""

    URL = "url"
    URL_LIMIT = "url_limit"
    AUTHORITY = "authority"
    ADDRESS = "address"
    FORBIDDEN = "forbidden"
    CANDIDATE = "candidate"
    HOST_MISMATCH = "host_mismatch"
    ANSWERS = "answers"
    LITERAL_MISMATCH = "literal_mismatch"


_MESSAGES = {
    DestinationCode.URL: "Destination requires canonical ASCII HTTPS URL text.",
    DestinationCode.URL_LIMIT: "Destination URL exceeds the 4 KiB limit.",
    DestinationCode.AUTHORITY: "Destination requires a canonical dotted host or IP literal on port 443.",
    DestinationCode.ADDRESS: "Destination address requires canonical IP text.",
    DestinationCode.FORBIDDEN: "Destination address is outside the public destination profile.",
    DestinationCode.CANDIDATE: "Destination candidate does not match the URL preflight facts.",
    DestinationCode.HOST_MISMATCH: "Supplied answers must identify the exact candidate hostname.",
    DestinationCode.ANSWERS: "Supplied answers require a tuple containing between 1 and 64 addresses.",
    DestinationCode.LITERAL_MISMATCH: "Supplied answers must match the destination IP literal.",
}


class DestinationError(ValueError):
    """Refuse without including a URL, hostname, or address in the message."""

    def __init__(self, code: DestinationCode) -> None:
        """Bind the fixed message to its refusal code."""
        self.code = code
        super().__init__(_MESSAGES[code])


@dataclass(frozen=True, slots=True)
class DestinationCandidate:
    """Immutable syntax facts, never a grant or proof of actual DNS resolution."""

    original_url: str
    normalized_url: str
    hostname: str
    ip_literal: str | None
    profile: Literal["public-https-destination@1"] = "public-https-destination@1"


@dataclass(frozen=True, slots=True)
class DestinationAddresses:
    """Checked supplied facts; completeness, freshness, and peer remain unproved."""

    candidate: DestinationCandidate
    addresses: tuple[str, ...]


def _address(value: str) -> IPv4Address | IPv6Address:
    if type(value) is not str or len(value) > _MAX_ADDRESS_LENGTH:
        raise DestinationError(DestinationCode.ADDRESS)
    try:
        address = ip_address(value)
    except ValueError:
        raise DestinationError(DestinationCode.ADDRESS) from None
    _require_public(address)
    if str(address) != value or "%" in value:
        raise DestinationError(DestinationCode.ADDRESS)
    return address


def _require_public(address: IPv4Address | IPv6Address) -> None:
    if isinstance(address, IPv4Address):
        forbidden = any(address in network for network in _IPV4_EXCLUDED)
    else:
        forbidden = address not in _IPV6_GLOBAL or any(address in network for network in _IPV6_EXCLUDED)
    if forbidden:
        raise DestinationError(DestinationCode.FORBIDDEN)


def _host(authority: str) -> tuple[str, str | None, str]:
    match = _AUTHORITY.fullmatch(authority)
    if match is None:
        raise DestinationError(DestinationCode.AUTHORITY)
    host = match[1]
    if host.startswith("["):
        address = _address(host[1:-1])
        if not isinstance(address, IPv6Address):
            raise DestinationError(DestinationCode.AUTHORITY)
        return str(address), str(address), host
    normalized = host.lower()
    if all(char in "0123456789." for char in normalized):
        _address(normalized)
        return normalized, normalized, normalized
    labels = normalized.split(".")
    if (
        len(normalized) > _MAX_HOST_LENGTH
        or "." not in normalized
        or _NUMERIC_LABEL.fullmatch(labels[-1]) is not None
        or any(_LABEL.fullmatch(label) is None for label in labels)
    ):
        raise DestinationError(DestinationCode.AUTHORITY)
    return normalized, None, normalized


def preflight_destination(url: str) -> DestinationCandidate:
    """Check one bounded locator, preserving the original path and query verbatim."""
    if type(url) is not str:
        raise DestinationError(DestinationCode.URL)
    if len(url) > _MAX_URL_BYTES:
        raise DestinationError(DestinationCode.URL_LIMIT)
    if any(not "!" <= char <= "~" or char == "\\" for char in url) or _BAD_ESCAPE.search(url):
        raise DestinationError(DestinationCode.URL)
    match = _URL.fullmatch(url)
    if match is None:
        raise DestinationError(DestinationCode.URL)
    hostname, literal, authority = _host(match[1])
    return DestinationCandidate(url, f"https://{authority}{match[2]}", hostname, literal)


def classify_destination_addresses(
    candidate: DestinationCandidate, *, hostname: str, addresses: tuple[str, ...]
) -> DestinationAddresses:
    """Check a complete supplied answer set without resolving or authorizing contact.

    The future trusted resolver must establish completeness and freshness. Every
    connection still requires independent authority, pinning, and peer/TLS checks.
    Duplicate entries count against the bound before ordered deduplication.
    """
    if type(candidate) is not DestinationCandidate or candidate != preflight_destination(candidate.original_url):
        raise DestinationError(DestinationCode.CANDIDATE)
    if type(hostname) is not str or hostname != candidate.hostname:
        raise DestinationError(DestinationCode.HOST_MISMATCH)
    if type(addresses) is not tuple or not 1 <= len(addresses) <= _MAX_ANSWERS:
        raise DestinationError(DestinationCode.ANSWERS)
    for value in addresses:
        _address(value)
        if candidate.ip_literal is not None and value != candidate.ip_literal:
            raise DestinationError(DestinationCode.LITERAL_MISMATCH)
    return DestinationAddresses(candidate, tuple(dict.fromkeys(addresses)))
