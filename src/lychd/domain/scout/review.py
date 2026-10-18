"""Complete inert native consent review from the immutable Scout preparation."""

from __future__ import annotations

from lychd.domain.codex.schemas import NativeConsentBinding
from lychd.domain.scout.attempts import ScoutAnyPreparation, ScoutFeedPreparation, ScoutPreparation, document_digest
from lychd.domain.scout.feed_review import feed_review
from lychd.domain.web.consent_reviews import NativeReviewDocument, ReviewFact


def page_review(preparation: ScoutPreparation) -> tuple[NativeConsentBinding, NativeReviewDocument]:
    """Bind the whole locator, recipient assertions, finite effects and local use."""
    p = preparation
    facts = (
        ("url", "Complete declared public URL", p.url),
        (
            "classification",
            "Classification basis",
            "Authenticated operator assertion of a public web locator; not a technical proof of publicness.",
        ),
        ("subject", "Local operator", p.subject),
        ("authentication", "Authentication generation", str(p.authentication_generation)),
        ("run", "Run", str(p.run_id)),
        ("request", "Acquisition request", str(p.request_id)),
        ("provider", "Native acquisition", f"{p.provider_id} / {p.profile}"),
        (
            "dns",
            "DNS recipient",
            f"TCP resolver {p.resolver_address}:{p.resolver_port}; exact hostname A and AAAA questions.",
        ),
        ("custody", "Operator-declared resolver custody", p.resolver_custody_statement),
        (
            "disclosure",
            "Website disclosure",
            "TLS exposes the hostname as SNI when it is not an IP literal. HTTPS sends the exact path and query to the certificate-verified, pinned public peer.",
        ),
        (
            "redirects",
            "Redirects and retries",
            "No redirects, automatic retry, provider fallback, browser rendering, scripts, cookies or page subresources.",
        ),
        (
            "limits",
            "Reserved network ceilings",
            f"Connects: {p.allowance.connects}; handoffs: {p.allowance.transmissions}; outbound payload bytes: {p.allowance.wire_bytes}. One 30-second acquisition deadline; response wire body at most 2 MiB, decoded body at most 4 MiB.",
        ),
        (
            "extraction",
            "Extraction",
            "Bounded HTML/plain-text extraction in the configured network-denied local worker. Page claims remain untrusted data; extraction does not remove every prompt injection.",
        ),
        ("model", "Local answer capability", p.capability_key),
        (
            "retention",
            "Material and answer retention",
            "Acquired bodies and extracted text are transient. The ordinary Bridge answer is retained and may quote the page. Retained references cannot authorize a refetch.",
        ),
        ("expiry", "Review and effect expiry", p.expires_at.isoformat()),
        (
            "withdrawal",
            "Withdraw approval",
            "Cancel the Run or revoke the current Scout policy. Cancellation stops future handoffs; it cannot retract bytes already accepted by the kernel.",
        ),
    )
    document = NativeReviewDocument(
        title="Read one explicitly declared public page and answer locally",
        facts=tuple(ReviewFact(key=key, label=label, value=value) for key, label, value in facts),
    )
    binding = NativeConsentBinding(
        owner="scout",
        action_id=str(p.attempt_id),
        preparation_digest=p.digest,
        review_profile="scout.public-page",
        review_profile_revision="1",
        review_producer="scout.public-page-review",
        review_producer_revision="1",
        audience_digest=document_digest(
            {"subject": p.subject, "authentication_generation": str(p.authentication_generation), "purpose": p.purpose}
        ),
        review_digest=document.digest,
    )
    return binding, document


def scout_review(preparation: ScoutAnyPreparation) -> tuple[NativeConsentBinding, NativeReviewDocument]:
    """Select by the decoded preparation class; never regenerate an old page as feed."""
    if type(preparation) is ScoutPreparation:
        return page_review(preparation)
    if type(preparation) is ScoutFeedPreparation:
        return feed_review(preparation)
    message = "Scout review preparation differs."
    raise ValueError(message)
