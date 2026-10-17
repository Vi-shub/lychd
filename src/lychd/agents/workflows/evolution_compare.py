"""Deterministic Bridge revision 5: compare admitted claims and retain the result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_graph import BaseNode, End, GraphRunContext

from lychd.agents.outputs import BridgeReply
from lychd.agents.services import WorkflowServices
from lychd.agents.workflows.base import PatternEdge, PatternManifest, PatternNode, Trigger, Workflow
from lychd.domain.cortex.graph import build_serial_graph
from lychd.domain.cortex.graph_runner import HardwareResumeBudget
from lychd.domain.cortex.ledger import RunAdmissionConflictError
from lychd.domain.cortex.priority import PRIORITY_DEFAULT
from lychd.domain.evolution import MAX_OUTPUT_BYTES, ComparisonPacket, compare_history, decode_packet
from lychd.domain.web.schemas import BridgeTurn

if TYPE_CHECKING:
    from lychd.agents.router import Intent

MAX_COMPARISON_PROMPT_CHARS = 100_000
COMPARISON_COMMAND = "/compare-history\n"
COMPARISON_SCOPE = (
    "LychD Evolution comparison — comparison-only. Inputs are caller-admitted claims. "
    "This calculation does not authenticate receipts, prove tests or evaluator independence, "
    "verify live state, or authorize adoption or activation."
)


class EvolutionComparisonError(RunAdmissionConflictError):
    """Payload-free refusal of a comparison command or checkpoint."""


def parse_comparison_prompt(prompt: str) -> ComparisonPacket:
    """Parse the exact admitted prompt without an additional normalization pass."""
    msg = "The comparison profile requires /compare-history followed by a newline and one valid bounded packet."
    if len(prompt) > MAX_COMPARISON_PROMPT_CHARS or not prompt.startswith(COMPARISON_COMMAND):
        raise EvolutionComparisonError(msg)
    try:
        return decode_packet(prompt[len(COMPARISON_COMMAND) :].encode("utf-8"))
    except (ValueError, RecursionError):
        raise EvolutionComparisonError(msg) from None


def comparison_reply(prompt: str) -> BridgeReply:
    """Derive one labelled, bounded calculation; no claim becomes evidence truth."""
    packet = parse_comparison_prompt(prompt)
    report = compare_history(packet.admission, packet.proposal).model_dump_json()
    if len(report.encode("utf-8")) > MAX_OUTPUT_BYTES:
        msg = "The comparison report exceeds its output bound."
        raise EvolutionComparisonError(msg)
    return BridgeReply(answer=f"{COMPARISON_SCOPE}\n\n{report}")


class EvolutionCompareState(BaseModel):
    """Only pinned identities and the prompt persist; the result is always derived."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")
    session_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=MAX_COMPARISON_PROMPT_CHARS)
    priority: int = Field(ge=0, le=100)
    hardware_resume_budget: HardwareResumeBudget = Field(default_factory=HardwareResumeBudget)

    @field_validator("hardware_resume_budget", mode="before")
    @classmethod
    def require_zero_hardware_budget(cls, value: object) -> HardwareResumeBudget:
        budget = HardwareResumeBudget.model_validate(
            value.model_dump() if isinstance(value, HardwareResumeBudget) else value, strict=True
        )
        if budget != HardwareResumeBudget():
            msg = "Comparison has no hardware-resume allowance."
            raise EvolutionComparisonError(msg)
        return budget


def _make_state(intent: Intent) -> EvolutionCompareState:
    parse_comparison_prompt(intent.prompt)
    if intent.run_id is None or any(
        binding is not None
        for binding in (
            intent.admitted_capability_key,
            intent.admitted_archive,
            intent.admitted_scout,
            intent.admitted_delegation,
        )
    ):
        msg = "Comparison requires its admitted Run and no model-bearing bindings."
        raise EvolutionComparisonError(msg)
    return EvolutionCompareState(
        session_id=intent.session_id,
        run_id=intent.run_id,
        prompt=intent.prompt,
        priority=intent.priority if intent.priority is not None else PRIORITY_DEFAULT,
    )


def _validate_state(intent: Intent, state: BaseModel) -> None:
    msg = "Comparison checkpoint does not match the admitted Run."
    if not isinstance(state, EvolutionCompareState):
        raise EvolutionComparisonError(msg)
    try:
        validated = EvolutionCompareState.model_validate_json(state.model_dump_json())
        expected = _make_state(intent)
    except (ValueError, RecursionError):
        raise EvolutionComparisonError(msg) from None
    if validated != expected:
        raise EvolutionComparisonError(msg)


@dataclass
class CompareAndSettle(BaseNode[EvolutionCompareState, WorkflowServices, BridgeReply]):
    """Calculate and settle ordinary Bridge history without model history or external effects."""

    async def run(self, ctx: GraphRunContext[EvolutionCompareState, WorkflowServices]) -> End[BridgeReply]:
        reply = comparison_reply(ctx.state.prompt)
        await ctx.deps.turns.settle_agent_turn(
            ctx.state.session_id,
            BridgeTurn(role="agent", content=reply.answer, run_id=ctx.state.run_id, state="settled"),
            new_messages=[],
        )
        return End(reply)


EVOLUTION_COMPARE_GRAPH = build_serial_graph(
    nodes=(CompareAndSettle,),
    state_type=EvolutionCompareState,
    deps_type=WorkflowServices,
    output_type=BridgeReply,
    name="bridge_chat",
)

BRIDGE_CHAT_EVOLUTION = Workflow(
    name="bridge_chat",
    title="Bridge Evolution comparison",
    description="Compare declared historical obligations without inference or adoption authority.",
    trigger=Trigger(hint="configured mechanical comparison", match=lambda intent: intent.source == "bridge"),
    graph=EVOLUTION_COMPARE_GRAPH,
    start_node=CompareAndSettle,
    make_state=_make_state,
    validate_state=_validate_state,
    manifest=PatternManifest(
        key="bridge_chat",
        revision="5",
        implementation_revision="py.5",
        checkpoint_schema="bridge-evolution-comparison-state-v1",
        entry_node="compare_and_settle",
        nodes=(
            PatternNode(
                key="compare_and_settle", label="Compare admitted claims and settle", implementation=CompareAndSettle
            ),
            PatternNode(key="end", label="End", kind="terminal"),
        ),
        edges=(PatternEdge(key="compare-to-end", source="compare_and_settle", target="end"),),
    ),
)
