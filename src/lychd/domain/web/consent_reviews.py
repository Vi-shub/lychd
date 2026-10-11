"""Complete inert native-review transport and the explicitly injected owner port.

The producer owns trusted preparation reads and current object authorization. The
shared resolver verifies identity and content; it grants no native effect authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol

from lychd.domain.codex.schemas import ConsentView, NativeConsentBinding
from lychd.domain.codex.sigil import Sigil

_FACT_LIMIT = 64
_DOCUMENT_BYTE_LIMIT = 65536


class ConsentReviewUnavailableError(ValueError):
    """No complete review matching the retained binding can be disclosed."""


class ConsentReviewForbiddenError(PermissionError):
    """The owner refused the current reviewer or requested purpose."""


class ConsentReviewFailureError(RuntimeError):
    """The injected owner failed without exposing its private diagnostics."""


def _bounded_text(value: str, maximum: int) -> None:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        message = "Invalid native review text."
        raise ValueError(message)


@dataclass(frozen=True, kw_only=True)
class ReviewFact:
    """One labelled plain-text fact; clients must render the value as inert text."""

    key: str
    label: str
    value: str

    def __post_init__(self) -> None:
        """Reject incomplete or unbounded transport values before disclosure."""
        _bounded_text(self.key, 100)
        _bounded_text(self.label, 200)
        _bounded_text(self.value, 8192)


@dataclass(frozen=True, kw_only=True)
class NativeReviewDocument:
    """Complete disclosure approved by its owner, with no arbitrary metadata."""

    title: str
    facts: tuple[ReviewFact, ...]

    def __post_init__(self) -> None:
        """Reject incomplete or unbounded transport values before disclosure."""
        _bounded_text(self.title, 200)
        if type(self.facts) is not tuple or not 1 <= len(self.facts) <= _FACT_LIMIT:
            message = "Invalid native review facts."
            raise ValueError(message)
        if any(type(fact) is not ReviewFact for fact in self.facts):
            message = "Invalid native review fact."
            raise ValueError(message)
        if len({fact.key for fact in self.facts}) != len(self.facts):
            message = "Native review fact keys must be unique."
            raise ValueError(message)
        if len(self.canonical_bytes()) > _DOCUMENT_BYTE_LIMIT:
            message = "Native review exceeds the document limit."
            raise ValueError(message)

    def canonical_bytes(self) -> bytes:
        """Return the explicit schema-1 UTF-8 representation, without truncation."""
        document = {
            "schema_version": 1,
            "title": self.title,
            "facts": [{"key": fact.key, "label": fact.label, "value": fact.value} for fact in self.facts],
        }
        return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )

    @property
    def digest(self) -> str:
        """Digest complete review bytes with their own schema and domain prefix."""
        return hashlib.sha256(b"lychd.consent-review.v1\n" + self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, kw_only=True)
class NativeReviewResolution:
    """Owner resolution for this reviewer and purpose, never cached in events."""

    binding: NativeConsentBinding
    document: NativeReviewDocument | None
    decision_allowed: bool


class ConsentReviewProducer(Protocol):
    """Read committed owner records and check current reviewer authority last."""

    async def resolve(
        self, *, consent: ConsentView, reviewer: Sigil, purpose: Literal["view", "decide"]
    ) -> NativeReviewResolution:
        """Disclose a complete bound review or refuse without leaking owner details."""
        ...


async def resolve_native_review(
    producer: ConsentReviewProducer | None,
    *,
    consent: ConsentView,
    reviewer: Sigil,
    purpose: Literal["view", "decide"],
) -> NativeReviewResolution:
    """Verify the same exact content and identity for disclosure and judgment."""
    if producer is None or not consent.native_review_required or consent.native_binding is None:
        message = "Native review is unavailable."
        raise ConsentReviewUnavailableError(message)
    try:
        result = await producer.resolve(consent=consent, reviewer=reviewer, purpose=purpose)
    except (ConsentReviewUnavailableError, ConsentReviewForbiddenError):
        raise
    except Exception as exc:
        message = "Native review producer failed."
        raise ConsentReviewFailureError(message) from exc
    if (
        type(result) is not NativeReviewResolution
        or result.binding != consent.native_binding
        or not isinstance(result.document, NativeReviewDocument)
        or result.document.digest != consent.native_binding.review_digest
        or type(result.decision_allowed) is not bool
    ):
        message = "Native review is unavailable."
        raise ConsentReviewUnavailableError(message)
    if purpose == "decide" and not result.decision_allowed:
        message = "Native review judgment is forbidden."
        raise ConsentReviewForbiddenError(message)
    return result
