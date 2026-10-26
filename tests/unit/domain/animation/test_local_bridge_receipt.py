"""Preserve dated pre-removal ASGI facts; no current container qualification."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from tests.support.local_bridge_receipt_freshness import (
    read_historical_receipt,
    require_historical_source_map,
    require_historical_tcp_coverage,
)

_ROOT = Path(__file__).resolve().parents[4]
_RECEIPT = _ROOT / "tests/fixtures/animator/local-bridge-vllm-runtime.json"
_SOURCES = {
    "scripts/local_model_probe.py",
    "src/lychd/app.py",
    "src/lychd/compositions/creation/schemas.py",
    "src/lychd/compositions/creation/services.py",
    "src/lychd/config/components.py",
    "src/lychd/config/constants.py",
    "src/lychd/config/settings/root.py",
    "src/lychd/config/settings/weaver.py",
    "src/lychd/domain/codex/local_access.py",
    "src/lychd/domain/codex/middleware.py",
    "src/lychd/domain/animation/model_factory.py",
    "src/lychd/domain/animation/capabilities.py",
    "src/lychd/domain/animation/schemas/generation.py",
    "src/lychd/domain/animation/schemas/runes/animators.py",
    "src/lychd/domain/animation/schemas/runes/models.py",
    "src/lychd/domain/animation/services/adapters/catalog.py",
    "src/lychd/domain/animation/services/loader.py",
    "src/lychd/domain/animation/services/registry.py",
    "src/lychd/domain/animation/services/adapters/runtimes/openai_compat.py",
    "src/lychd/domain/animation/services/adapters/surfaces.py",
    "src/lychd/domain/cortex/bridge_preference.py",
    "src/lychd/domain/cortex/context.py",
    "src/lychd/domain/cortex/dispatcher.py",
    "src/lychd/domain/cortex/engine.py",
    "src/lychd/domain/cortex/graph_runner.py",
    "src/lychd/domain/cortex/leases.py",
    "src/lychd/domain/cortex/ledger.py",
    "src/lychd/domain/cortex/runs.py",
    "src/lychd/agents/context_limits.py",
    "src/lychd/agents/factory.py",
    "src/lychd/agents/outputs.py",
    "src/lychd/agents/router.py",
    "src/lychd/agents/the_first_one.py",
    "src/lychd/agents/workflows/bridge_chat.py",
    "src/lychd/agents/workflows/nodes.py",
    "src/lychd/db/authority.py",
    "src/lychd/db/bootstrap.py",
    "src/lychd/db/engine.py",
    "src/lychd/db/sessions.py",
    "src/lychd/db/models/__init__.py",
    "src/lychd/db/models/creation.py",
    "src/lychd/db/migrations/versions/0016_creation_investigation.py",
    "src/lychd/db/migrations/versions/0018_creation_successor.py",
    "src/lychd/extensions/context.py",
    "src/lychd/extensions/manager.py",
    "src/lychd/extensions/builtin/animator/vllm/register.py",
    "src/lychd/extensions/builtin/animator/soulstones/vllm.py",
    "src/lychd/interface/api/lifecycle.py",
    "src/lychd/interface/api/creation.py",
    "src/lychd/interface/server_policy.py",
    "src/lychd/interface/web/altar_services.py",
    "src/lychd/interface/web/bridge.py",
    "src/lychd/interface/web/lifespan.py",
    "src/lychd/ghouls/runs.py",
    "src/lychd/system/operator/admission.py",
    "src/lychd/system/services/queues.py",
    "tests/conftest.py",
    "tests/integration/test_local_bridge_runtime.py",
    "tests/integration/test_production_wiring.py",
}
_HISTORICAL_DEPENDENCIES = {
    "advanced-alchemy": "1.11.0",
    "alembic": "1.19.2",
    "asyncpg": "0.32.0",
    "cryptography": "50.0.2",
    "docker": "7.2.0",
    "http-message-signatures": "2.0.1",
    "httpx": "0.28.1",
    "httpx2": "2.12.0",
    "litestar": "2.24.0",
    "litestar-saq": "0.8.0",
    "openai": "3.28.0",
    "psycopg": "3.3.6",
    "pydantic": "2.14.0",
    "pydantic-ai-slim": "2.55.0",
    "pydantic-graph": "2.55.0",
    "pydantic-settings": "2.15.0",
    "pytest": "9.1.1",
    "saq": "0.26.4",
    "sqlalchemy": "2.0.54",
    "testcontainers": "4.15.0",
    "typing-extensions": "4.16.0",
}


def test_historical_local_bridge_receipt_keeps_its_exact_boundary() -> None:
    receipt = read_historical_receipt(_RECEIPT)
    assert set(receipt) == {
        "schema",
        "record_kind",
        "qualification_date",
        "status",
        "source_identity",
        "execution",
        "model",
        "outcome",
        "database",
        "runner",
        "retained_history_evidence_id",
        "timings_seconds",
        "qualification_evidence_ids",
        "code_evidence",
        "representation_redaction",
        "dependency_versions",
        "source_fingerprints",
        "freshness_check",
        "identity_boundary",
        "unexercised",
    }
    assert receipt["schema"] == "local-bridge-vllm-runtime@2"
    assert receipt["record_kind"] == "sanitized_public_local_bridge_qualification"
    assert receipt["qualification_date"] == "2026-10-19"
    assert receipt["status"] == "passed"
    assert receipt["representation_redaction"] == {
        "redacted_on": "2026-10-26",
        "kind": "private-evidence-fingerprints-to-opaque-references",
        "new_runtime_qualification": False,
    }
    assert "The optional thinking policy was omitted" in receipt["identity_boundary"]
    assert "Creation investigation was omitted." in receipt["identity_boundary"]
    assert receipt["source_identity"] == {
        "base_revision_declared": "31b39d03c389f09bbfe5d918f5f532cf636eb35b",
        "clean_checkout_attested": False,
        "atomic_transitive_closure_attested": False,
        "scope": "bounded-critical-source-and-test-fingerprints",
    }
    assert receipt["execution"] == {
        "http": "litestar.testing.TestClient",
        "application": "real-create_app-two-in-process-lifespans",
        "worker": "real-in-process-SAQ",
        "graph": "real-Bridge-Graph",
        "dispatch": "real-Dispatcher-and-LeaseLedger",
        "agent": "real-AgentForge-and-Pydantic-AI",
        "database": "real-disposable-PostgreSQL",
        "input_assembly": "source-registered-builtins-explicit-SettingsSnapshot-and-RuneRegistry",
        "runtime_actuator": "refusing-fixture-port",
        "model_transport": "real-streaming-OpenAI-compatible-chat",
    }
    assert receipt["model"] == {
        "served_model_id": "Qwen3.8-27B",
        "model_bytes_attested": False,
        "context_tokens": 4096,
        "max_output_tokens": 256,
        "cooperative_deadline_seconds": 30,
        "maximum_model_posts": 3,
    }
    assert receipt["outcome"] == {
        "boots": 2,
        "run_status": "DONE",
        "pattern_id": "bridge_chat",
        "pattern_revision": "2",
        "manifest_exact_match": True,
        "warm_on_both_boots": True,
        "settled_turns": 2,
        "model_posts": 1,
        "model_responses": 1,
        "structured_output_calls": 1,
        "structured_output_name": "final_result",
        "wire_finish_reason": "tool_calls",
        "retained_history_identical": True,
        "model_posts_on_second_boot": 0,
        "active_leases_after_each_boot": 0,
        "model_boundary_violations": 0,
        "lifecycle_calls": 0,
        "substrate_unpublished": True,
    }
    assert receipt["database"] == {
        "image_id": "sha256:1963bc48febf543433baa1ce3edcc6cc08154de722e22495f86681cc9a849026",
        "created_containers": 1,
        "container_removed": True,
        "published_interface": "literal-loopback-only",
        "required_resources": {"mem_limit": "512m", "nano_cpus": 1_000_000_000, "pids_limit": 128},
    }
    assert receipt["runner"] == {
        "cleanup_verified": True,
        "coordinator_cleanup_reserve_seconds": 30,
        "coordinator_deadline_seconds": 240,
        "default_make_ryuk_profile": False,
        "operator_unit_stop_timeout_seconds": 5,
        "outer_timeout_seconds": 250,
        "profile": "local-bridge-runtime-real-model-cached-PG-R2-owned-coordinator-resource-hook",
        "pytest_errors": 0,
        "pytest_failed": 0,
        "pytest_passed": 1,
        "pytest_selected": 1,
        "pytest_skipped": 0,
        "remaining_containers": 0,
        "ryuk_disabled_in_runner_process": True,
        "test_wait_seconds": 210,
        "unresolved_creates": 0,
    }
    assert set(receipt["unexercised"]) == {
        "browser-and-full-accessibility",
        "separate-process-restart",
        "selected-qwen3.8-non-thinking-policy",
        "useful-task-workload",
        "process-crash-recovery",
        "installed-Vessel",
        "Vessel-network-namespace",
        "managed-model-lifecycle",
        "shared-load-and-capacity",
        "live-cancellation",
        "model-byte-identity",
        "general-purpose-tools",
        "durable-semantic-memory",
        "evolution-and-promotion",
        "remote-access-and-hostile-browser",
    }
    history_id = UUID(receipt["retained_history_evidence_id"])
    assert history_id.version == 4
    assert str(history_id) == receipt["retained_history_evidence_id"]
    # The original freshness label describes the dated receipt, never this current test.
    assert receipt["freshness_check"] == "offline-metadata-source-and-selected-dependency-check-only"
    assert not any(
        item in _RECEIPT.read_text()
        for item in (
            "://",
            "/home/",
            "/tmp/",  # noqa: S108 - refuse private host paths in published metadata
            "127.0.0.1",
            "::1",
            "password",
            "username",
            "response_id",
            "session_id",
            "run_id",
            "retained_history_sha256",
            "retained_turns_sha256",
            "application_receipt_sha256",
            "coordinator_receipt_sha256",
            "resource_receipt_sha256",
            "model_profile_sha256",
            "candidate_manifest_sha256",
            "pytest_log_sha256",
        )
    )


def test_historical_local_bridge_receipt_preserves_source_lineage() -> None:
    receipt = read_historical_receipt(_RECEIPT)
    require_historical_source_map(receipt, _SOURCES, "d7d6c42e6f8f1501d05f32839087da327cb4217f16ade9e0345e4982fc62f4de")
    evidence = receipt["code_evidence"]
    assert set(evidence) == {
        "coordinator_source_sha256",
        "joined_test_sha256_before_trial",
        "resource_launcher_source_sha256",
    }
    assert (
        evidence["joined_test_sha256_before_trial"]
        == receipt["source_fingerprints"]["tests/integration/test_local_bridge_runtime.py"]
    )
    assert evidence["coordinator_source_sha256"] == "e81e1540890e24a81e19dfb2e8393abd9c0b078c788bc4b72baa3a73a8435b3f"
    assert (
        evidence["resource_launcher_source_sha256"]
        == "5501216f6134ad20f1f03d95734715f757369d301be8620bf5016da3b1fc0aa5"
    )
    for digest in evidence.values():
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
    evidence_ids = receipt["qualification_evidence_ids"]
    assert set(evidence_ids) == {
        "application_receipt",
        "candidate_manifest",
        "coordinator_receipt",
        "model_profile",
        "pytest_log",
        "resource_receipt",
    }
    all_ids = [*evidence_ids.values(), receipt["retained_history_evidence_id"]]
    assert len(set(all_ids)) == 7
    for identity in all_ids:
        parsed = UUID(identity)
        assert parsed.version == 4
        assert str(parsed) == identity
    require_historical_tcp_coverage(_ROOT, _SOURCES, _SOURCES)


def test_historical_local_bridge_receipt_preserves_observed_dependencies() -> None:
    receipt = read_historical_receipt(_RECEIPT)
    assert receipt["dependency_versions"] == _HISTORICAL_DEPENDENCIES
