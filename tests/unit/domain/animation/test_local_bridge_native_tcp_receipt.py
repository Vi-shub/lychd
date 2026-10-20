"""Check the dated native TCP receipt offline without opening runtime surfaces."""

# The historical metadata module owns the shared bounded source/dependency lists.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import re
import tomllib
from importlib.metadata import version

from tests.support.local_bridge_receipt_freshness import NATIVE_SOURCES, require_current_tcp_coverage
from tests.unit.domain.animation import test_local_bridge_receipt as historical

_ROOT = historical._ROOT
_RECEIPT = _ROOT / "tests/fixtures/animator/local-bridge-vllm-native-tcp.json"
_NATIVE_TEST = "tests/integration/test_local_bridge_native_tcp.py"
_NATIVE_SOURCES = NATIVE_SOURCES
_NATIVE_DEPENDENCIES = (*historical._CORE_DEPENDENCIES, "click", "granian", "python-dotenv")


def test_native_tcp_receipt_preserves_its_observed_scope_and_privacy() -> None:
    text = _RECEIPT.read_text()
    receipt = json.loads(text)
    assert receipt["schema"] == "local-bridge-vllm-native-tcp@1"
    assert receipt["record_kind"] == "sanitized_public_local_bridge_native_tcp_qualification"
    assert receipt["qualification_date"] == "2026-10-20"
    assert receipt["status"] == "passed"
    assert "The optional thinking policy was omitted" in receipt["identity_boundary"]
    assert "Creation investigation was omitted." in receipt["identity_boundary"]
    assert "The separate root independent physical-settlement receipt is required" in receipt["identity_boundary"]
    assert receipt["source_identity"] == {
        "atomic_transitive_closure_attested": False,
        "candidate_manifest_sha256": "64b57438bb2f30d325505bbb1d02c820383c1d691e9c2bc634d1fdd4ee1dea29",
        "clean_checkout_observed": False,
        "deleted_path_count": 22,
        "revision_observed": "3e30a84c4340efca94270f1ab429ca6f5c6341a6",
        "scope": "bounded-critical-source-and-test-fingerprints",
        "source_base_tree": "9f89de06ae91409e727f506058a1fc4bfe14c918",
        "source_path_count": 1570,
    }
    execution = receipt["execution"]
    assert execution["application"] == "production-source-CLI-two-native-TCP-boots"
    assert execution["http"] == "real-loopback-TCP-httpx2-client"
    assert execution["server"] == "Granian-embedded-owned-native-launcher"
    assert execution["lifespan"] == "test-local-wrapper-yields-real-production-lifespan"
    assert execution["runtime_actuator"] == "refusing-fixture-port"
    assert execution["engine_disposal"] == "supplemental-test-cleanup-after-normal-CLI-return"
    assert receipt["authentication"] == {
        "authenticated_TCP_ready_on_both_boots": True,
        "refused_message_admissions": {
            "no_auth": 401,
            "wrong_auth": 401,
            "foreign_origin": 403,
            "missing_csrf": 403,
        },
        "durable_runs_before_admitted_turn": 0,
        "model_posts_before_admitted_turn": 0,
    }
    assert receipt["freshness_check"] == "offline-metadata-source-and-selected-dependency-check-only"
    assert (
        "Updating metadata or hashes alone does not requalify changed critical inputs." in receipt["identity_boundary"]
    )
    for name in ("retained_history_sha256", "retained_turns_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", receipt[name])
    assert not re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", text)
    assert not any(
        forbidden in text
        for forbidden in (
            "://",
            "/home/",
            "/tmp/",  # noqa: S108 - refuse private host material
            "/run/user/",
            "127.0.0.1",
            "::1",
            "password",
            "username",
            '"pid"',
            '"parent_pid"',
            '"group"',
            '"start_time"',
            '"response_id"',
            '"session_id"',
            '"run_id"',
            '"container_id"',
            '"prompt"',
            '"message_history"',
        )
    )


