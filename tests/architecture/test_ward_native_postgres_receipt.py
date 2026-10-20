"""Offline architecture evidence/freshness checks; never rerun the PG qualification."""

from __future__ import annotations

import copy
import json
import re
import tomllib
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_RECEIPT = _ROOT / "tests/fixtures/ward/ward-native-postgres-2026-10-20.json"
_RECEIPT_SHA256 = "3b9e674973e9937554219756837e6189dcad3aebd210dd2e9ae6dfa51c8540e7"
# Exact logical key topology is frozen beside the complete receipt digest.
_KEYS = {
    "root": [
        "checks",
        "dependency_boundary",
        "dependency_versions",
        "execution",
        "fault_model_boundary",
        "fixture_boundaries",
        "freshness_check",
        "historical_ward_receipt_sha256",
        "observed_on",
        "profile",
        "python_version",
        "qualification_evidence",
        "record_kind",
        "resources",
        "schema",
        "source_fingerprints",
        "source_identity",
        "status",
        "terminal",
        "unproved",
    ],
    "root.source_identity": [
        "actual_qualification_source_paths",
        "atomic_transitive_closure_attested",
        "base_revision_declared",
        "clean_checkout_attested",
        "maintained_scope",
        "older_ward_runs_requalified_by_hash_refresh",
        "qualified_source_map_sha256",
    ],
    "root.qualification_evidence": [
        "admission_sha256",
        "coordinator_result_sha256",
        "coordinator_source_sha256",
        "pytest_log_sha256",
        "resource_launcher_source_sha256",
        "resource_receipt_sha256",
        "root_retention_sha256",
    ],
    "root.execution": [
        "application",
        "application_registration",
        "failed_tests",
        "foreign_constructor_executed",
        "image_pulls",
        "installed_cli",
        "model_posts",
        "native_authority_tests",
        "passed_tests",
        "postgres_image",
        "postgres_objects",
        "production_activation",
        "pytest_elapsed_seconds",
        "pytest_exit_code",
        "skipped_tests",
        "test_targets",
        "tests",
        "ward_tests",
    ],
    "root.checks": ["native_authority", "ward"],
    "root.checks.ward": [
        "all_persisted_audit_columns_secret_negative_checks",
        "caller_isolation_preserved",
        "credential_exchange_once_and_concurrent_refusal",
        "credential_session_permission_expiry",
        "credential_session_principal_revocation",
        "default_deny_and_exact_principal_action_object_audience_purpose",
        "empty_0020_to_0019_and_reupgrade",
        "explicit_bootstrap_runtime_grant_and_actual_runtime_dml",
        "full_alembic_0019_to_0020",
        "nonempty_admin_downgrade_refusal_sqlstate",
        "prepare_time_revocation_and_post_audit_recheck",
        "retained_unknown_attempt_and_callback_fault_models",
        "runtime_schema_and_version_permission_denial_sqlstate",
        "stale_policy_generation_after_false_then_true_regrant",
        "unsupported_repeatable_read_serializable_autocommit_refused",
        "user_human_and_session_credential_principal_constraints",
        "version_tables_and_retained_rows_preserved_after_refusal",
    ],
    "root.checks.native_authority": [
        "actual_Principal_lock_wait_observes_committed_revocation",
        "current_authorized_exact_replay_and_legacy_refusal",
        "database_clock_expiry_after_real_stage_prevents_publication",
        "exact_retained_candidate_bytes_and_plan",
        "independent_attributed_assessment_and_hold_reject",
        "lost_decision_commit_ack_exact_retained_identity",
        "mapper_table_and_prebound_connection_escape_refused_before_effect",
        "shutdown_serializes_with_inflight_operation",
    ],
    "root.fixture_boundaries": [
        "actual_ward_runtime_DML_and_DDL_denials",
        "genuine_application_or_database_process_restart",
        "native_authority_full_migration_receipt",
        "native_authority_schema_setup",
        "service_reopen_only",
        "shared_filesystem_database_atomicity",
        "ward_schema_setup",
    ],
    "root.fault_model_boundary": [
        "callback_after_handoff_error",
        "callback_before_handoff_error",
        "eligible_publication_outcome",
        "exactly_once_delivery_proved",
        "native_decision_acknowledgement_error",
        "physically_broken_connection_tested",
        "post_commit_acknowledgement_error",
    ],
    "root.resources": [
        "aggregate_native_limits_attested",
        "cleanup_reserve_seconds",
        "coordinator_wait_seconds",
        "declared_launcher_rlimits",
        "disk_quota_attested",
        "hostile_containment_attested",
        "postgres_each",
        "postgres_objects",
        "unit_wall_seconds",
    ],
    "root.resources.declared_launcher_rlimits": [
        "address_space_bytes",
        "core_bytes",
        "cpu_hard_seconds",
        "cpu_soft_seconds",
        "file_size_bytes",
        "open_files",
    ],
    "root.resources.postgres_each": [
        "cpu_period_us",
        "cpu_quota_us",
        "gpu_requests",
        "kernel_readback_before_readiness",
        "memory_bytes",
        "pids_max",
        "publication",
        "swap_bytes",
    ],
    "root.terminal": [
        "anonymous_volume_sets_removed",
        "cleanup_errors",
        "credential_contents_read",
        "listeners_closed",
        "model_idle_and_counters_unchanged",
        "owned_objects_absent",
        "postgres_containers_removed",
        "root_anonymous_volumes_absent",
        "root_independent_retention_verified",
        "root_model_posts",
        "root_recorded_cgroups_absent",
        "root_recorded_processes_absent",
        "source_unchanged",
        "unit_absent",
        "warm_identity_and_protected_metadata_preserved",
    ],
    "root.dependency_versions": [
        "advanced-alchemy",
        "alembic",
        "asyncpg",
        "docker",
        "litestar",
        "litestar-saq",
        "pgvector",
        "psycopg",
        "psycopg-binary",
        "pydantic",
        "pytest",
        "saq",
        "sqlalchemy",
        "testcontainers",
    ],
    "root.source_fingerprints": [
        ".python-version",
        "docs/adr/06-persistence.md",
        "docs/adr/12-configuration.md",
        "docs/adr/18-evolution.md",
        "docs/adr/38-iam.md",
        "pyproject.toml",
        "src/lychd/compositions/creation/native_candidate.py",
        "src/lychd/compositions/creation/schemas.py",
        "src/lychd/config/constants.py",
        "src/lychd/config/settings/root.py",
        "src/lychd/config/settings/section.py",
        "src/lychd/config/settings/server.py",
        "src/lychd/config/settings/sources.py",
        "src/lychd/db/authority.py",
        "src/lychd/db/bootstrap.py",
        "src/lychd/db/creation.py",
        "src/lychd/db/engine.py",
        "src/lychd/db/factory.py",
        "src/lychd/db/migrations/alembic.ini",
        "src/lychd/db/migrations/env.py",
        "src/lychd/db/migrations/script.py.mako",
        "src/lychd/db/migrations/versions/0001_phylactery_first_light.py",
        "src/lychd/db/migrations/versions/0002_pin_pattern_manifest.py",
        "src/lychd/db/migrations/versions/0003_delegated_agent_ledger.py",
        "src/lychd/db/migrations/versions/0004_run_delivery_outbox.py",
        "src/lychd/db/migrations/versions/0005_preauth_priority.py",
        "src/lychd/db/migrations/versions/0006_preauth_source_presence.py",
        "src/lychd/db/migrations/versions/0007_nexus_swap_admission.py",
        "src/lychd/db/migrations/versions/0008_wait_owner_evidence.py",
        "src/lychd/db/migrations/versions/0009_atlas_projects.py",
        "src/lychd/db/migrations/versions/0010_native_consent_binding.py",
        "src/lychd/db/migrations/versions/0011_blockworld_placement.py",
        "src/lychd/db/migrations/versions/0012_archive_public_notes.py",
        "src/lychd/db/migrations/versions/0013_archive_derivations.py",
        "src/lychd/db/migrations/versions/0015_delegated_host_disposition.py",
        "src/lychd/db/migrations/versions/0016_creation_investigation.py",
        "src/lychd/db/migrations/versions/0017_creation_feedback.py",
        "src/lychd/db/migrations/versions/0018_creation_successor.py",
        "src/lychd/db/migrations/versions/2026-10-13_scout_attempts_0014.py",
        "src/lychd/db/migrations/versions/2026-10-18_scavenger_observation_0019.py",
        "src/lychd/db/migrations/versions/2026-10-20_ward_local_candidate_0020.py",
        "src/lychd/db/models/__init__.py",
        "src/lychd/db/models/creation.py",
        "src/lychd/db/models/session.py",
        "src/lychd/db/models/ward.py",
        "src/lychd/db/sessions.py",
        "src/lychd/db/ward.py",
        "src/lychd/domain/artifact_custody.py",
        "src/lychd/domain/artifacts.py",
        "src/lychd/domain/evolution/native_candidate.py",
        "src/lychd/domain/ward/__init__.py",
        "src/lychd/domain/ward/service.py",
        "src/lychd/system/services/artifact_custody.py",
        "src/lychd/system/services/native_candidate.py",
        "src/lychd/system/templates/pg_hba_v1.conf.jinja",
        "tests/conftest.py",
        "tests/integration/test_archive_migrations_pg.py",
        "tests/integration/test_database_authority_pg.py",
        "tests/integration/test_native_candidate_authority_pg.py",
        "tests/integration/test_ward_pg.py",
        "tests/unit/domain/test_artifact_custody.py",
        "tests/unit/domain/ward/test_ward.py",
        "tests/unit/system/services/test_artifact_custody.py",
        "tests/unit/system/services/test_native_candidate_authority.py",
        "tests/unit/system/services/test_native_candidate_custody.py",
        "uv.lock",
    ],
    "root.unproved": [
        "aggregate_native_or_hostile_containment",
        "exactly_once_delivery",
        "foreign_constructor_containment",
        "general_application_registration",
        "general_owned_content_integration",
        "genuine_application_or_database_process_restart",
        "installed_body_or_CLI",
        "native_fixture_full_migration_chain",
        "physically_broken_database_connection",
        "positive_OwnerAdmission_or_promotion",
        "production_activation",
        "remote_IAM_or_device_enrollment",
        "repository_source_writes",
        "shared_filesystem_database_transaction",
    ],
}
_SOURCES = frozenset(_KEYS["root.source_fingerprints"])
_DEPENDENCIES = frozenset(_KEYS["root.dependency_versions"])
_MIGRATIONS = frozenset(path for path in _SOURCES if path.startswith("src/lychd/db/migrations/versions/"))


