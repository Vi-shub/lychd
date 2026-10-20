"""Validate dated installed-hybrid facts offline without runtime surfaces."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[4]
_RECEIPT = _ROOT / "tests/fixtures/animator/local-bridge-installed-hybrid-2026-10-20.json"
_SHA256 = "528798def54c5f25f656acd1a1d6ba17a620dfd93a6fe46e6b596da0d490017d"
_SOURCES = (
    "clients/web/package-lock.json",
    "clients/web/package.json",
    "clients/web/src/app.css",
    "clients/web/src/lib/api/client.ts",
    "clients/web/src/lib/api/csrf.ts",
    "clients/web/src/lib/api/models.ts",
    "clients/web/src/lib/api/openapi.d.ts",
    "clients/web/src/lib/api/run-stream.ts",
    "clients/web/src/lib/api/runtime.ts",
    "clients/web/src/lib/atlas/navigation.ts",
    "clients/web/src/lib/bridge/projection.ts",
    "clients/web/src/lib/bridge/workspace.svelte.ts",
    "clients/web/src/lib/components/AltarShell.svelte",
    "clients/web/src/lib/components/AltarWelcome.svelte",
    "clients/web/src/lib/components/AnimatorRail.svelte",
    "clients/web/src/lib/components/ArchiveRecallPanel.svelte",
    "clients/web/src/lib/components/AtlasLinks.svelte",
    "clients/web/src/lib/components/BridgePreferenceSummary.svelte",
    "clients/web/src/lib/components/BridgeView.svelte",
    "clients/web/src/lib/components/CircleRun.svelte",
    "clients/web/src/lib/components/ConsentCard.svelte",
    "clients/web/src/lib/components/CreationSummary.svelte",
    "clients/web/src/lib/components/DelegateMark.svelte",
    "clients/web/src/lib/components/GenUI.svelte",
    "clients/web/src/lib/components/LoomView.svelte",
    "clients/web/src/lib/components/OrbField.svelte",
    "clients/web/src/lib/components/OrbView.svelte",
    "clients/web/src/lib/components/graph/FlowEdge.svelte",
    "clients/web/src/lib/components/graph/FlowNode.svelte",
    "clients/web/src/lib/components/graph/FlowSurface.svelte",
    "clients/web/src/lib/components/graph/GraphCanvas.svelte",
    "clients/web/src/lib/components/graph/layout.ts",
    "clients/web/src/lib/components/orb-evidence.ts",
    "clients/web/src/lib/delegation/presentation.ts",
    "clients/web/src/lib/graph/loom.ts",
    "clients/web/src/lib/graph/model.ts",
    "clients/web/src/lib/graph/orb.ts",
    "clients/web/src/lib/navigation/instruments.ts",
    "clients/web/src/lib/orb/live.ts",
    "clients/web/src/lib/orb/playback.ts",
    "clients/web/src/routes/+layout.svelte",
    "clients/web/src/routes/+layout.ts",
    "clients/web/src/routes/bridge/+page.svelte",
    "clients/web/src/routes/bridge/[session_id]/+page.svelte",
    "clients/web/src/routes/loom/+page.svelte",
    "clients/web/src/routes/loom/[pattern_id]/[revision]/+page.svelte",
    "clients/web/src/routes/orb/+page.svelte",
    "clients/web/src/routes/orb/[run_id]/+page.svelte",
    "scripts/local_model_probe.py",
    "src/lychd/__main__.py",
    "src/lychd/agents/context_limits.py",
    "src/lychd/agents/factory.py",
    "src/lychd/agents/outputs.py",
    "src/lychd/agents/router.py",
    "src/lychd/agents/the_first_one.py",
    "src/lychd/agents/workflows/__init__.py",
    "src/lychd/agents/workflows/_legacy_inline.py",
    "src/lychd/agents/workflows/base.py",
    "src/lychd/agents/workflows/bridge_chat.py",
    "src/lychd/agents/workflows/nodes.py",
    "src/lychd/app.py",
    "src/lychd/cli/base.py",
    "src/lychd/cli/commands.py",
    "src/lychd/cli/operator.py",
    "src/lychd/compositions/creation/schemas.py",
    "src/lychd/compositions/creation/services.py",
    "src/lychd/config/components.py",
    "src/lychd/config/constants.py",
    "src/lychd/config/settings/root.py",
    "src/lychd/config/settings/server.py",
    "src/lychd/config/settings/weaver.py",
    "src/lychd/db/authority.py",
    "src/lychd/db/bootstrap.py",
    "src/lychd/db/engine.py",
    "src/lychd/db/migrations/versions/0016_creation_investigation.py",
    "src/lychd/db/migrations/versions/0018_creation_successor.py",
    "src/lychd/db/models/__init__.py",
    "src/lychd/db/models/creation.py",
    "src/lychd/db/sessions.py",
    "src/lychd/domain/animation/capabilities.py",
    "src/lychd/domain/animation/model_factory.py",
    "src/lychd/domain/animation/schemas/generation.py",
    "src/lychd/domain/animation/schemas/runes/animators.py",
    "src/lychd/domain/animation/schemas/runes/models.py",
    "src/lychd/domain/animation/services/adapters/catalog.py",
    "src/lychd/domain/animation/services/adapters/runtimes/openai_compat.py",
    "src/lychd/domain/animation/services/adapters/surfaces.py",
    "src/lychd/domain/animation/services/loader.py",
    "src/lychd/domain/animation/services/registry.py",
    "src/lychd/domain/codex/local_access.py",
    "src/lychd/domain/codex/middleware.py",
    "src/lychd/domain/cortex/bridge_preference.py",
    "src/lychd/domain/cortex/context.py",
    "src/lychd/domain/cortex/dispatcher.py",
    "src/lychd/domain/cortex/engine.py",
    "src/lychd/domain/cortex/execution_context.py",
    "src/lychd/domain/cortex/graph.py",
    "src/lychd/domain/cortex/graph_runner.py",
    "src/lychd/domain/cortex/leases.py",
    "src/lychd/domain/cortex/ledger.py",
    "src/lychd/domain/cortex/runs.py",
    "src/lychd/domain/cortex/stasis.py",
    "src/lychd/domain/web/contracts.py",
    "src/lychd/domain/web/orb.py",
    "src/lychd/domain/web/projection.py",
    "src/lychd/domain/web/schemas.py",
    "src/lychd/extensions/builtin/animator/soulstones/vllm.py",
    "src/lychd/extensions/builtin/animator/vllm/register.py",
    "src/lychd/extensions/context.py",
    "src/lychd/extensions/manager.py",
    "src/lychd/ghouls/runs.py",
    "src/lychd/interface/api/creation.py",
    "src/lychd/interface/api/lifecycle.py",
    "src/lychd/interface/server.py",
    "src/lychd/interface/server_policy.py",
    "src/lychd/interface/web/altar.py",
    "src/lychd/interface/web/altar_services.py",
    "src/lychd/interface/web/bridge.py",
    "src/lychd/interface/web/bridge_stream.py",
    "src/lychd/interface/web/lifespan.py",
    "src/lychd/interface/web/loom.py",
    "src/lychd/interface/web/orb.py",
    "src/lychd/system/constants.py",
    "src/lychd/system/operator/__init__.py",
    "src/lychd/system/operator/admission.py",
    "src/lychd/system/operator/composition.py",
    "src/lychd/system/operator/control.py",
    "src/lychd/system/operator/models.py",
    "src/lychd/system/services/queues.py",
    "tests/conftest.py",
    "tests/integration/test_local_bridge_native_tcp.py",
    "tests/integration/test_local_bridge_process_restart.py",
    "tests/integration/test_local_bridge_runtime.py",
    "tests/integration/test_production_wiring.py",
    "tests/integration/test_server_process.py",
    "tests/unit/domain/animation/test_local_bridge_native_tcp.py",
)
_DEPENDENCIES = {
    "alembic": "1.19.2",
    "asyncpg": "0.32.0",
    "click": "8.5.0",
    "cryptography": "50.0.2",
    "dbus-fast": "5.2.0",
    "granian": "2.8.2",
    "httpx2": "2.12.0",
    "litestar": "2.24.0",
    "litestar-saq": "0.8.0",
    "openai": "3.28.0",
    "psycopg": "3.3.6",
    "pydantic": "2.14.0",
    "pydantic-ai-slim": "2.55.0",
    "pydantic-graph": "2.55.0",
    "saq": "0.26.4",
    "sqlalchemy": "2.0.54",
}
_FACTS_SHA256 = "73414cc23b86edd199982203ff7d73ad4552f1accb0e7a0afceefdb38debb476"
_SOURCE_IDENTITY_SHA256 = "b485c7453d7d43e198aa923a04c75321a57c60257d1aefcccb0687d4467afb9f"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _read() -> dict[str, Any]:
    payload = _RECEIPT.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == _SHA256
    return cast("dict[str, Any]", json.loads(payload))


def test_installed_hybrid_receipt_preserves_exact_actual_scope_and_oracles() -> None:
    receipt = _read()
    assert set(receipt) == {
        "schema",
        "record_kind",
        "retained_pattern_manifest_sha256",
        "selected_dependency_versions",
        "facts",
        "qualification_date",
        "retained_turns_raw_file_sha256",
        "source_identity",
        "retained_history_sha256",
        "retained_turns_logical_sha256",
        "status",
        "source_fingerprints",
        "evidence_sha256",
        "freshness_check",
    }
    assert receipt["schema"] == "local-bridge-installed-hybrid@1"
    assert receipt["record_kind"] == "sanitized_public_trusted_installed_local_vllm_hybrid_qualification"
    assert receipt["qualification_date"] == "2026-10-20"
    assert receipt["status"] == "passed"
    assert receipt["freshness_check"] == "offline-source-package-and-selected-dependency-metadata-only"
    assert hashlib.sha256(_canonical(receipt["facts"])).hexdigest() == _FACTS_SHA256
    assert hashlib.sha256(_canonical(receipt["source_identity"])).hexdigest() == _SOURCE_IDENTITY_SHA256
    facts = receipt["facts"]
    assert facts["execution"]["raw_helper_proof_success"] is False
    assert facts["execution"]["separate_Root_scoped_qualification_accepted"] is True
    assert all(
        facts["execution"][k] is False
        for k in (
            "generated_Quadlet_execution",
            "model_runtime_execution",
            "Podman_container_effects",
            "managed_model_lifecycle_qualified",
            "F1_browser_exercised",
        )
    )
    assert facts["calls"]["real_upstream_POSTs"] == 1
    assert (facts["calls"]["actual_prompt_tokens"], facts["calls"]["actual_generated_tokens"]) == (605, 200)
    assert facts["calls"]["second_boot_model_POSTs"] == 0
    assert facts["lifecycle"]["distinct_native_boots"] == 2
    assert facts["lifecycle"]["owned_secret_count"] == 5
    assert facts["lifecycle"]["inert_model_marker_activations"] == 0
    assert facts["useful_task"]["SDK_histories_byte_identical"] is True
    assert facts["useful_task"]["same_wire_and_SDK_provider_identity_and_result"] is True
    assert facts["useful_task"]["retained_manifest_matches_installed_BRIDGE_CHAT_BOUND_snapshot"] is True
    assert facts["resources"]["native_aggregate_hard_memory_CPU_cap_claim"] is False
    assert facts["closure"]["Root_independent_read_only_terminal_accepted"] is True
    assert facts["closure"]["next_occurrence_authorized"] is False
    assert (
        receipt["evidence_sha256"]["Root_accepted"]
        == "52fabd6589c46a3c8807a366eb872b69add2f3eb7f70a50bc4b0d5183dca2ad7"
    )
    assert set(receipt["evidence_sha256"]) == {
        "application_receipt",
        "relay_driver",
        "restart_SDK_history",
        "Root_audit_log",
        "helper_source_repair",
        "installed_package_artifact",
        "restart_Orb",
        "restart_turns",
        "task_turns",
        "Root_accepted",
        "Root_consumer_terminal_before_collection",
        "Root_terminal_inputs",
        "Root_audit_source",
        "installed_artifact_binding",
        "installed_runtime_freeze",
        "task_SDK_history",
        "independent_raw_review",
        "helper_source_review",
        "relay_terminal",
        "Root_self_gate",
        "image_source_agreement",
        "relay_wire",
        "Root_audit_result",
        "Root_independent_gate",
        "relay_runtime_freeze",
        "Root_useful_SDK_review",
        "Root_warm_terminal",
        "Root_effect_release",
        "Root_resources",
        "task_Orb",
    }
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in receipt["evidence_sha256"].values())


def test_installed_hybrid_receipt_binds_current_source_and_artifact_scope() -> None:
    receipt = _read()
    assert set(receipt["source_fingerprints"]) == set(_SOURCES)
    assert len(_SOURCES) == 136
    for relative in _SOURCES:
        path = _ROOT / relative
        assert path.is_file()
        assert not path.is_symlink()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt["source_fingerprints"][relative], relative
    root = _ROOT / "src/lychd"
    production: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == "public" or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        assert not path.is_symlink()
        if path.is_file():
            production[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(production) == 494
    assert (
        hashlib.sha256(_canonical(production)).hexdigest()
        == "ac1376eb93a2a8ae2528477af51077b36ea64eced098cfdb38e118104cb98ead"
    )
    identity = receipt["source_identity"]
    assert identity["installed_noneditable_package_file_count"] == 548
    assert (
        identity["installed_package_manifest_sha256"]
        == "ffac71ba4bb9f3c8a3f07a10be8ff815676c9d2a6dcb6f1c9f8ca73f3920f89f"
    )
    assert identity["wheel_sha256"] == "284386357196466fb69ac6e9b1218c515fa7884617d095803b2aa7ff7f2de45c"
    assert identity["whole_current_checkout_qualified"] is False
    assert receipt["selected_dependency_versions"] == _DEPENDENCIES
    locked = tomllib.loads((_ROOT / "uv.lock").read_text())["package"]
    for name, expected in _DEPENDENCIES.items():
        assert version(name) == expected, name
        assert any(p["name"] == name and p["version"] == expected for p in locked), name


def test_installed_hybrid_receipt_omits_private_raw_material() -> None:
    _read()
    text = _RECEIPT.read_text()
    assert not re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", text)
    assert not any(
        value in text
        for value in (
            "://",
            "/home/",
            "/tmp/",  # noqa: S108 - refuse private host material
            "/run/user/",
            "127.0.0.1",
            "::1",
            "password",
            "username",
            '"pid"',
            '"group"',
            '"parent_pid"',
            '"start_time"',
            '"response_id"',
            '"session_id"',
            '"run_id"',
            '"container_id"',
            '"prompt"',
            '"message_history"',
        )
    )
