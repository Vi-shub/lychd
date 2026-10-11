"""Native builder migration keeps exact typed v1 cursors without replaying uncertain work."""

# Exercise the production worker's initial-resume validator with captured Run truth.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_serializer
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from lychd.agents.workflows import BRIDGE_CHAT, DELEGATED_RITE, builtin_workflow_registry, resolve_pinned_workflow
from lychd.agents.workflows.bridge_chat import BridgeChatState
from lychd.domain.cortex.graph import build_serial_graph
from lychd.domain.cortex.graph_runner import GraphRunner, HardwareResumeBudget
from lychd.domain.cortex.runs import RunRecord, RunStatus
from lychd.domain.cortex.stasis import DurableStasisPhylactery, EndSnapshot, InMemoryStasisStore, NodeSnapshot
from lychd.ghouls.runs import _validate_resume_owner
from tests.agents.conftest import make_services
from tests.agents.fakes import FakeConsents, FakeEvents, FakeOrchestrator, FakeTurns, approval_test_toolset

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "graph" / "v1"


class _NoHardwareOrchestrator(FakeOrchestrator):
    async def handle_transition(self, *_args: Any, **_kwargs: Any) -> None:
        pytest.fail("a captured consent continuation must not request hardware")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("name", ["consent-created", "consent-pending", "delegate-created"])
@pytest.mark.asyncio
async def test_v1_checkpoint_decodes_against_its_exact_retained_pattern(name: str) -> None:
    captured = _fixture(name)
    saved_run = captured["run"]
    workflow = resolve_pinned_workflow(
        builtin_workflow_registry(), workflow_name=saved_run["workflow_name"], snapshot=saved_run["pattern_manifest"]
    )
    assert workflow in (BRIDGE_CHAT, DELEGATED_RITE)
    assert workflow is not None
    store = InMemoryStasisStore()
    await store.replace(saved_run["run_id"], captured["snapshots"])
    persistence = DurableStasisPhylactery(job_id=saved_run["run_id"], store=store)
    persistence.set_graph_types(workflow.graph)

    decoded = await persistence.load_all()
    assert [item.id for item in decoded] == [item["id"] for item in captured["snapshots"]]
    last = decoded[-1]
    assert isinstance(last, NodeSnapshot)
    assert last.status == ("pending" if name == "consent-pending" else "created")
    assert last.state.model_dump(mode="json") == captured["snapshots"][-1]["state"]
    assert last.node.get_node_id() == captured["snapshots"][-1]["node"]["node_id"]
    if name == "consent-pending":
        assert await persistence.load_next() is None
    else:
        resumed = await persistence.load_next()
        assert resumed is not None
        assert resumed.id == last.id
        assert resumed.status == "pending"


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.asyncio
async def test_v1_consent_checkpoint_resumes_native_builder_with_its_exact_verdict(*, approved: bool) -> None:
    captured = _fixture("consent-created")
    saved_run = captured["run"]
    run = RunRecord(
        run_id=saved_run["run_id"],
        session_id=saved_run["session_id"],
        workflow_name=saved_run["workflow_name"],
        pattern_manifest=saved_run["pattern_manifest"],
        source=saved_run["source"],
        queue_name=saved_run["queue_name"],
        priority=saved_run["priority"],
        status=RunStatus.RUNNING,
        prompt=saved_run["prompt"],
        consent_id=saved_run["consent_id"],
    )
    store = InMemoryStasisStore()
    await store.replace(run.run_id, captured["snapshots"])
    persistence = DurableStasisPhylactery(job_id=run.run_id, store=store)
    events, turns, orchestrator = FakeEvents(), FakeTurns(), _NoHardwareOrchestrator()

    async def settle(_messages: Any, info: Any) -> AsyncIterator[dict[int, DeltaToolCall]]:
        yield {
            0: DeltaToolCall(
                name=info.output_tools[0].name, json_args='{"answer":"done","fragments":[]}', tool_call_id="reply"
            )
        }

    services = make_services(
        model=FunctionModel(stream_function=settle),
        events=events,
        turns=turns,
        consents=FakeConsents(verdicts={saved_run["consent_id"]: approved}),
        orchestrator=orchestrator,
        toolsets=(approval_test_toolset(),),
    )
    assert BRIDGE_CHAT.validate_state is not None
    runner = GraphRunner[BridgeChatState](
        orchestrator=orchestrator,
        persistence=persistence,
        signal_priority=run.priority,
        validate_state=partial(BRIDGE_CHAT.validate_state, run.to_intent()),
        validate_resume=partial(_validate_resume_owner, run),
    )
    output = await runner.resume_graph(BRIDGE_CHAT.graph, deps=services)
    assert output.answer == "done"
    assert len(orchestrator.calls) == int(approved)
    assert await store.exists(run.run_id)  # caller still owns terminal settlement and deletion


