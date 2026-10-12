"""Private local bindings must not rewrite or escape the persisted v2 Pattern."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from lychd.agents.workflows import BRIDGE_CHAT, builtin_workflow_registry
from lychd.agents.workflows._legacy_inline import legacy_inline_lock
from lychd.agents.workflows.base import Workflow


@pytest.mark.parametrize("workflow", builtin_workflow_registry().all(), ids=lambda workflow: workflow.manifest.revision)
def test_private_lock_covers_exactly_executable_stations(workflow: Workflow) -> None:
    manifest = workflow.manifest
    snapshot_bytes = json.dumps(manifest.snapshot(), sort_keys=True).encode()
    lock = legacy_inline_lock(manifest)
    executable = {node.key: node for node in manifest.nodes if node.implementation is not None}

    assert lock.version == "internal-v0"
    assert lock == legacy_inline_lock(manifest)
    assert lock.digest == legacy_inline_lock(manifest).digest
    assert len(lock.digest) == 64
    assert {binding.placement.station_id for binding in lock.bindings} == set(executable)
    assert len({binding.placement.contract for binding in lock.bindings}) == len(executable)
    assert len({binding.placement for binding in lock.bindings}) == len(executable)
    assert len({binding.implementation for binding in lock.bindings}) == len(executable)
    for binding in lock.bindings:
        placement, implementation = binding.placement, binding.implementation
        contract = placement.contract
        node = executable[placement.station_id]
        assert contract.kind == "legacy_inline"
        assert (contract.pattern_id, contract.pattern_revision, contract.manifest_digest, contract.station_id) == (
            manifest.key,
            manifest.revision,
            manifest.digest,
            node.key,
        )
        assert placement.station_kind == node.kind
        assert implementation.contract == contract
        assert implementation.implementation_revision == manifest.implementation_revision
        assert implementation.checkpoint_schema == manifest.checkpoint_schema
        assert node.implementation is not None
        assert implementation.node_fingerprint == f"{node.implementation.__module__}:{node.implementation.__qualname__}"
    assert json.dumps(manifest.snapshot(), sort_keys=True).encode() == snapshot_bytes


def test_changed_python_binding_changes_private_lock_without_changing_v2_bytes() -> None:
    manifest = BRIDGE_CHAT.manifest
    node = manifest.nodes[0]
    assert node.implementation is not None
    alternate = type("AlternateNode", (node.implementation,), {"__module__": "test_local_binding"})
    changed = replace(manifest, nodes=(replace(node, implementation=alternate), *manifest.nodes[1:]))
    original_lock, changed_lock = legacy_inline_lock(manifest), legacy_inline_lock(changed)

    assert json.dumps(manifest.snapshot(), sort_keys=True) == json.dumps(changed.snapshot(), sort_keys=True)
    assert manifest.digest == changed.digest
    assert original_lock.digest != changed_lock.digest
    assert tuple(binding.placement for binding in original_lock.bindings) == tuple(
        binding.placement for binding in changed_lock.bindings
    )
    changed_bindings = [
        (before, after)
        for before, after in zip(original_lock.bindings, changed_lock.bindings, strict=True)
        if before != after
    ]
    assert len(changed_bindings) == 1
    before, after = changed_bindings[0]
    assert before.implementation.contract == after.implementation.contract
    assert after.implementation.node_fingerprint == "test_local_binding:AlternateNode"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "another_pattern"),
        ("revision", "another_revision"),
        ("implementation_revision", "another_implementation"),
        ("checkpoint_schema", "another_checkpoint"),
    ],
)
def test_private_lock_binds_exact_manifest_identity(field: str, value: str) -> None:
    manifest = BRIDGE_CHAT.manifest
    changed = replace(manifest, **{field: value})

    assert legacy_inline_lock(manifest).digest != legacy_inline_lock(changed).digest


def test_private_binding_values_are_frozen() -> None:
    lock = legacy_inline_lock(BRIDGE_CHAT.manifest)
    binding = lock.bindings[0]
    for value, field in (
        (lock, "bindings"),
        (binding, "implementation"),
        (binding.placement, "station_id"),
        (binding.placement.contract, "station_id"),
        (binding.implementation, "node_fingerprint"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, "changed")
