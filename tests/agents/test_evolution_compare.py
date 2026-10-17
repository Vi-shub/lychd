"""Deterministic comparison casting preserves declaration and checkpoint boundaries."""

from __future__ import annotations

import json
from typing import Literal

import pytest
from pydantic import ValidationError

from lychd.agents.router import Intent
from lychd.agents.workflows import BRIDGE_CHAT, BRIDGE_CHAT_EVOLUTION, builtin_workflow_registry
from lychd.agents.workflows import evolution_compare as module
from lychd.agents.workflows.evolution_compare import (
    COMPARISON_COMMAND,
    COMPARISON_SCOPE,
    EvolutionCompareState,
    EvolutionComparisonError,
    comparison_reply,
    parse_comparison_prompt,
)
from lychd.config.settings import BridgeCastingSettings, WeaverSettings
from lychd.domain.artifacts import ArtifactRef
from lychd.domain.evolution import (
    ComparisonAdmission,
    ComparisonPacket,
    ComparisonProposal,
    DispositionProposal,
    EvidenceSummary,
    HistoricalObligation,
    admission_digest,
    compare_history,
)
from tests.scout_fixtures import scout_profile


def comparison_prompt(*, result: Literal["pass", "fail", "unknown"] = "pass", reject: bool = False) -> str:
    """One explicit declared obligation; no test execution is claimed by its receipt."""

    def ref(name: str) -> ArtifactRef:
        return ArtifactRef(artifact_id=name, digest="sha256:" + "1" * 64, media_type="application/json", size=1)

    contract = ref("inherited-contract")
    admission = ComparisonAdmission(
        base=ref("installed"),
        candidate=ref("candidate"),
        policy=ref("policy"),
        history=ref("history"),
        obligations=(HistoricalObligation(obligation_id="boundary", owner="owner", inherited_contract=contract),),
    )
    summary = EvidenceSummary(
        obligation_id="boundary",
        admission_digest=admission_digest(admission),
        base=admission.base,
        candidate=admission.candidate,
        policy=admission.policy,
        selected_contract=contract,
        purpose="candidate_contract",
        kind="verification_receipt",
        producer="declared-verifier",
        reported_result=result,
        receipt=ref("attributed-receipt"),
    )
    proposal = ComparisonProposal(
        admission_digest=admission_digest(admission),
        dispositions=(
            DispositionProposal(
                obligation_id="boundary",
                owner="owner",
                disposition="reject" if reject else "retain",
                reason="Keep the declared boundary, or record its rejection.",
                selected_contract=contract,
                evidence=() if reject else (summary,),
            ),
        ),
    )
    return (
        COMPARISON_COMMAND
        + ComparisonPacket(
            profile="evolution-comparison@1",
            admission=admission,
            proposal=proposal,
        ).model_dump_json()
    )


def test_revision_five_is_opt_in_and_refuses_model_bearing_bindings() -> None:
    assert builtin_workflow_registry().default is BRIDGE_CHAT
    settings = WeaverSettings(bridge=BridgeCastingSettings(revision="5"))
    registry = builtin_workflow_registry(weaver=settings)
    assert registry.default is BRIDGE_CHAT_EVOLUTION
    assert all(registry.get_revision("bridge_chat", revision) is not None for revision in ("1", "2", "3", "4"))
    assert not registry.is_active("bridge_chat", "4")
    for binding in ({"capability_key": "local:chat:model"}, {"scout": scout_profile()}):
        with pytest.raises(ValidationError):
            BridgeCastingSettings.model_validate({"revision": "5", **binding})
    for revision in ("2", "3", "4"):
        with pytest.raises(ValidationError):
            BridgeCastingSettings.model_validate({"revision": revision})


@pytest.mark.parametrize("suffix", ["", "{}", '{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}'])
def test_parser_refuses_bad_packets_without_payload_in_errors(suffix: str) -> None:
    with pytest.raises(EvolutionComparisonError, match="valid bounded packet") as raised:
        parse_comparison_prompt(COMPARISON_COMMAND + suffix)
    assert suffix not in str(raised.value) if suffix else True


@pytest.mark.parametrize("prompt", ["hello", "/compare-history {}", "/compare-history\r\n{}", "x" * 100_001])
def test_parser_requires_exact_admitted_command_and_character_limit(prompt: str) -> None:
    with pytest.raises(EvolutionComparisonError):
        parse_comparison_prompt(prompt)


@pytest.mark.parametrize(
    ("result", "reject", "outcome"),
    [("pass", False, "comparison_complete"), ("unknown", False, "blocked"), ("pass", True, "rejection_recorded")],
)
def test_reply_is_exact_native_calculation_with_fixed_authority_scope(
    result: Literal["pass", "fail", "unknown"], *, reject: bool, outcome: str
) -> None:
    prompt = comparison_prompt(result=result, reject=reject)
    packet = parse_comparison_prompt(prompt)
    answer = comparison_reply(prompt).answer
    scope, report = answer.split("\n\n", 1)
    assert scope == COMPARISON_SCOPE
    assert report == compare_history(packet.admission, packet.proposal).model_dump_json()
    assert json.loads(report)["outcome"] == outcome


@pytest.mark.parametrize(
    ("field", "value"), [("run_id", "other"), ("session_id", "other"), ("prompt", "hello"), ("priority", 1)]
)
def test_checkpoint_cannot_substitute_any_admitted_binding(field: str, value: object) -> None:
    intent = Intent(session_id="session", run_id="canonical-run", prompt=comparison_prompt(), priority=70)
    state = BRIDGE_CHAT_EVOLUTION.make_state(intent)
    assert BRIDGE_CHAT_EVOLUTION.validate_state is not None
    BRIDGE_CHAT_EVOLUTION.validate_state(intent, state)
    assert type(state).model_validate_json(state.model_dump_json()) == state
    with pytest.raises(EvolutionComparisonError, match="admitted Run"):
        BRIDGE_CHAT_EVOLUTION.validate_state(intent, state.model_copy(update={field: value}))


def test_checkpoint_budget_and_result_substitution_refuse() -> None:
    state = BRIDGE_CHAT_EVOLUTION.make_state(Intent(session_id="s", run_id="r", prompt=comparison_prompt()))
    for change in (
        {"hardware_resume_budget": {"total_resumes": False}},
        {
            "hardware_resume_budget": {
                "total_resumes": 1,
                "same_capability_key": "forbidden",
                "same_capability_resumes": 1,
            }
        },
        {"report": "substitute"},
    ):
        with pytest.raises(ValidationError):
            EvolutionCompareState.model_validate_json(json.dumps({**state.model_dump(mode="json"), **change}))


def test_utility_output_bound_remains_effective(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 1)
    with pytest.raises(EvolutionComparisonError, match="output bound"):
        comparison_reply(comparison_prompt())