class _BudgetState(BaseModel):
    data: str
    warm: bool
    hardware_resume_budget: HardwareResumeBudget = Field(default_factory=HardwareResumeBudget)


@dataclass
class StasisAcrossDurableParkNode(BaseNode[_BudgetState, None, None]):
    async def run(self, ctx: GraphRunContext[_BudgetState, None]) -> End[None]:
        _ = ctx
        pytest.fail("decoding a hardware checkpoint must not execute its station")


@pytest.mark.asyncio
async def test_v1_hardware_resume_budget_survives_codec_replacement() -> None:
    graph = build_serial_graph(
        nodes=[StasisAcrossDurableParkNode], state_type=_BudgetState, deps_type=type(None), output_type=type(None)
    )
    store = InMemoryStasisStore()
    await store.replace("hardware", _fixture("hardware-budget")["snapshots"])
    persistence = DurableStasisPhylactery(job_id="hardware", store=store)
    persistence.set_graph_types(graph)
    restored = await persistence.load_next()
    assert restored is not None
    assert restored.state.hardware_resume_budget.total_resumes == 1
    assert restored.state.hardware_resume_budget.same_capability_resumes == 1
    assert restored.state.hardware_resume_budget.same_capability_key == "mock-anim:chat:mock-cap"


@pytest.mark.parametrize(
    "corruption", ["unknown-node", "unknown-node-field", "duplicate-id", "multiple-created", "invalid-state"]
)
@pytest.mark.asyncio
async def test_checkpoint_codec_refuses_unbound_or_ambiguous_history(corruption: str) -> None:
    snapshots = _fixture("consent-created")["snapshots"]
    if corruption == "unknown-node":
        snapshots[-1]["node"]["node_id"] = "ForeignEffect"
    elif corruption == "unknown-node-field":
        snapshots[-1]["node"]["effect"] = "injected"
    elif corruption == "duplicate-id":
        snapshots[-1]["id"] = snapshots[0]["id"]
    elif corruption == "multiple-created":
        duplicate = copy.deepcopy(snapshots[-1])
        duplicate["id"] += "-extra"
        snapshots.append(duplicate)
    else:
        snapshots[-1]["state"]["priority"] = "not-a-priority"
    store = InMemoryStasisStore()
    await store.replace("hostile", snapshots)
    persistence = DurableStasisPhylactery(job_id="hostile", store=store)
    persistence.set_graph_types(BRIDGE_CHAT.graph)
    message = {
        "unknown-node": "not in its admitted graph",
        "unknown-node-field": "unknown fields",
        "duplicate-id": "snapshot ids must be unique",
        "multiple-created": "multiple resumable stations",
        "invalid-state": "priority",
    }[corruption]
    with pytest.raises(ValueError, match=message):
        await persistence.load_next()


class _StrictCheckpointState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    bot_id: UUID
    plot_origin: tuple[int, int, int]
    cursor: int

    @field_serializer("cursor")
    def serialize_cursor(self, value: Any) -> Any:
        # Keep hostile JSON primitives observable for the codec's write validator.
        return value


class _StrictCheckpointOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    action_id: UUID
    coordinate: tuple[int, int, int]
    cursor: int

    @field_serializer("cursor")
    def serialize_cursor(self, value: Any) -> Any:
        # Keep hostile JSON primitives observable for the codec's write validator.
        return value


class _NoCheckpointEffects:
    async def handle_transition(self, *_args: Any, **_kwargs: Any) -> None:
        pytest.fail("strict checkpoint continuation must not request hardware")

    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"strict checkpoint continuation must not access effect dependency {name}")


