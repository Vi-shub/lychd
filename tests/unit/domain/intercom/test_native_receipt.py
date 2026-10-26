"""Keep native peer observations historical without qualifying merged source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from tests.unit.domain.intercom.test_receipt import (
    _HISTORICAL_DEPENDENCIES,
    _require_current_qualification,
    _source_pins,
)

_ROOT = Path(__file__).resolve().parents[4]
_RECEIPT = _ROOT / "tests/fixtures/intercom/native-public-source.json"


def _receipt() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(_RECEIPT.read_text()))


def test_historical_native_qualification_keeps_original_bytes_and_inventory() -> None:
    assert hashlib.sha256(_RECEIPT.read_bytes()).hexdigest() == (
        "56b06f47496c2475dd09045555f6bdd02b6b145050ab2c0b84605c76dd9a15a3"
    )
    receipt = _receipt()
    assert receipt["schema_version"] == 1
    assert receipt["profile"] == "native-public-peer@1"
    assert receipt["qualification"] == "native-public-peer-2026-10-22"
    assert receipt["base_revision_declared"] == "0e0dc859699edf279aecc17d0eff0a9ac97945ed"
    assert receipt["clean_checkout_attested"] is False
    pins = _source_pins(receipt)
    assert len(pins) == 60
    assert {
        "scripts/intercom_native_lab.py",
        "src/lychd/agents/workflows/native_public_peer.py",
        "src/lychd/domain/cortex/graph_runner.py",
        "src/lychd/domain/cortex/ledger.py",
        "src/lychd/domain/intercom/native.py",
        "src/lychd/system/services/peer_runs.py",
        "src/lychd/db/migrations/versions/0017_native_peer_graph.py",
        "tests/integration/test_native_peer_migrations_pg.py",
        "tests/unit/cli/test_intercom_native_lab.py",
    } <= pins.keys()
    assert receipt["production_dependencies"] == _HISTORICAL_DEPENDENCIES


def test_historical_native_peer_observation_cannot_qualify_current_sources() -> None:
    with pytest.raises(AssertionError, match="Source inventory is stale"):
        _require_current_qualification(_receipt())


def test_native_evidence_preserves_observed_counts_and_explicit_limits() -> None:
    receipt = _receipt()
    observed = receipt["observed"]
    assert observed["native_sdk_process_cases"] == 6
    assert observed["affected_postgresql_passed"] == 228
    assert observed["unadmitted_scout_composed_profile_skipped"] == 1
    assert observed["native_migration_cases_included"] == 11
    assert observed["operator_dispatcher_cases"] == 6
    assert observed["standalone_sdk_cases"] == 4
    assert observed["remaining_confirmed_review_p1_p2"] == 0
    assert all(
        observed[name] is True
        for name in (
            "checkpoint_before_contact",
            "same_task_fresh_authentication_after_restart",
            "post_claim_pre_wire_process_death_unknown_without_resubmit",
            "credential_rotation_during_actual_run_lock_wait_refuses_resume",
            "one_correlated_terminal_one_successor",
            "revoked_peer_local_cancellation",
            "blocked_tls_read_locally_retired_with_disconnect_witness",
            "remote_uncertainty_retained",
            "zero_accepted_bytes_is_not_zero_execution_proof",
            "known_failure_does_not_end_operator_dispatcher",
            "completed_result_requires_current_separate_read",
            "fixture_servers_closed",
            "fixture_private_keys_removed",
            "owned_test_containers_removed",
        )
    )
    assert len(receipt["limits"]) == 10
    assert all(value is False for value in receipt["limits"].values())
    evidence = cast("dict[str, str]", receipt["evidence_ids"])
    assert set(evidence) == {"native_process", "postgresql", "review", "operator_dispatcher", "standalone_transport"}
    assert len(set(evidence.values())) == 5
    for identity in evidence.values():
        parsed = UUID(identity)
        assert parsed.version == 4
        assert str(parsed) == identity
    assert receipt["independent_peer"]["sdk"] == "a2a-sdk==1.2.3"
    assert not any(
        item in _RECEIPT.read_text()
        for item in ("://", "/home/", "/tmp/", "127.0.0.1", '"run_id"', '"session_id"', '"prompt"', '"password"')  # noqa: S108 - refuse private paths
    )