def _shape(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        mapping = cast("dict[str, Any]", value)
        assert set(mapping) == set(_KEYS[path]), path
        for key, item in mapping.items():
            _shape(key, "key")
            _shape(item, path + "." + key)
    elif isinstance(value, list):
        for item in cast("list[Any]", value):
            _shape(item, path + "[]")
    elif isinstance(value, str):
        assert not value.startswith(("/", "~/"))
        assert not any(
            part in value
            for part in ("://", "\\", "/home/", "/tmp/", "/run/", "127.0.0.1", "::1")  # noqa: S108 - rejecting private paths
        )
        assert not re.search(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", value)
    else:
        assert type(value) in (bool, int, float, type(None))


def _claims(receipt: dict[str, Any]) -> None:
    _shape(receipt)
    assert receipt["schema"] == "ward-native-postgres-qualification@1"
    assert receipt["profile"] == "host-opaque-candidate@1"
    assert receipt["status"] == "passed"
    assert receipt["observed_on"] == "2026-10-20"
    identity = receipt["source_identity"]
    assert identity["base_revision_declared"] == "d260ca552fad5c5d9503ea9efc092959dfc57968"
    assert identity["actual_qualification_source_paths"] == 1546
    assert identity["qualified_source_map_sha256"] == (
        "3a567cec71eba891114c554a9c981b661990a086c6f7a51f29e88fdbca04fce2"
    )
    assert identity["clean_checkout_attested"] is False
    assert identity["atomic_transitive_closure_attested"] is False
    assert identity["older_ward_runs_requalified_by_hash_refresh"] is False
    assert len(_SOURCES) == 66
    assert len(_MIGRATIONS) == 20
    execution = receipt["execution"]
    assert execution["test_targets"] == [
        "tests/integration/test_ward_pg.py",
        "tests/integration/test_native_candidate_authority_pg.py",
    ]
    assert (execution["tests"], execution["ward_tests"], execution["native_authority_tests"]) == (23, 18, 5)
    assert execution["passed_tests"] == 23
    assert execution["failed_tests"] == execution["skipped_tests"] == execution["pytest_exit_code"] == 0
    assert execution["postgres_objects"] == 3
    assert execution["model_posts"] == execution["image_pulls"] == 0
    for field in ("installed_cli", "application_registration", "foreign_constructor_executed", "production_activation"):
        assert execution[field] is False
    boundary = receipt["fixture_boundaries"]
    assert boundary["ward_schema_setup"] == "real-bootstrap-HBA-and-full-Alembic-chain-with-0019-to-0020-cycle"
    assert boundary["native_authority_schema_setup"] == (
        "isolated-SQLAlchemy-metadata-drop-all-and-create-all-for-Ward-and-Creation-tables"
    )
    assert boundary["native_authority_full_migration_receipt"] is False
    assert boundary["actual_ward_runtime_DML_and_DDL_denials"] is True
    assert boundary["genuine_application_or_database_process_restart"] is False
    assert boundary["service_reopen_only"] is True
    assert boundary["shared_filesystem_database_atomicity"] is False
    checks = receipt["checks"]
    assert checks["ward"]["runtime_schema_and_version_permission_denial_sqlstate"] == "42501"
    assert checks["ward"]["nonempty_admin_downgrade_refusal_sqlstate"] == "P0001"
    assert all(value is True for key, value in checks["ward"].items() if not key.endswith("sqlstate"))
    assert all(value is True for value in checks["native_authority"].values())
    assert all(value is True for value in receipt["unproved"].values())
    fault = receipt["fault_model_boundary"]
    assert fault["native_decision_acknowledgement_error"] == "injected-after-real-database-commit"
    assert fault["physically_broken_connection_tested"] is False
    assert fault["exactly_once_delivery_proved"] is False
    assert fault["eligible_publication_outcome"] == "unknown"
    _resource_claims(receipt)
    for digest in receipt["qualification_evidence"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert receipt["historical_ward_receipt_sha256"] == (
        "83256113ad9b6ecde094a82068c64b6faea711b4fdeac213d773ba42bb3a7716"
    )


def _resource_claims(receipt: dict[str, Any]) -> None:
    resources = receipt["resources"]
    assert resources["postgres_objects"] == 3
    assert resources["postgres_each"] == {
        "memory_bytes": 536870912,
        "swap_bytes": 0,
        "cpu_quota_us": 100000,
        "cpu_period_us": 100000,
        "pids_max": 128,
        "kernel_readback_before_readiness": True,
        "publication": "literal-loopback-only",
        "gpu_requests": False,
    }
    for field in ("aggregate_native_limits_attested", "disk_quota_attested", "hostile_containment_attested"):
        assert resources[field] is False
    terminal = receipt["terminal"]
    for field in (
        "postgres_containers_removed",
        "anonymous_volume_sets_removed",
        "listeners_closed",
        "root_recorded_processes_absent",
        "root_recorded_cgroups_absent",
        "root_anonymous_volumes_absent",
    ):
        assert terminal[field] == 3
    for field in (
        "unit_absent",
        "owned_objects_absent",
        "source_unchanged",
        "root_independent_retention_verified",
        "warm_identity_and_protected_metadata_preserved",
        "model_idle_and_counters_unchanged",
    ):
        assert terminal[field] is True
    assert terminal["cleanup_errors"] == terminal["root_model_posts"] == 0
    assert terminal["credential_contents_read"] is False


def _load() -> dict[str, Any]:
    raw = _RECEIPT.read_bytes()
    assert sha256(raw).hexdigest() == _RECEIPT_SHA256
    receipt = json.loads(raw)
    _claims(receipt)
    return receipt


def test_combined_ward_native_receipt_scope_privacy_and_exact_evidence() -> None:
    _load()


def test_combined_receipt_current_source_migrations_and_selected_dependency_freshness() -> None:
    receipt = _load()
    for path, digest in receipt["source_fingerprints"].items():
        assert sha256((_ROOT / path).read_bytes()).hexdigest() == digest, path
    assert {
        str(path.relative_to(_ROOT)) for path in (_ROOT / "src/lychd/db/migrations/versions").glob("*.py")
    } == _MIGRATIONS
    lock = tomllib.loads((_ROOT / "uv.lock").read_text())
    assert receipt["dependency_versions"] == {
        package["name"]: package["version"] for package in lock["package"] if package["name"] in _DEPENDENCIES
    }
    # Installed metadata is a current offline freshness check, not a new trial SDK attestation.
    for name, expected in receipt["dependency_versions"].items():
        try:
            installed = version(name)
        except PackageNotFoundError:
            assert name in {"docker", "testcontainers"}
        else:
            assert installed == expected, name
    assert receipt["python_version"] == (_ROOT / ".python-version").read_text().strip()
    assert receipt["freshness_check"] == (
        "offline-current-critical-source-and-selected-lock-check-only-not-a-runtime-rerun"
    )


@pytest.mark.parametrize(
    "mutation",
    ["private-url", "unknown-field", "missing-source", "migration-overclaim", "activation", "old-run-refresh"],
)
def test_scope_and_privacy_changes_require_new_evidence(mutation: str) -> None:
    receipt = copy.deepcopy(_load())
    if mutation == "private-url":
        receipt["execution"]["application"] = "https://private.invalid/value"
    elif mutation == "unknown-field":
        receipt["raw_payload"] = "not-public"
    elif mutation == "missing-source":
        del receipt["source_fingerprints"]["src/lychd/system/services/native_candidate.py"]
    elif mutation == "migration-overclaim":
        receipt["fixture_boundaries"]["native_authority_full_migration_receipt"] = True
    elif mutation == "activation":
        receipt["execution"]["production_activation"] = True
    else:
        receipt["source_identity"]["older_ward_runs_requalified_by_hash_refresh"] = True
    with pytest.raises(AssertionError):
        _claims(receipt)