@dataclass
class _StrictCheckpointNode(BaseNode[_StrictCheckpointState, _NoCheckpointEffects, _StrictCheckpointOutput]):
    __pydantic_config__ = ConfigDict(strict=True, extra="forbid")
    action_id: UUID
    coordinate: tuple[int, int, int]
    cursor: int

    @field_serializer("cursor")
    def serialize_cursor(self, value: Any) -> Any:
        # Keep hostile JSON primitives observable for the codec's write validator.
        return value

    async def run(
        self, ctx: GraphRunContext[_StrictCheckpointState, _NoCheckpointEffects]
    ) -> End[_StrictCheckpointOutput]:
        assert ctx.state == _StrictCheckpointState(
            bot_id=UUID("12345678-1234-5678-1234-567812345678"), plot_origin=(12, 64, -5), cursor=3
        )
        assert self == _StrictCheckpointNode(UUID("87654321-4321-8765-4321-876543218765"), (13, 64, -5), 3)
        ctx.state.cursor += 1
        return End(
            _StrictCheckpointOutput(action_id=self.action_id, coordinate=self.coordinate, cursor=ctx.state.cursor)
        )


def _strict_checkpoint_graph() -> Graph[_StrictCheckpointState, _NoCheckpointEffects, Any, _StrictCheckpointOutput]:
    return build_serial_graph(
        nodes=(_StrictCheckpointNode,),
        state_type=_StrictCheckpointState,
        deps_type=_NoCheckpointEffects,
        output_type=_StrictCheckpointOutput,
    )


def _strict_checkpoint_document() -> dict[str, Any]:
    # Authored wire values are independent of the codec's serializer.
    return {
        "kind": "node",
        "id": "strict-cursor:1",
        "state": {"bot_id": "12345678-1234-5678-1234-567812345678", "plot_origin": [12, 64, -5], "cursor": 3},
        "node": {
            "node_id": "_StrictCheckpointNode",
            "action_id": "87654321-4321-8765-4321-876543218765",
            "coordinate": [13, 64, -5],
            "cursor": 3,
        },
        "status": "created",
        "start_ts": None,
        "duration": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["snapshot", "authored-document"])
async def test_strict_checkpoint_json_resumes_in_a_replacement_runner(source: str) -> None:
    graph = _strict_checkpoint_graph()
    store = InMemoryStasisStore()
    persistence = DurableStasisPhylactery(job_id="strict-resume", store=store)
    persistence.set_graph_types(graph)
    expected = _strict_checkpoint_document()
    if source == "snapshot":
        await persistence.snapshot_node(
            _StrictCheckpointState(
                bot_id=UUID("12345678-1234-5678-1234-567812345678"), plot_origin=(12, 64, -5), cursor=3
            ),
            _StrictCheckpointNode(UUID("87654321-4321-8765-4321-876543218765"), (13, 64, -5), 3),
        )
        documents = await store.load("strict-resume")
        assert documents is not None
        assert documents[0] == {**expected, "id": documents[0]["id"]}
    else:
        documents = [expected]

    # A JSON transfer discards Python identity; the new store and runner cannot
    # accidentally reuse the original state or node objects.
    replacement_store = InMemoryStasisStore()
    await replacement_store.replace("strict-resume", json.loads(json.dumps(documents)))
    replacement = DurableStasisPhylactery(job_id="strict-resume", store=replacement_store)
    effects = _NoCheckpointEffects()
    runner = GraphRunner[_StrictCheckpointState](orchestrator=effects, persistence=replacement, signal_priority=50)
    result = await runner.resume_graph(graph, deps=effects)
    assert result == _StrictCheckpointOutput(
        action_id=UUID("87654321-4321-8765-4321-876543218765"), coordinate=(13, 64, -5), cursor=4
    )
    completed = await replacement.load_all()
    assert len(completed) == 2
    assert isinstance(completed[0], NodeSnapshot)
    assert completed[0].status == "success"
    assert completed[0].state.cursor == 3
    assert isinstance(completed[1], EndSnapshot)
    assert completed[1].state == _StrictCheckpointState(
        bot_id=UUID("12345678-1234-5678-1234-567812345678"), plot_origin=(12, 64, -5), cursor=4
    )
    assert completed[1].result.data == result
    assert await replacement.load_next() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["state", "node", "output"])
