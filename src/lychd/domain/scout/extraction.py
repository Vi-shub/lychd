"""Attributed extraction values, without acquisition or custody authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

type ExtractionMedia = Literal["text/plain", "text/html"]
type RssMedia = Literal["text/xml", "application/xml", "application/rss+xml"]


class ExtractionCode(StrEnum):
    """Closed supervisor outcomes; foreign diagnostics never become messages."""

    INPUT = "input"
    MEDIA = "media"
    CONTAINMENT = "containment"
    TIMEOUT = "timeout"
    OUTPUT = "output"
    PROTOCOL = "protocol"
    WORKER = "worker"
    CLEANUP = "cleanup"


class ExtractionError(ValueError):
    """A content-free refusal; CLEANUP means owned work is not proved settled."""

    def __init__(self, code: ExtractionCode, *, worker_code: str | None = None) -> None:
        """Retain a closed outcome without embedding content or child diagnostics."""
        self.code = code
        self.worker_code = worker_code
        super().__init__(f"Scout extraction refused: {code.value}.")


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """Ephemeral untrusted data, not a grant, artifact, or semantic-loss measure.

    The source digest identifies supplied decoded bytes, not network wire bytes.
    The record digest identifies the exact UTF-8 worker record including its LF;
    the text digest separately identifies the returned text encoded as UTF-8.
    The acquisition owner must attach its own locator and attempt attribution.
    """

    media_type: ExtractionMedia
    text: str = field(repr=False)
    source_digest: str = field(repr=False)
    record_digest: str = field(repr=False)
    text_digest: str = field(repr=False)
    strategy: str
    transformations: tuple[str, ...]
    extractor: Literal["scout-static-extract@1"] = "scout-static-extract@1"


@dataclass(frozen=True, slots=True)
class FeedChannel:
    """Publisher-supplied text and locator, without contact or trust authority."""

    title: str = field(repr=False)
    link: str = field(repr=False)
    summary: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One untrusted observation; dates and identifiers remain publisher claims."""

    title: str = field(repr=False)
    link: str = field(repr=False)
    summary: str = field(repr=False)
    published: str | None = field(repr=False)
    identifier: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExtractedFeed:
    """Ephemeral RSS observations, without source eligibility or byte custody.

    Digests identify supplied decoded bytes and the complete worker record, not
    network wire bytes. The acquisition owner supplies locator/attempt attribution.
    Item locators confer no follow-up contact permission; a finite response does
    not establish coverage of the source website.
    """

    media_type: RssMedia
    channel: FeedChannel = field(repr=False)
    items: tuple[FeedItem, ...] = field(repr=False)
    omitted_elements: int
    source_digest: str = field(repr=False)
    record_digest: str = field(repr=False)
    strategy: str
    transformations: tuple[str, ...]
    extractor: Literal["scout-rss-extract@1"] = "scout-rss-extract@1"
