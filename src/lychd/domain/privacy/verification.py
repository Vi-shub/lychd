"""Independent local verification of the closed diagnostic fixture's exact output.

The verifier never invokes the applicator and does not accept its success claim.
It reconstructs the allowed output from the closed grammar, validates the complete
ordered receipt chain, and searches the complete sanitized projection for every
explicitly declared sensitive value. It makes no semantic anonymity claim.
"""

from __future__ import annotations

from pydantic import ValidationError

from lychd.domain.privacy.contracts import (
    LANES,
    MAX_CANDIDATE_BYTES,
    REDACTION,
    CutVerification,
    DisclosurePlan,
    PreparationPolicy,
    PrivacyRefused,
    TransformationReceipt,
    UtilitySpan,
    WholeProjection,
    WireProjection,
    bytes_digest,
    canonical_bytes,
    digest,
    operation_counts,
    validate_plan,
)
from lychd.domain.privacy.labels import PrivacyValue


def checked[T: PrivacyValue](value: T) -> T:
    """Revalidate copied/constructed models at a trust boundary without raw errors."""
    try:
        return type(value).model_validate(value.model_dump(mode="python", warnings=False))
    except (ValidationError, TypeError, ValueError):
        msg = "invalid-contract"
        raise PrivacyRefused(msg) from None


def verify_candidate(
    projection: WholeProjection,
    plan: DisclosurePlan,
    candidate: bytes,
    receipts: tuple[TransformationReceipt, ...],
    policy: PreparationPolicy,
    *,
    now: int,
) -> CutVerification:
    """Prove declared removal and exact utility under this one bounded threat model."""
    if len(receipts) != 2:  # noqa: PLR2004 - exact two-stage profile
        msg = "receipt-chain"
        raise PrivacyRefused(msg)
    projection, plan, policy = checked(projection), checked(plan), checked(policy)
    receipts = tuple(checked(receipt) for receipt in receipts)
    validate_plan(projection, plan, policy, now)
    payload = _read_candidate(candidate)
    sensitive_values: list[str] = []
    for lane in LANES:
        expected: list[str] = []
        for material in getattr(projection, lane):
            ordered = sorted((*material.utility_spans, *material.sensitive_spans), key=lambda span: span.start)
            expected.append("".join(span.token if isinstance(span, UtilitySpan) else REDACTION for span in ordered))
            sensitive_values.extend(material.text[span.start : span.end] for span in material.sensitive_spans)
        if getattr(payload, lane) != tuple(expected):
            msg = "utility-or-transformation"
            raise PrivacyRefused(msg)
    # Inspect across every component and adjacent-component boundary, never only
    # the field in which the source happened to declare the sensitive span.
    whole_text = "".join(text for lane in LANES for text in getattr(payload, lane))
    wire_text = candidate.decode("utf-8")
    if any(value in whole_text or value in wire_text for value in sensitive_values):
        msg = "residual-sensitive-value"
        raise PrivacyRefused(msg)
    _verify_receipts(projection, plan, candidate, receipts)
    return CutVerification(
        source_digest=plan.source_digest,
        source_manifest_digest=plan.source_manifest_digest,
        plan_digest=digest(plan),
        influence_label_digest=plan.influence_label_digest,
        candidate_digest=bytes_digest(candidate),
        receipt_chain_digest=digest(receipts),
        terminal_receipt_digest=digest(receipts[-1]),
        policy_digest=digest(policy),
        verified_at=now,
        expires_at=plan.expires_at,
    )


def _read_candidate(candidate: object) -> WireProjection:
    if not isinstance(candidate, bytes) or len(candidate) > MAX_CANDIDATE_BYTES:
        msg = "candidate-bound"
        raise PrivacyRefused(msg)
    try:
        payload = WireProjection.model_validate_json(candidate)
        valid = canonical_bytes(payload) == candidate
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        msg = "candidate-schema"
        raise PrivacyRefused(msg) from None
    if not valid:
        msg = "candidate-schema"
        raise PrivacyRefused(msg)
    return payload


def _verify_receipts(
    projection: WholeProjection,
    plan: DisclosurePlan,
    candidate: bytes,
    receipts: tuple[TransformationReceipt, ...],
) -> None:
    if len(receipts) != 2:  # noqa: PLR2004 - exact two-stage profile
        msg = "receipt-chain"
        raise PrivacyRefused(msg)
    for index, receipt in enumerate(receipts):
        if (
            receipt.sequence != index
            or receipt.source_digest != plan.source_digest
            or receipt.plan_digest != digest(plan)
            or receipt.policy_digest != plan.policy_digest
            or receipt.influence_label_digest != plan.influence_label_digest
            or receipt.parent_receipt_digests != tuple(digest(parent) for parent in receipts[:index])
            or receipt.input_digest != (plan.source_digest if index == 0 else receipts[index - 1].output_digest)
        ):
            msg = "receipt-chain"
            raise PrivacyRefused(msg)
    first, terminal = receipts
    if (
        first.operation != "validate-closed-grammar"
        or first.output_digest != plan.source_digest
        or first.operations
        or terminal.operation != "replace-sensitive-spans"
        or terminal.output_digest != bytes_digest(candidate)
        or terminal.operations != operation_counts(projection)
    ):
        msg = "receipt-transformation"
        raise PrivacyRefused(msg)
