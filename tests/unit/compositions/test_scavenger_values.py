"""Whole-field, least-data and local filter boundaries without acquisition or storage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from lychd.compositions.scavenger.schemas import (
    OBSERVATION_BYTES,
    SEED_BYTES,
    CampaignSeed,
    LiteralFilter,
    MarketObservation,
    ObservationItem,
    ScavengerRefused,
    canonical,
    decode,
    digest,
    local_view,
    title_link_field,
)
from lychd.domain.scout.extraction import ExtractedFeed, FeedChannel, FeedItem

URL = "https://example.com/feed?exact=1&order=2"
HASH = "1" * 64


def seed_document() -> dict[str, Any]:
    return {
        "subject_profile": "Technology@1",
        "need": "Observe available graphics cards; fit remains unassessed.",
        "inventory": [],
        "hard_criteria": [],
        "preferences": [{"criterion": "Lower noise", "weight": 2}],
        "budget_minor": None,
        "currency": "EUR",
        "deadline": "2026-10-19T06:00:00Z",
        "region": None,
        "evidence_standard": "Unverified publisher title/link only",
        "travel_delivery": "Not assessed; no travel or delivery commitment",
        "source_policy": "admitted-public-feed-only@1",
        "disclosure_policy": "none",
        "learning_mode": "none",
        "operating_mode": "Watch",
    }


def observation_document() -> dict[str, Any]:
    return {
        "observation_id": str(UUID(int=1)),
        "admission_id": str(UUID(int=2)),
        "run_id": str(UUID(int=3)),
        "seed_digest": HASH,
        "pattern_digest": HASH,
        "acquisition_binding_digest": HASH,
        "requested_url": URL,
        "contacted_url": URL,
        "acquired_at": "2026-10-18T02:00:00Z",
        "scout_receipt_id": str(UUID(int=4)),
        "scout_receipt_digest": HASH,
        "source_digest": HASH,
        "record_digest": HASH,
        "omitted_elements": 7,
        "items": [
            {"ordinal": 0, "duplicate_of": None, "title": "Straße GPU", "link": "https://example.com/a?b=2&a=1"},
            {"ordinal": 1, "duplicate_of": None, "title": None, "link": "https://example.com/b"},
            {"ordinal": 2, "duplicate_of": 0, "title": "A later title", "link": "https://example.com/a?b=2&a=1"},
            {"ordinal": 3, "duplicate_of": None, "title": "Different query", "link": "https://example.com/a?a=1&b=2"},
        ],
    }


def test_complete_seed_roundtrips_and_explicit_unknowns_remain_unknown() -> None:
    seed = CampaignSeed.model_validate(seed_document())
    assert seed.budget_minor is None
    assert seed.region is None
    assert seed.inventory == seed.hard_criteria == ()
    assert seed.digest == digest(canonical(seed))
    assert decode(canonical(seed)) == seed
    # Historical seed decoding must not consult wall time or silently change its deadline.
    old = seed_document() | {"deadline": "2020-01-01T00:00:00Z"}
    assert CampaignSeed.model_validate(old).deadline.year == 2020
    assert "graphics cards" not in repr(seed)


@pytest.mark.parametrize("field", list(seed_document()))
def test_no_required_seed_fact_is_inferred(field: str) -> None:
    document = seed_document()
    del document[field]
    with pytest.raises(ValidationError):
        CampaignSeed.model_validate(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operating_mode", "Bounded Autopilot"),
        ("learning_mode", "auto"),
        ("disclosure_policy", "public"),
        ("source_policy", "any-public-web"),
        ("budget_minor", True),
        ("budget_minor", -1),
        ("budget_minor", 1.5),
        ("region", ""),
        ("need", "\x00private marker"),
        ("need", "é" * 1025),
        ("deadline", "2026-10-19T06:00:00"),
        ("preferences", [{"criterion": "Fast", "weight": True}]),
    ],
)
def test_seed_refuses_wider_or_ambiguous_values(field: str, value: Any) -> None:
    with pytest.raises((ValidationError, ScavengerRefused)):
        CampaignSeed.model_validate(seed_document() | {field: value})


def test_seed_and_observation_total_byte_bounds_refuse_without_truncation() -> None:
    seed = seed_document() | {"inventory": ["é" * 512] * 16}
    assert len(json.dumps(seed, ensure_ascii=False).encode()) > SEED_BYTES
    with pytest.raises((ValidationError, ScavengerRefused)):
        CampaignSeed.model_validate(seed)
    document = observation_document()
    document["items"] = [
        {"ordinal": n, "duplicate_of": None, "title": "é" * 512, "link": f"https://example.com/{n}/" + "x" * 3000}
        for n in range(100)
    ]
    assert len(json.dumps(document, ensure_ascii=False).encode()) > OBSERVATION_BYTES
    with pytest.raises((ValidationError, ScavengerRefused)):
        MarketObservation.model_validate(document)


def test_filter_keeps_full_field_counts_duplicate_occurrences_and_exact_query_meaning() -> None:
    observation = MarketObservation.model_validate(observation_document())
    before = canonical(observation)
    view = local_view(observation, LiteralFilter(query="STRASSE"))
    assert view.visible_ordinals == (0,)
    assert (view.total_count, view.visible_count, view.unmatched_count) == (4, 1, 3)
    assert (view.duplicate_count, view.missing_title_count) == (1, 1)
    assert local_view(observation, LiteralFilter(query="")).visible_ordinals == (0, 1, 2, 3)
    assert local_view(observation, LiteralFilter(query="a?b=2&a=1")).visible_ordinals == (0, 2)
    assert local_view(observation, LiteralFilter(query=".*")).visible_ordinals == ()
    assert canonical(observation) == before
    assert decode(before, observation=True) == observation
    assert view.field_digest == observation.field_digest
    assert "Straße" not in repr(observation)
    assert URL not in repr(observation)


@pytest.mark.parametrize("change", ["duplicate", "ordinal", "redirect", "timezone", "extra", "too_many"])
def test_whole_field_refuses_rewritten_identity_and_widened_shape(change: str) -> None:
    document = observation_document()
    if change == "duplicate":
        document["items"][2]["duplicate_of"] = None
    elif change == "ordinal":
        document["items"][0]["ordinal"] = 1
    elif change == "redirect":
        document["contacted_url"] = URL + "&other=1"
    elif change == "timezone":
        document["acquired_at"] = "2026-10-18T02:00:00"
    elif change == "extra":
        document["summary"] = "Unretained source text"
    else:
        document["items"] *= 26
    with pytest.raises((ValidationError, ScavengerRefused)):
        MarketObservation.model_validate(document)


@pytest.mark.parametrize("link", ["javascript:alert(1)", "http://example.com/item", "https://127.0.0.1/item"])
def test_item_links_are_inert_validated_https_syntax_without_contact(link: str) -> None:
    with pytest.raises((ValidationError, ScavengerRefused)):
        ObservationItem(ordinal=0, duplicate_of=None, title=None, link=link)


def test_projection_discards_channel_summary_publisher_identity_and_date() -> None:
    feed = ExtractedFeed(
        media_type="text/xml",
        channel=FeedChannel("channel-secret", "https://example.com", "channel-body-secret"),
        items=(
            FeedItem("Title", "https://example.com/item", "summary-secret", "date-secret", "guid-secret"),
            FeedItem("", "https://example.com/item", "other-summary", None, None),
        ),
        omitted_elements=1,
        source_digest=HASH,
        record_digest=HASH,
        strategy="rss2-expat-title-link-summary@1",
        transformations=(),
    )
    items = title_link_field(feed)
    assert len(items) == 2
    assert items[1].title is None
    assert items[1].duplicate_of == 0
    encoded = canonical(items)
    assert b"Title" in encoded
    for excluded in (b"channel-secret", b"summary-secret", b"date-secret", b"guid-secret"):
        assert excluded not in encoded
    assert feed.items[0].summary == "summary-secret"


@pytest.mark.parametrize("data", [b'{"need":"one","need":"two"}', b'{"value":NaN}', b"[" * 2000, b"\xff"])
def test_record_decoder_refuses_duplicate_keys_invalid_utf8_and_non_json(data: bytes) -> None:
    with pytest.raises(ScavengerRefused):
        decode(data)


def test_unicode_bounds_and_frozen_records_preserve_exact_source_values() -> None:
    link = "https://example.com/item?original=%C5%BE"
    item = ObservationItem(ordinal=0, duplicate_of=None, title="é" * 512, link=link)
    assert item.title == "é" * 512
    assert item.link == link
    with pytest.raises((ValidationError, ScavengerRefused)):
        ObservationItem(ordinal=0, duplicate_of=None, title="é" * 513, link=link)
    with pytest.raises(ValidationError):
        item.title = "Changed"
    with pytest.raises((ValidationError, ScavengerRefused)):
        LiteralFilter(query="é" * 129)
    observation = MarketObservation.model_validate(observation_document())
    assert observation.acquired_at == datetime(2026, 10, 18, 2, tzinfo=UTC)