@pytest.mark.parametrize("invalid", [True, "3", 3.0], ids=["boolean", "string", "float"])
async def test_strict_checkpoint_json_refuses_integer_coercion(boundary: str, invalid: Any) -> None:
    document = _strict_checkpoint_document()
    if boundary == "output":
        output_data: dict[str, Any] = {key: value for key, value in document["node"].items() if key != "node_id"}
        output_data["cursor"] = invalid
        document = {
            "kind": "end",
            "id": "strict-end:1",
            "state": document["state"],
            "result": {"data": output_data},
            "ts": "2026-10-10T12:00:00Z",
        }
    else:
        document[boundary]["cursor"] = invalid
    store = InMemoryStasisStore()
    await store.replace("strict-invalid", [document])
    persistence = DurableStasisPhylactery(job_id="strict-invalid", store=store)
    persistence.set_graph_types(_strict_checkpoint_graph())
    with pytest.raises(ValidationError) as raised:
        await persistence.load_all()
    assert [(error["loc"], error["type"]) for error in raised.value.errors()] == [(("cursor",), "int_type")]
    assert await store.load("strict-invalid") == [document]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "field", "value", "message"),
    [
        ("state", "unknown", "injected", "Extra inputs"),
        ("state", "bot_id", "invalid-uuid", "valid UUID"),
        ("node", "action_id", "invalid-uuid", "valid UUID"),
        ("node", "unknown", "injected", "unknown fields"),
        ("node", "node_id", "ForeignEffect", "not in its admitted graph"),
    ],
)
async def test_strict_checkpoint_json_refuses_corrupt_fields(
    boundary: str, field: str, value: str, message: str
) -> None:
    document = _strict_checkpoint_document()
    document[boundary][field] = value
    store = InMemoryStasisStore()
    await store.replace("strict-corrupt", [document])
    persistence = DurableStasisPhylactery(job_id="strict-corrupt", store=store)
    persistence.set_graph_types(_strict_checkpoint_graph())
    with pytest.raises(ValueError, match=message):
        await persistence.load_next()
    assert await store.load("strict-corrupt") == [document]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["state", "node", "output"])
@pytest.mark.parametrize("invalid", [True, "3", 3.0], ids=["boolean", "string", "float"])
async def test_invalid_strict_checkpoint_write_preserves_readable_history(boundary: str, invalid: Any) -> None:
    store = InMemoryStasisStore()
    persistence = DurableStasisPhylactery(job_id="strict-retained", store=store)
    persistence.set_graph_types(_strict_checkpoint_graph())
    state = _StrictCheckpointState(
        bot_id=UUID("12345678-1234-5678-1234-567812345678"), plot_origin=(12, 64, -5), cursor=3
    )
    node = _StrictCheckpointNode(UUID("87654321-4321-8765-4321-876543218765"), (13, 64, -5), 3)
    await persistence.snapshot_node(state, node)
    before = await store.load("strict-retained")
    output = _StrictCheckpointOutput(action_id=node.action_id, coordinate=node.coordinate, cursor=4)
    if boundary == "state":
        state.cursor = cast("int", invalid)
    elif boundary == "node":
        node.cursor = cast("int", invalid)
    else:
        output.cursor = cast("int", invalid)
    value = {"state": state, "node": node, "output": output}[boundary]
    serialized_cursor = TypeAdapter(type(value)).dump_python(value, mode="json")["cursor"]
    assert type(serialized_cursor) is type(invalid)
    assert serialized_cursor == invalid
    write = (
        persistence.snapshot_node(state, node) if boundary == "node" else persistence.snapshot_end(state, End(output))
    )
    with pytest.raises(ValidationError) as raised:
        await write
    assert [(error["loc"], error["type"]) for error in raised.value.errors()] == [(("cursor",), "int_type")]
    assert await store.load("strict-retained") == before
    replacement = DurableStasisPhylactery(job_id="strict-retained", store=store)
    replacement.set_graph_types(_strict_checkpoint_graph())
    restored = await replacement.load_next()
    assert restored is not None
    assert restored.state.cursor == 3
    assert isinstance(restored.node, _StrictCheckpointNode)
    assert restored.node.cursor == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running", "error"])
async def test_strict_checkpoint_json_does_not_replay_uncertain_status(status: str) -> None:
    document = _strict_checkpoint_document()
    document["status"] = status
    store = InMemoryStasisStore()
    await store.replace("strict-status", [document])
    persistence = DurableStasisPhylactery(job_id="strict-status", store=store)
    persistence.set_graph_types(_strict_checkpoint_graph())
    restored = await persistence.load_all()
    assert len(restored) == 1
    assert isinstance(restored[0], NodeSnapshot)
    assert restored[0].status == status
    assert await persistence.load_next() is None
    assert await store.load("strict-status") == [document]