def test_native_tcp_receipt_requires_normal_processes_and_identical_retained_state() -> None:
    receipt = json.loads(_RECEIPT.read_text())
    processes = receipt["processes"]
    assert processes["application_children"] == 2
    assert processes["start_method"] == "spawn"
    assert all(
        processes[name]
        for name in (
            "sequential",
            "distinct_child_identities_checked",
            "parent_identity_checked",
            "start_time_identity_checked",
            "first_child_exited_before_second_start",
        )
    )
    assert processes["exit_codes"] == [0, 0]
    assert processes["forced_termination"] == [False, False]
    for name in ("joined", "reaped", "group_absent", "listener_closed", "CLI_returned", "production_lifespan_closed"):
        assert processes[name] == [True, True]
    assert processes["shutdown"] == "test-owned-SIGTERM-and-normal-production-CLI-return"
    assert processes["authenticated_host_stop_exercised"] is False
    outcome = receipt["outcome"]
    assert outcome["boots"] == 2
    assert outcome["model_posts_by_boot"] == [1, 0]
    assert outcome["model_posts"] == outcome["model_responses"] == outcome["structured_output_calls"] == 1
    assert outcome["external_generated_token_delta"] == 143
    assert outcome["external_prompt_token_delta"] == 511
    assert outcome["external_successful_request_delta"] == 1
    assert outcome["settled_turns"] == 2
    assert outcome["run_status"] == "DONE"
    assert (outcome["pattern_id"], outcome["pattern_revision"]) == ("bridge_chat", "2")
    assert outcome["structured_output_name"] == "final_result"
    assert outcome["wire_finish_reason"] == "tool_calls"
    assert all(
        outcome[name]
        for name in (
            "manifest_exact_match",
            "warm_on_both_boots",
            "retained_history_identical",
            "retained_turns_identical",
            "substrate_unpublished",
            "supplemental_engine_disposal_completed",
        )
    )
    assert all(
        outcome[name] == 0 for name in ("active_leases_after_each_boot", "model_boundary_violations", "lifecycle_calls")
    )
    assert outcome["external_compute_unknown"] is False


def test_native_tcp_receipt_keeps_model_resource_and_cleanup_limits_explicit() -> None:
    receipt = json.loads(_RECEIPT.read_text())
    assert receipt["model"] == {
        "served_model_id": "Qwen3.8-27B",
        "model_bytes_attested": False,
        "context_tokens": 4096,
        "max_output_tokens": 256,
        "cooperative_deadline_seconds": 30,
        "maximum_model_posts": 3,
        "concurrent_runs": 1,
    }
    limits = {
        "AS": [3 * 1024**3] * 2,
        "CPU": [90, 100],
        "FSIZE": [8 * 1024**2] * 2,
        "NOFILE": [256] * 2,
        "CORE": [0, 0],
    }
    assert receipt["native_resources"] == {
        "scope": "per-process-after-interpreter-and-module-startup",
        "observed_limits_by_boot": [limits, limits],
        "aggregate_CPU_RAM_GPU_limit_attested": False,
    }
    database = receipt["database"]
    assert database["created_containers"] == 1
    assert database["container_removed"] is True
    assert database["anonymous_volumes_created"] == database["anonymous_volumes_removed"] == 1
    assert database["published_interface"] == "literal-loopback-only"
    assert database["required_resources"] == {"mem_limit": "512m", "nano_cpus": 1_000_000_000, "pids_limit": 128}
    assert database["effective_cgroup_limits"] == {
        "memory_bytes": 512 * 1024**2,
        "cpu_quota_period_microseconds": [100_000, 100_000],
        "pids": 128,
    }
    assert database["GPU_device_requests"] is False
    assert "GPU_devices" not in database
    assert database["removed_after_two_observed_absent_native_groups"] is True
    runner = receipt["runner"]
    assert runner["pytest_selected"] == runner["pytest_passed"] == 1
    assert receipt["timings_seconds"] == {"pytest": 20.39, "coordinator": 22.991247455007397}
    assert runner["outer_timeout_seconds"] == 250
    assert runner["operator_unit_stop_timeout_seconds"] == 5
    assert runner["coordinator_deadline_seconds"] == 285
    assert runner["relay_runtime_seconds"] == 300
    assert runner["maximum_admitted_model_POST_attempts"] == 1
    assert runner["profile"] == "local-bridge-native-current-C1-cached-PG-one-POST-relay-owned-resource-launcher"
    assert runner["ryuk_disabled_in_runner_process"] is True
    assert runner["default_make_ryuk_profile"] is False
    assert runner["native_test_removal_guard_exercised"] is True
    assert "native_removal_guard_observed" not in runner
    assert runner["cleanup_verified"] is True
    assert runner["warm_model_preserved_by_driver"] is True
    assert runner["root_independent_settlement_verified"] is True
    assert runner["timed_out"] is runner["mutant_applied"] is False
    assert all(
        runner[name] == 0
        for name in (
            "pytest_failed",
            "pytest_errors",
            "pytest_skipped",
            "returncode",
            "remaining_containers",
            "remaining_volumes",
        )
    )
    assert {
        "installed-command-or-caged-Vessel",
        "Vessel-network-namespace",
        "managed-model-lifecycle",
        "process-crash-recovery",
        "selected-qwen3.8-non-thinking-policy",
        "useful-task-workload",
        "real-browser-and-accessibility",
        "browser-SSE-and-reconnect",
        "authenticated-host-stop",
        "autonomous-software-labor",
        "aggregate-native-resource-containment",
    } <= set(receipt["unexercised"])


