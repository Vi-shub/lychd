"""Finite Scavenger observation values; validation confers no effect or read authority."""

from __future__ import annotations

import json
import re
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Any, Literal, NoReturn, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lychd.domain.scout.destination import DestinationError, preflight_destination
from lychd.domain.scout.extraction import ExtractedFeed

COMPOSITION = "scavenger"
COMPOSITION_REVISION = "3"
PATTERN = "scavenger.observe_market"
PATTERN_REVISION = "2"
PROFILE = "bazos-pc-title-link@1"
SEED_BYTES = 16 * 1024
OBSERVATION_BYTES = 256 * 1024
MAX_ITEMS = 100
TITLE_BYTES = 1024
LINK_BYTES = 4096
FILTER_BYTES = 256
_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type Nonnegative = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class ScavengerRefused(ValueError):  # noqa: N818 - closed domain refusal
    """A content-free failure of the finite observation contract."""


def _refuse(code: str) -> NoReturn:
    raise ScavengerRefused(code) from None


def canonical(value: BaseModel | tuple[BaseModel, ...]) -> bytes:
    """Encode local application custody, not a portable signed Scroll encoding."""
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else [v.model_dump(mode="json") for v in value]
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _text(value: str, maximum: int, *, empty: bool = False) -> str:
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError:
        _refuse("text_encoding")
    if size > maximum or _CONTROLS.search(value) or (not empty and not value.strip()):
        _refuse("text_bounds")
    return value


def _locator(value: str) -> str:
    _text(value, LINK_BYTES)
    try:
        preflight_destination(value)
    except DestinationError:
        _refuse("locator")
    return value


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Preference(Record):
    """Declared soft preference only; this observation profile does not rank."""

    criterion: str = Field(repr=False)
    weight: Nonnegative

    @field_validator("criterion")
    @classmethod
    def criterion_bytes(cls, value: str) -> str:
        return _text(value, 1024)


class CampaignSeed(Record):
    """Complete supplied facts; explicit unknown budget/region establishes no eligibility."""

    schema_version: Literal["CampaignSeed@1"] = "CampaignSeed@1"
    subject_profile: Literal["Bazaar@1", "Technology@1"]
    need: str = Field(repr=False)
    inventory: tuple[str, ...] = Field(max_length=16, repr=False)
    hard_criteria: tuple[str, ...] = Field(max_length=16, repr=False)
    preferences: tuple[Preference, ...] = Field(max_length=16, repr=False)
    budget_minor: Nonnegative | None = Field(repr=False)
    currency: Literal["EUR"]
    deadline: datetime = Field(repr=False)
    region: str | None = Field(repr=False)
    evidence_standard: str = Field(repr=False)
    travel_delivery: str = Field(repr=False)
    source_policy: Literal["admitted-public-feed-only@1"]
    disclosure_policy: Literal["none"]
    learning_mode: Literal["none"]
    operating_mode: Literal["Watch"]

    @model_validator(mode="after")
    def bounds(self) -> Self:
        _text(self.need, 2048)
        for value in (*self.inventory, *self.hard_criteria, self.evidence_standard, self.travel_delivery):
            _text(value, 1024)
        if self.region is not None:
            _text(self.region, 256)
        if self.deadline.utcoffset() is None:
            _refuse("deadline_timezone")
        if len(canonical(self)) > SEED_BYTES:
            _refuse("seed_bytes")
        return self

    @property
    def digest(self) -> str:
        return digest(canonical(self))


class ObservationItem(Record):
    """One occurrence, identified within its observation by its source ordinal."""

    ordinal: int = Field(strict=True, ge=0, lt=MAX_ITEMS)
    duplicate_of: int | None = Field(strict=True, ge=0, lt=MAX_ITEMS)
    title: str | None = Field(repr=False)
    link: str = Field(repr=False)

    @field_validator("title")
    @classmethod
    def title_bytes(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, TITLE_BYTES)

    @field_validator("link")
    @classmethod
    def inert_link(cls, value: str) -> str:
        return _locator(value)


