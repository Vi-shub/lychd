"""Influence follows the complete current context, never its display labels alone."""

from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest

from lychd.domain.cortex.context import AssembledContext, Block, ContextOrchestrator
from lychd.domain.privacy.labels import (
    HandlingConstraint,
    PrivacyCategory,
    PrivacyClass,
    PrivatizationLabel,
)


def _public_context() -> AssembledContext:
    return AssembledContext(
        blocks=(
            Block(
                layer=1,
                key="public-fixture",
                text="Public instructions",
                label=PrivatizationLabel(classification=PrivacyClass.PUBLIC, weight=0, lineage_known=True),
            ),
        ),
        state_window=[],
        continuation=[],
        query="",
    )


def test_block_and_empty_context_default_to_unknown_restricted() -> None:
    assert Block(layer=6, key="query", text="hello").label == PrivatizationLabel()
    context = replace(_public_context(), blocks=())
    assert context.influence_label == PrivatizationLabel()


@pytest.mark.parametrize("lane", ["query", "state_window", "continuation"])
def test_ungoverned_material_cannot_inherit_a_public_display_label(lane: str) -> None:
    original = _public_context()
    assert original.influence_label.classification is PrivacyClass.PUBLIC
    changed = replace(original, **{lane: "private query" if lane == "query" else [{"tool_result": "private"}]})
    assert changed.influence_label.classification is PrivacyClass.RESTRICTED
    assert changed.influence_label.weight == 1
    assert changed.influence_label.lineage_known is False
    assert original.influence_label.classification is PrivacyClass.PUBLIC


def test_transient_material_cannot_inherit_an_otherwise_public_context_label() -> None:
    original = _public_context()
    changed = replace(original, transient_chars=1)
    assert changed.influence_label == PrivatizationLabel()
    assert changed.blocks == original.blocks
    assert changed.model_history() == []
    assert changed.query == ""
    assert original.influence_label.classification is PrivacyClass.PUBLIC


@pytest.mark.parametrize("transient", [False, True])
def test_aggregate_preserves_all_known_influence_when_unknown_material_arrives(*, transient: bool) -> None:
    subject, parent = uuid4(), uuid4()
    label = PrivatizationLabel(
        classification=PrivacyClass.PRIVATE,
        weight=0.8,
        categories=frozenset({PrivacyCategory.DIRECT_IDENTIFIER}),
        subjects=frozenset({subject}),
        material_parents=frozenset({parent}),
        constraints=frozenset({HandlingConstraint.LOCAL_ONLY}),
        lineage_known=True,
    )
    context = replace(
        _public_context(),
        blocks=(*_public_context().blocks, Block(layer=2, key="private-fixture", text="private", label=label)),
    )
    assert context.influence_label == label
    # AssembledContext deliberately detaches mutable history at its public seams.
    # A later mutation must not leave a lower cached label on the resulting value.
    if transient:
        context = replace(context, transient_chars=1)
    else:
        context.continuation.append({"tool_result": "unknown source"})
    aggregate = context.influence_label
    assert aggregate.classification is PrivacyClass.RESTRICTED
    assert aggregate.categories == label.categories
    assert aggregate.subjects == label.subjects
    assert aggregate.material_parents == label.material_parents
    assert aggregate.constraints == label.constraints


def test_normal_assembly_and_cached_projection_remain_restricted() -> None:
    class Registry:
        def list_capability_states(self) -> list[Any]:
            return []

    context = ContextOrchestrator(registry=cast("Any", Registry()))
    history = [{"tool_result": "private result"}]
    assembled = context.assemble(run_id="run", session_id="session", query="question", history=history)
    cached = context.get("run")
    assert cached is not None
    assert assembled.influence_label == cached.influence_label == PrivatizationLabel()
    assert all(block.label.lineage_known is False for block in assembled.blocks)
    history[0]["tool_result"] = "changed"
    assert assembled.model_history() == [{"tool_result": "private result"}]
    assert cached.model_history() == assembled.model_history()
