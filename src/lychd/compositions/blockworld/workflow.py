"""The explicitly assembled serial placement Pattern; normal boot imports none of it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel
from pydantic_graph import BaseNode, End, GraphRunContext

from lychd.agents.router import Intent
from lychd.agents.services import WorkflowServices
from lychd.agents.workflows.base import Gate, PatternEdge, PatternManifest, PatternNode, Trigger, Workflow
from lychd.compositions.blockworld.ports import Refused
from lychd.compositions.blockworld.schemas import PlacementIntegrityError, PlacementResult, PlacementStateV1
from lychd.compositions.blockworld.services import PlacementService, validate_action
from lychd.domain.cortex.graph import build_serial_graph


@dataclass(frozen=True, kw_only=True)
class BlockworldDeps:
    common: WorkflowServices
    placement: PlacementService


async def _owned(ctx: GraphRunContext[PlacementStateV1, BlockworldDeps]) -> Any:
    state = ctx.state
    if state.action_id is None:
        msg = "Station has no retained preparation."
        raise PlacementIntegrityError(msg)
    action = await ctx.deps.placement.store.read_action(state.action_id)
    mission = await ctx.deps.placement.store.load_for_run(state.run_id)
    validate_action(action, mission)
    if (action.run_id, action.consent_id, action.preparation_digest, action.native_binding.digest) != (
        state.run_id,
        state.pending_consent_id,
        state.preparation_digest,
        state.native_binding_digest,
    ):
        msg = "Checkpoint references differ from retained placement."
        raise PlacementIntegrityError(msg)
    return action


def _replace(ctx: GraphRunContext[PlacementStateV1, BlockworldDeps], **values: Any) -> None:
    document = ctx.state.model_dump(mode="json")
    document.update(values)
    replacement = PlacementStateV1.model_validate(document)
    for field in PlacementStateV1.model_fields:
        setattr(ctx.state, field, getattr(replacement, field))


@dataclass
class PreparePlacement(BaseNode[PlacementStateV1, BlockworldDeps]):
    async def run(
        self, ctx: GraphRunContext[PlacementStateV1, BlockworldDeps]
    ) -> AwaitPlacementConsent | ProjectPlacementResult:
        if ctx.state.action_id is not None or ctx.state.result is not None:
            msg = "Prepare cannot replace existing checkpoint custody."
            raise PlacementIntegrityError(msg)
        prepared = await ctx.deps.placement.prepare(ctx.state.run_id)
        if isinstance(prepared, Refused):
            _replace(ctx, result=prepared.result.model_dump(mode="json"))
            return ProjectPlacementResult()
        action = await ctx.deps.placement.store.read_action(prepared.action_id)
        _replace(
            ctx,
            action_id=action.action_id,
            preparation_digest=action.preparation_digest,
            pending_consent_id=action.consent_id,
            native_binding_digest=action.native_binding.digest,
        )
        await _owned(ctx)
        return AwaitPlacementConsent()


@dataclass
class AwaitPlacementConsent(Gate, BaseNode[PlacementStateV1, BlockworldDeps]):
    async def run(
        self, ctx: GraphRunContext[PlacementStateV1, BlockworldDeps]
    ) -> AwaitPlacementConsent | ExecutePlacement | ProjectPlacementResult:
        action = await _owned(ctx)
        result = await ctx.deps.placement.gate(
            action.action_id, ctx.deps.common.sigil_provider(), ctx.deps.common.consents
        )
        if result is not None:
            _replace(ctx, result=result.model_dump(mode="json"))
            return ProjectPlacementResult()
        return ExecutePlacement()


@dataclass
class ExecutePlacement(BaseNode[PlacementStateV1, BlockworldDeps]):
    async def run(self, ctx: GraphRunContext[PlacementStateV1, BlockworldDeps]) -> ProjectPlacementResult:
        action = await _owned(ctx)
        result = await ctx.deps.placement.execute(
            action.action_id, action.native_binding, ctx.deps.common.sigil_provider()
        )
        _replace(ctx, result=result.model_dump(mode="json"))
        return ProjectPlacementResult()


@dataclass
class ProjectPlacementResult(BaseNode[PlacementStateV1, BlockworldDeps, PlacementResult]):
    async def run(self, ctx: GraphRunContext[PlacementStateV1, BlockworldDeps]) -> End[PlacementResult]:
        if ctx.state.action_id is None:
            mission = await ctx.deps.placement.store.load_for_run(ctx.state.run_id)
            result = mission.result
            if result is None or result.kind != "not_issued" or result != ctx.state.result:
                msg = "Mission-only refusal is not retained."
                raise PlacementIntegrityError(msg)
        else:
            await _owned(ctx)
            result = await ctx.deps.placement.retained_result(ctx.state.action_id)
            if result != ctx.state.result:
                msg = "Projected result differs from retained custody."
                raise PlacementIntegrityError(msg)
        return End(result)


def _state(intent: Intent) -> PlacementStateV1:
    if intent.run_id is None:
        msg = "Placement state requires persisted Run identity."
        raise PlacementIntegrityError(msg)
    return PlacementStateV1(
        schema_version=1,
        run_id=intent.run_id,
        session_id=intent.session_id,
        action_id=None,
        preparation_digest=None,
        pending_consent_id=None,
        native_binding_digest=None,
        result=None,
    )


def _validate_state(intent: Intent, state: BaseModel) -> None:
    if type(state) is not PlacementStateV1 or state.run_id != intent.run_id or state.session_id != intent.session_id:
        msg = "Checkpoint differs from admitted Intent."
        raise PlacementIntegrityError(msg)
    state.model_dump(mode="json")


def placement_workflow(placement: PlacementService) -> Workflow:
    graph = build_serial_graph(
        nodes=(PreparePlacement, AwaitPlacementConsent, ExecutePlacement, ProjectPlacementResult),
        state_type=PlacementStateV1,
        deps_type=BlockworldDeps,
        output_type=cast("Any", PlacementResult),
        name="blockworld_placement",
    )
    nodes = (
        ("prepare", "Prepare placement", "step", PreparePlacement),
        ("consent", "Await placement consent", "gate", AwaitPlacementConsent),
        ("execute", "Execute placement", "step", ExecutePlacement),
        ("project", "Project placement result", "step", ProjectPlacementResult),
        ("end", "End", "terminal", None),
    )
    edges = (
        ("prepare-to-consent", "prepare", "consent"),
        ("prepare-to-project", "prepare", "project"),
        ("consent-waits", "consent", "consent"),
        ("consent-to-execute", "consent", "execute"),
        ("consent-to-project", "consent", "project"),
        ("execute-to-project", "execute", "project"),
        ("project-to-end", "project", "end"),
    )
    return Workflow(
        name="blockworld.bounded_mission",
        title="Place one block on the admitted plot",
        description="Review and attempt one retained bounded placement.",
        trigger=Trigger(
            hint="An explicitly admitted placement", match=lambda intent: intent.source == "blockworld.placement"
        ),
        graph=graph,
        start_node=PreparePlacement,
        make_state=_state,
        validate_state=_validate_state,
        bind_deps=lambda common: BlockworldDeps(common=common, placement=placement),
        manifest=PatternManifest(
            key="blockworld.bounded_mission",
            revision="2",
            implementation_revision="py.1",
            checkpoint_schema="blockworld-placement-state-v1",
            entry_node="prepare",
            nodes=tuple(
                PatternNode(key=key, label=label, kind=cast("Any", kind), implementation=implementation)
                for key, label, kind, implementation in nodes
            ),
            edges=tuple(PatternEdge(key=key, source=source, target=target) for key, source, target in edges),
        ),
    )
