"""Deterministic exact-span replacement; this module makes no privacy verdict."""

from __future__ import annotations

from lychd.domain.privacy.contracts import (
    LANES,
    PROFILE,
    REDACTION,
    DisclosurePlan,
    PreparationPolicy,
    TransformationReceipt,
    WholeProjection,
    bytes_digest,
    canonical_bytes,
    digest,
    operation_counts,
    validate_plan,
)


def censor(
    projection: WholeProjection, plan: DisclosurePlan, policy: PreparationPolicy, *, now: int
) -> tuple[bytes, tuple[TransformationReceipt, ...]]:
    """Return a fresh complete branch and ordered non-authorizing receipts."""
    validate_plan(projection, plan, policy, now)
    payload: dict[str, str | list[str]] = {"profile": PROFILE}
    for lane in LANES:
        rendered: list[str] = []
        for material in getattr(projection, lane):
            text = material.text
            for span in sorted(material.sensitive_spans, key=lambda item: item.start, reverse=True):
                text = text[: span.start] + REDACTION + text[span.end :]
            rendered.append(text)
        payload[lane] = rendered
    candidate = canonical_bytes(payload)
    validation = TransformationReceipt(
        source_digest=plan.source_digest,
        plan_digest=digest(plan),
        policy_digest=plan.policy_digest,
        influence_label_digest=plan.influence_label_digest,
        input_digest=plan.source_digest,
        sequence=0,
        operation="validate-closed-grammar",
        output_digest=plan.source_digest,
        parent_receipt_digests=(),
        operations=(),
    )
    replacement = TransformationReceipt(
        source_digest=plan.source_digest,
        plan_digest=digest(plan),
        policy_digest=plan.policy_digest,
        influence_label_digest=plan.influence_label_digest,
        input_digest=plan.source_digest,
        sequence=1,
        operation="replace-sensitive-spans",
        output_digest=bytes_digest(candidate),
        parent_receipt_digests=(digest(validation),),
        operations=operation_counts(projection),
    )
    return candidate, (validation, replacement)