def test_native_tcp_receipt_matches_qualified_sources_and_provenance() -> None:
    receipt = json.loads(_RECEIPT.read_text())
    assert set(receipt["source_fingerprints"]) == historical._SOURCES | _NATIVE_SOURCES
    assert len(receipt["source_fingerprints"]) == 75
    evidence = receipt["qualification_evidence"]
    assert evidence["native_test_sha256_before_trial"] == receipt["source_fingerprints"][_NATIVE_TEST]
    assert evidence["candidate_manifest_sha256"] == receipt["source_identity"]["candidate_manifest_sha256"]
    assert evidence == {
        "application_receipt_sha256": "3f564d1639fa1a961528e5d4aaf506e88e8b4e7efb34bf6eaed349f2d9928d21",
        "candidate_manifest_sha256": "64b57438bb2f30d325505bbb1d02c820383c1d691e9c2bc634d1fdd4ee1dea29",
        "coordinator_receipt_sha256": "91ef1140949e22fddb1a720828ea99fd08968deadac746aa42647b3486817565",
        "coordinator_source_sha256": "39161ed25ab36eb9e342b0fd8377cf0ae8c03333d2d81d439a14798fe96756b8",
        "driver_result_receipt_sha256": "68f83a8997fb50abf00a73a6ac9fbc93dfed9b48ba9e05daa927732752d294a0",
        "driver_source_sha256": "57fcb708085d133cea915d4cf333136afb1add929a46730efe743be159ce447f",
        "input_binding_sha256": "b65b730d94a38594f31da75e2791859346717441fa76bc2a62307d0ee66075ba",
        "model_profile_sha256": "1dce7be8e55ec67a3b72be7dcd360f9d19d37fc49551987f3e7e2d411ed58878",
        "native_test_sha256_before_trial": receipt["source_fingerprints"][_NATIVE_TEST],
        "one_POST_relay_terminal_sha256": "270a066836073ba577f3671a84eb94eaf3f65959aaac4696e879ee4d117f82e7",
        "outer_receipt_sha256": "fcf027c11997405a3c88281ab38d5cc8c23da401b6a70f70716d1fe329f0228a",
        "physical_inventory_sha256": "671c57a6333e2b6830467b3beda90b1f49d60b5b88c898a61f214a2fcfeae8e4",
        "pytest_log_sha256": "079c148c2705949946354d5174c53ac09d3397d8b73eb3e402eafac6ae79bebc",
        "resource_launcher_source_sha256": "a547a5e4057e5ec3e09ffe18f741ac8327c3d394d371c06f61ab3f38064b2c65",
        "resource_receipt_sha256": "049effd951742fde8d8d12e102aeca1b496ff5f4a7a6c30766be692f41eb322c",
        "root_PG_live_sha256": "4644f583a104ed803e14ce623e788b7b0c6f27ccf1480dc0b3bec58c9cab7d3a",
        "root_independent_acceptance_sha256": "b16abf4a1e3c0252d9e502e5f189bcc5ed4a9647d6b658c366e20180be244f6c",
        "root_independent_settlement_sha256": "507f9124f9b48c7bcd5b0e23d8ffb37fcd6bf653bf2b5c21d48088b2cb8ad20b",
        "sdk_manifest_sha256": "ba5c38a75127bfdd30756da83202dce8518165fd4c3e57aefb5309e31c746370",
        "source_manifest_sha256": "27c3951856be693f154243403861f576c2830c85cb29cb2267dd8cc5840118ae",
    }
    assert evidence["coordinator_source_sha256"] == "39161ed25ab36eb9e342b0fd8377cf0ae8c03333d2d81d439a14798fe96756b8"
    assert (
        evidence["resource_launcher_source_sha256"]
        == "a547a5e4057e5ec3e09ffe18f741ac8327c3d394d371c06f61ab3f38064b2c65"
    )
    for digest in evidence.values():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    require_current_tcp_coverage(_ROOT, historical._SOURCES, historical._SOURCES | _NATIVE_SOURCES)


def test_native_tcp_receipt_matches_selected_dependencies() -> None:
    receipt = json.loads(_RECEIPT.read_text())
    dependencies = {name: version(name) for name in _NATIVE_DEPENDENCIES}
    # Optional SDKs use exact lock entries in ordinary offline CI.
    locked = tomllib.loads((_ROOT / "uv.lock").read_text())
    dependencies.update(
        {item["name"]: item["version"] for item in locked["package"] if item["name"] in {"docker", "testcontainers"}}
    )
    assert receipt["dependency_versions"] == dependencies