class MarketObservation(Record):
    """Whole finite title/link field with attribution, never an acquisition grant."""

    schema_version: Literal["MarketObservation@1"] = "MarketObservation@1"
    observation_id: UUID
    admission_id: UUID
    run_id: UUID
    seed_digest: Digest = Field(repr=False)
    pattern_digest: Digest = Field(repr=False)
    acquisition_binding_digest: Digest = Field(repr=False)
    profile: Literal["bazos-pc-title-link@1"] = PROFILE
    acquisition_profile: Literal["rss-public-feed@1"] = "rss-public-feed@1"
    extractor: Literal["scout-rss-extract@1"] = "scout-rss-extract@1"
    requested_url: str = Field(repr=False)
    contacted_url: str = Field(repr=False)
    acquired_at: datetime
    scout_receipt_id: UUID
    scout_receipt_digest: Digest = Field(repr=False)
    source_digest: Digest = Field(repr=False)
    record_digest: Digest = Field(repr=False)
    omitted_elements: int = Field(strict=True, ge=0, le=10000)
    coverage: Literal["bounded-feed-response-only"] = "bounded-feed-response-only"
    items: tuple[ObservationItem, ...] = Field(max_length=MAX_ITEMS, repr=False)

    @field_validator("requested_url", "contacted_url")
    @classmethod
    def inert_locator(cls, value: str) -> str:
        return _locator(value)

    @model_validator(mode="after")
    def whole_field(self) -> Self:
        if self.acquired_at.utcoffset() is None:
            _refuse("acquisition_timezone")
        if self.requested_url != self.contacted_url:
            _refuse("feed_redirect")
        first: dict[str, int] = {}
        for ordinal, item in enumerate(self.items):
            if item.ordinal != ordinal or item.duplicate_of != first.get(item.link):
                _refuse("field_order")
            first.setdefault(item.link, ordinal)
        if len(canonical(self)) > OBSERVATION_BYTES:
            _refuse("observation_bytes")
        return self

    @property
    def digest(self) -> str:
        return digest(canonical(self))

    @property
    def field_digest(self) -> str:
        return digest(canonical(self.items))


class LiteralFilter(Record):
    """Case-folded literal title/link substring; no regex or semantic judgment."""

    revision: Literal["literal-casefold@1"] = "literal-casefold@1"
    query: str = Field(repr=False)

    @field_validator("query")
    @classmethod
    def query_bytes(cls, value: str) -> str:
        return _text(value, FILTER_BYTES, empty=True)


class ObservationView(Record):
    """Derived ordinal selection over an unchanged whole field."""

    observation_id: UUID
    observation_digest: Digest = Field(repr=False)
    field_digest: Digest = Field(repr=False)
    filter: LiteralFilter = Field(repr=False)
    visible_ordinals: tuple[int, ...]
    total_count: Nonnegative
    visible_count: Nonnegative
    unmatched_count: Nonnegative
    duplicate_count: Nonnegative
    missing_title_count: Nonnegative


def local_view(observation: MarketObservation, selection: LiteralFilter) -> ObservationView:
    """Derive a pure projection; callers still need current application read authority."""
    query = selection.query.casefold()
    visible = tuple(
        item.ordinal
        for item in observation.items
        if query in (item.title or "").casefold() or query in item.link.casefold()
    )
    return ObservationView(
        observation_id=observation.observation_id,
        observation_digest=observation.digest,
        field_digest=observation.field_digest,
        filter=selection,
        visible_ordinals=visible,
        total_count=len(observation.items),
        visible_count=len(visible),
        unmatched_count=len(observation.items) - len(visible),
        duplicate_count=sum(item.duplicate_of is not None for item in observation.items),
        missing_title_count=sum(item.title is None for item in observation.items),
    )


def title_link_field(feed: ExtractedFeed) -> tuple[ObservationItem, ...]:
    """Discard excluded content without deriving authority from an extraction-shaped value."""
    if type(feed) is not ExtractedFeed or len(feed.items) > MAX_ITEMS:
        _refuse("extraction_shape")
    first: dict[str, int] = {}
    result: list[ObservationItem] = []
    for ordinal, item in enumerate(feed.items):
        result.append(
            ObservationItem(
                ordinal=ordinal, duplicate_of=first.get(item.link), title=item.title or None, link=item.link
            )
        )
        first.setdefault(item.link, ordinal)
    return tuple(result)


def decode(value: bytes, *, observation: bool = False) -> CampaignSeed | MarketObservation:
    """Bound stored local JSON and refuse ambiguous keys before model validation."""
    limit = OBSERVATION_BYTES if observation else SEED_BYTES
    if type(value) is not bytes or len(value) > limit:
        _refuse("record_bytes")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in values:
            if key in result:
                _refuse("duplicate_key")
            result[key] = item
        return result

    def invalid_constant(_value: str) -> Any:
        _refuse("invalid_json")

    try:
        data = json.loads(
            value.decode("utf-8", errors="strict"), object_pairs_hook=pairs, parse_constant=invalid_constant
        )
    except (UnicodeError, ValueError, RecursionError):
        _refuse("invalid_json")
    return MarketObservation.model_validate(data) if observation else CampaignSeed.model_validate(data)
