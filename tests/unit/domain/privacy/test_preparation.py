"""Proof and attacks on the closed diagnostic profile; no network or private data."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lychd.domain.privacy.censor import censor
from lychd.domain.privacy.contracts import (
    LANES,
    MAX_CANDIDATE_BYTES,
    CutBinding,
    DisclosurePlan,
    PreparationPolicy,
    PreparedCut,
    PrivacyRefused,
    SensitiveSpan,
    TextMaterial,
    UtilitySpan,
    WholeProjection,
    bytes_digest,
    canonical_bytes,
    digest,
)
from lychd.domain.privacy.fixtures import diagnostic_fixture, fixture_binding
from lychd.domain.privacy.labels import (
    HandlingConstraint,
    PrivacyCategory,
    PrivacyClass,
    PrivatizationLabel,
    join_labels,
)
from lychd.domain.privacy.preparation import CutAuthority, make_plan, prepare_cut, public_event
from lychd.domain.privacy.verification import verify_candidate

NOW = 1_800_000_000


@pytest.fixture
def prepared() -> tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut]:
    source = diagnostic_fixture()
    policy = PreparationPolicy()
    plan = make_plan(source, fixture_binding(), policy, expires_at=NOW + 300)
    authority = CutAuthority()
    return source, plan, policy, authority, prepare_cut(source, plan, policy, authority, now=NOW)


def test_complete_projection_is_irreversible_and_utility_preserving(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut],
) -> None:
    source, plan, policy, authority, result = prepared
    original = canonical_bytes(source)
    payload = json.loads(result.candidate)
    assert set(payload) == {"profile", *LANES}
    assert b"SYNTHETIC-PERSON-017" not in result.candidate
    for lane in LANES:
        assert payload[lane] == [
            "Build node-a depends on node-b failed; owner: [redacted]; diagnostic: E001; citation: [1]"
        ]
    assert result.verification.privacy_verdict == "declared-spans-absent"
    assert result.verification.utility_verdict == "exact-closed-grammar-preserved"
    assert source.influence().classification is PrivacyClass.PRIVATE
    assert canonical_bytes(source) == original
    assert result.cut.plan.influence_label_digest == digest(source.influence())
    assert result.receipts[1].parent_receipt_digests == (digest(result.receipts[0]),)
    assert result.cut.terminal_receipt_digest == digest(result.receipts[-1])
    authority.verify_seal(result.cut, result.candidate, plan, plan.binding, policy, now=NOW + 1)
    assert censor(source, plan, policy, now=NOW) == (result.candidate, result.receipts)


def test_evidence_contains_no_raw_source_or_source_identity(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut],
) -> None:
    source, _, _, _, result = prepared
    evidence = canonical_bytes((result.cut, result.receipts, result.verification))
    assert b"SYNTHETIC-PERSON-017" not in evidence
    for material in source.materials():
        assert str(material.source_id).encode() not in evidence
    assert b"reversal" not in evidence
    assert b"SYNTHETIC-PERSON-017" not in repr(source).encode()
    first, second = public_event(), public_event()
    assert set(first.model_dump()) == {"event_id"}
    assert first.event_id != second.event_id


@pytest.mark.parametrize("lane", LANES)
def test_unknown_influence_in_any_lane_refuses(lane: str) -> None:
    source = diagnostic_fixture()
    material = getattr(source, lane)[0].model_copy(update={"label": PrivatizationLabel()})
    source = source.model_copy(update={lane: (material,)})
    with pytest.raises(PrivacyRefused, match="unknown-lineage"):
        make_plan(source, fixture_binding(), PreparationPolicy(), expires_at=NOW + 300)


def test_join_preserves_unknown_and_all_influence() -> None:
    parent, subject = uuid4(), uuid4()
    private = PrivatizationLabel(
        classification=PrivacyClass.PRIVATE,
        weight=0.7,
        categories=frozenset({PrivacyCategory.DIRECT_IDENTIFIER}),
        material_parents=frozenset({parent}),
        subjects=frozenset({subject}),
        constraints=frozenset({HandlingConstraint.NO_TRAINING}),
        lineage_known=True,
    )
    public = PrivatizationLabel(classification=PrivacyClass.PUBLIC, weight=0.0, lineage_known=True)
    assert join_labels((public, private)) == private
    unknown = join_labels((private, PrivatizationLabel()))
    assert not unknown.lineage_known
    assert unknown.classification is PrivacyClass.RESTRICTED
    assert unknown.weight == 1.0
    assert unknown.subjects == {subject}
    assert unknown.material_parents == {parent}
    assert unknown.categories == private.categories
    assert unknown.constraints == private.constraints
    assert join_labels(()) == PrivatizationLabel()


@pytest.mark.parametrize(
    "field", ["consumer_id", "principal_id", "run_id", "occurrence_id", "attempt_id", "purpose_id"]
)
def test_identity_mixing_refuses(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut], field: str
) -> None:
    _, plan, policy, authority, result = prepared
    binding = plan.binding.model_copy(update={field: uuid4()})
    with pytest.raises(PrivacyRefused, match="cut-binding"):
        authority.verify_seal(result.cut, result.candidate, plan, binding, policy, now=NOW)


@pytest.mark.parametrize("field", ["target_digest", "sigil_digest"])
def test_target_and_sigil_substitution_refuse(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut], field: str
) -> None:
    _, plan, policy, authority, result = prepared
    binding = plan.binding.model_copy(update={field: "f" * 64})
    with pytest.raises(PrivacyRefused, match="cut-binding"):
        authority.verify_seal(result.cut, result.candidate, plan, binding, policy, now=NOW)


@pytest.mark.parametrize("when", [NOW - 1, NOW + 300, NOW + 301])
def test_expired_or_future_cut_refuses(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut], when: int
) -> None:
    _, plan, policy, authority, result = prepared
    with pytest.raises(PrivacyRefused, match="expiry"):
        authority.verify_seal(result.cut, result.candidate, plan, plan.binding, policy, now=when)


def test_different_process_authority_cannot_accept_seal(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut],
) -> None:
    _, plan, policy, _, result = prepared
    with pytest.raises(PrivacyRefused, match="cut-seal"):
        CutAuthority().verify_seal(result.cut, result.candidate, plan, plan.binding, policy, now=NOW)


@pytest.mark.parametrize("field", ["candidate_digest", "receipt_chain_digest", "terminal_receipt_digest", "seal"])
def test_cut_tampering_refuses(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut], field: str
) -> None:
    _, plan, policy, authority, result = prepared
    cut = result.cut.model_copy(update={field: "f" * 64})
    with pytest.raises(PrivacyRefused, match="cut-seal"):
        authority.verify_seal(cut, result.candidate, plan, plan.binding, policy, now=NOW)


def test_changed_policy_and_wire_refuse(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut],
) -> None:
    _, plan, policy, authority, result = prepared
    with pytest.raises(PrivacyRefused, match="cut-binding"):
        authority.verify_seal(result.cut, result.candidate + b" ", plan, plan.binding, policy, now=NOW)
    with pytest.raises(PrivacyRefused, match="cut-binding"):
        authority.verify_seal(
            result.cut,
            result.candidate,
            plan,
            plan.binding,
            policy.model_copy(update={"max_lifetime_seconds": 400}),
            now=NOW,
        )


def test_modified_source_cannot_reuse_plan(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut],
) -> None:
    source, plan, policy, authority, _ = prepared
    query = source.query[0].model_copy(update={"text": source.query[0].text.replace("PERSON-017", "PERSON-018")})
    changed = source.model_copy(update={"query": (query,)})
    with pytest.raises(PrivacyRefused, match="plan-binding"):
        prepare_cut(changed, plan, policy, authority, now=NOW)


@pytest.mark.parametrize("attack", ["reorder", "truncate", "append", "parent", "counts", "output"])
def test_receipt_chain_attacks_refuse(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut], attack: str
) -> None:
    source, plan, policy, _, result = prepared
    first, terminal = result.receipts
    attacks = {
        "reorder": (terminal, first),
        "truncate": (terminal,),
        "append": (*result.receipts, terminal),
        "parent": (first, terminal.model_copy(update={"parent_receipt_digests": ()})),
        "counts": (first, terminal.model_copy(update={"operations": ()})),
        "output": (first, terminal.model_copy(update={"output_digest": "f" * 64})),
    }
    with pytest.raises(PrivacyRefused, match="receipt"):
        verify_candidate(source, plan, result.candidate, attacks[attack], policy, now=NOW)


@pytest.mark.parametrize("replacement", [b"node-c", b"[redacted]"])
def test_forged_matching_receipts_do_not_hide_utility_damage(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut], replacement: bytes
) -> None:
    source, plan, policy, _, result = prepared
    candidate = result.candidate.replace(b"node-b", replacement)
    receipts = (result.receipts[0], result.receipts[1].model_copy(update={"output_digest": bytes_digest(candidate)}))
    with pytest.raises(PrivacyRefused, match="utility-or-transformation"):
        verify_candidate(source, plan, candidate, receipts, policy, now=NOW)


def test_verifier_disagreement_cannot_be_sealed(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut],
) -> None:
    source, plan, policy, authority, result = prepared
    forged = result.verification.model_copy(update={"candidate_digest": "f" * 64})
    with pytest.raises(PrivacyRefused, match="verifier-disagreement"):
        authority.seal(source, plan, result.candidate, result.receipts, forged, policy, now=NOW)


@pytest.mark.parametrize("text", ["Secret Holder", "; "])
def test_all_redacted_and_punctuation_only_are_not_useful(text: str) -> None:
    if text == "; ":
        material = TextMaterial(
            source_id=uuid4(),
            text=text,
            label=PrivatizationLabel(classification=PrivacyClass.PUBLIC, weight=0.0, lineage_known=True),
            utility_spans=(UtilitySpan(start=0, end=2, token="; "),),  # noqa: S106 - grammar punctuation
        )
    else:
        material = TextMaterial(
            source_id=uuid4(),
            text=text,
            label=PrivatizationLabel(categories=frozenset({PrivacyCategory.DIRECT_IDENTIFIER}), lineage_known=True),
            sensitive_spans=(SensitiveSpan(start=0, end=len(text), category=PrivacyCategory.DIRECT_IDENTIFIER),),
        )
    with pytest.raises(PrivacyRefused, match="required-diagnostic-utility"):
        make_plan(WholeProjection(query=(material,)), fixture_binding(), PreparationPolicy(), expires_at=NOW + 300)


@pytest.mark.parametrize("attack", ["gap", "overlap", "outside", "undeclared", "unknown-token", "duplicate-source"])
def test_invalid_source_partition_refuses(attack: str) -> None:
    source = diagnostic_fixture()
    material = source.query[0]
    span = material.sensitive_spans[0]
    if attack == "duplicate-source":
        source = source.model_copy(update={"query": source.blocks})
    else:
        updates = {
            "gap": {"sensitive_spans": (span.model_copy(update={"start": span.start + 1}),)},
            "overlap": {"sensitive_spans": (span.model_copy(update={"start": span.start - 1}),)},
            "outside": {"sensitive_spans": (span.model_copy(update={"end": len(material.text) + 1}),)},
            "undeclared": {"text": material.text + "unknown private prose"},
            "unknown-token": {"text": material.text.replace("Build ", "Other ")},
        }
        source = source.model_copy(update={"query": (material.model_copy(update=updates[attack]),)})
    with pytest.raises(PrivacyRefused):
        make_plan(source, fixture_binding(), PreparationPolicy(), expires_at=NOW + 300)


@pytest.mark.parametrize("attack", ["secret", "local-only", "missing-category"])
def test_prohibited_and_missing_handling_facts_refuse(attack: str) -> None:
    source = diagnostic_fixture()
    material = source.query[0]
    updates = {
        "secret": {"categories": frozenset({PrivacyCategory.SECRET})},
        "local-only": {"constraints": frozenset({HandlingConstraint.LOCAL_ONLY})},
        "missing-category": {"categories": frozenset[PrivacyCategory]()},
    }
    material = material.model_copy(update={"label": material.label.model_copy(update=updates[attack])})
    with pytest.raises(PrivacyRefused):
        make_plan(
            source.model_copy(update={"query": (material,)}),
            fixture_binding(),
            PreparationPolicy(),
            expires_at=NOW + 300,
        )


def test_expiry_and_disclosure_budget_are_checked_before_preparation() -> None:
    source, policy, authority = diagnostic_fixture(), PreparationPolicy(), CutAuthority()
    for expires_at, uses in ((NOW, 1), (NOW + 601, 1), (NOW + 300, 2)):
        plan = make_plan(source, fixture_binding(), policy, expires_at=expires_at, max_disclosures=uses)
        with pytest.raises(PrivacyRefused):
            prepare_cut(source, plan, policy, authority, now=NOW)


@pytest.mark.parametrize("attack", ["metadata", "history", "duplicate-key", "huge", "deep"])
def test_candidate_schema_and_bounds_refuse(
    prepared: tuple[WholeProjection, DisclosurePlan, PreparationPolicy, CutAuthority, PreparedCut], attack: str
) -> None:
    source, plan, policy, _, result = prepared
    payload = json.loads(result.candidate)
    attacks = {
        "metadata": canonical_bytes({**payload, "cache_id": "old-raw-cache"}),
        "history": canonical_bytes({**payload, "history": ["SYNTHETIC-PERSON-017"]}),
        "duplicate-key": b'{"profile":"junk",' + result.candidate[1:],
        "huge": b" " * (MAX_CANDIDATE_BYTES + 1),
        "deep": b"[" * 1000 + b"0" + b"]" * 1000,
    }
    with pytest.raises(PrivacyRefused):
        verify_candidate(source, plan, attacks[attack], result.receipts, policy, now=NOW)


def test_private_value_reappearing_as_public_token_fails_closed() -> None:
    source = diagnostic_fixture()
    material = source.query[0]
    old = material.sensitive_spans[0]
    replacement = "node-a"
    removed = old.end - old.start - len(replacement)
    material = material.model_copy(
        update={
            "text": material.text[: old.start] + replacement + material.text[old.end :],
            "sensitive_spans": (old.model_copy(update={"end": old.start + len(replacement)}),),
            "utility_spans": tuple(
                span
                if span.start < old.start
                else span.model_copy(update={"start": span.start - removed, "end": span.end - removed})
                for span in material.utility_spans
            ),
        }
    )
    source = source.model_copy(update={"query": (material,)})
    policy = PreparationPolicy()
    plan = make_plan(source, fixture_binding(), policy, expires_at=NOW + 300)
    with pytest.raises(PrivacyRefused, match="residual-sensitive-value"):
        prepare_cut(source, plan, policy, CutAuthority(), now=NOW)


def test_closed_models_refuse_free_metadata_and_validation_omits_source() -> None:
    source = diagnostic_fixture()
    payload = source.model_dump()
    payload["filename"] = "private-name.txt"
    with pytest.raises(ValidationError) as caught:
        WholeProjection.model_validate(payload)
    assert "private-name.txt" not in str(caught.value)
    binding = fixture_binding()
    malformed = CutBinding.model_construct(_fields_set=None, **{**binding.model_dump(), "principal_id": "not-an-id"})
    with pytest.raises(PrivacyRefused, match="invalid-contract"):
        make_plan(source, malformed, PreparationPolicy(), expires_at=NOW + 300)


@pytest.mark.parametrize("token_index", [0, 1, 2, 3, 4, 5, 8, 9, 11, 12])
def test_sensitive_modifier_cannot_interrupt_required_utility(token_index: int) -> None:
    source = diagnostic_fixture()
    material = source.query[0]
    owner = material.sensitive_spans[0]
    insertion = material.utility_spans[token_index].start
    modifier = " never"
    spans = tuple(
        span.model_copy(update={"start": span.start + len(modifier), "end": span.end + len(modifier)})
        if span.start >= insertion
        else span
        for span in material.utility_spans
    )
    shifted_owner = (
        owner.model_copy(update={"start": owner.start + len(modifier), "end": owner.end + len(modifier)})
        if owner.start >= insertion
        else owner
    )
    changed = material.model_copy(
        update={
            "text": material.text[:insertion] + modifier + material.text[insertion:],
            "utility_spans": spans,
            "sensitive_spans": (
                SensitiveSpan(
                    start=insertion, end=insertion + len(modifier), category=PrivacyCategory.DIRECT_IDENTIFIER
                ),
                shifted_owner,
            ),
        }
    )
    with pytest.raises(PrivacyRefused, match="required-diagnostic-utility"):
        make_plan(
            source.model_copy(update={"query": (changed,)}),
            fixture_binding(),
            PreparationPolicy(),
            expires_at=NOW + 300,
        )


def test_single_sensitive_span_moved_from_owner_to_relation_refuses() -> None:
    source = diagnostic_fixture()
    material = source.query[0]
    old = material.sensitive_spans[0]
    insertion = len("Build node-a")
    text_without_owner = material.text[: old.start] + material.text[old.end :]
    modifier = " never"
    text = text_without_owner[:insertion] + modifier + text_without_owner[insertion:]
    relocated: list[UtilitySpan] = []
    for span in material.utility_spans:
        start, end = span.start, span.end
        if start >= old.end:
            start -= old.end - old.start
            end -= old.end - old.start
        if start >= insertion:
            start += len(modifier)
            end += len(modifier)
        relocated.append(span.model_copy(update={"start": start, "end": end}))
    changed = material.model_copy(
        update={
            "text": text,
            "utility_spans": tuple(relocated),
            "sensitive_spans": (
                SensitiveSpan(
                    start=insertion, end=insertion + len(modifier), category=PrivacyCategory.DIRECT_IDENTIFIER
                ),
            ),
        }
    )
    with pytest.raises(PrivacyRefused, match="diagnostic-physical-grammar"):
        make_plan(
            source.model_copy(update={"query": (changed,)}),
            fixture_binding(),
            PreparationPolicy(),
            expires_at=NOW + 300,
        )


def test_source_label_metadata_flood_fails_safely() -> None:
    source = diagnostic_fixture()
    material = source.query[0]
    huge = material.label.model_copy(update={"subjects": frozenset(uuid4() for _ in range(5000))})
    material = material.model_copy(update={"label": huge})
    with pytest.raises(PrivacyRefused, match="invalid-contract"):
        make_plan(
            source.model_copy(update={"query": (material,)}),
            fixture_binding(),
            PreparationPolicy(),
            expires_at=NOW + 300,
        )


def test_label_join_overflow_refuses_without_dropping_ancestry() -> None:
    first = PrivatizationLabel(subjects=frozenset(uuid4() for _ in range(70)), lineage_known=True)
    second = PrivatizationLabel(material_parents=frozenset(uuid4() for _ in range(70)), lineage_known=True)
    with pytest.raises(PrivacyRefused, match="label-reference-bound"):
        join_labels((first, second))
    assert len(first.subjects) == len(second.material_parents) == 70


def test_projection_reference_budget_counts_repeated_metadata() -> None:
    source = diagnostic_fixture()
    shared = frozenset(uuid4() for _ in range(50))
    fields = {
        lane: (
            getattr(source, lane)[0].model_copy(
                update={"label": getattr(source, lane)[0].label.model_copy(update={"subjects": shared})}
            ),
        )
        for lane in LANES
    }
    source = source.model_copy(update=fields)
    with pytest.raises(PrivacyRefused, match="projection-reference-bound"):
        make_plan(source, fixture_binding(), PreparationPolicy(), expires_at=NOW + 300)
