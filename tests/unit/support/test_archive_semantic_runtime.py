"""Inert admission boundaries and freshness of the actual native observation."""

# ruff: noqa: INP001 - focused tests use the existing support namespace

import json
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.support import archive_semantic_runtime as proof
from tests.support.archive_semantic_runtime import _admit_root, _sources  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def scratch() -> Iterator[Path]:
    with TemporaryDirectory(prefix="lychd-admit-", dir="/tmp") as directory:
        yield Path(directory)


def test_default_gate_precedes_fixture_and_tool_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LYCHD_REQUIRE_ARCHIVE_SEMANTIC_NATIVE_PROOF", raising=False)
    monkeypatch.setattr(proof.native, "qualification", lambda: pytest.fail("qualification before opt-in"))
    monkeypatch.setattr(proof.native, "required_tools", lambda: pytest.fail("tools before opt-in"))
    with pytest.raises(pytest.skip.Exception):
        proof.run_native_semantic_proof(tmp_path / "unused", monkeypatch, None)
    assert not (tmp_path / "unused").exists()


def test_missing_fixture_refuses_before_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LYCHD_REQUIRE_ARCHIVE_SEMANTIC_NATIVE_PROOF", "1")
    monkeypatch.delenv("LYCHD_EMBEDDING_FIXTURE", raising=False)
    monkeypatch.setattr(proof.native, "required_tools", lambda: pytest.fail("tools before fixture admission"))
    with pytest.raises(AssertionError, match="LYCHD_EMBEDDING_FIXTURE"):
        proof.run_native_semantic_proof(tmp_path / "unused", monkeypatch, None)
    assert not (tmp_path / "unused").exists()


def test_short_fresh_private_root_is_only_admitted_not_created(scratch: Path) -> None:
    root = scratch / "new"
    assert _admit_root(root) == root / "pg-socket"
    assert not root.exists()


def test_relative_and_aliased_roots_refuse(scratch: Path) -> None:
    with pytest.raises(AssertionError, match="absolute_runtime_root"):
        _admit_root(Path("relative"))
    with pytest.raises(AssertionError, match="unaliased_runtime_root"):
        _admit_root(scratch / ".." / "alias")


def test_reused_root_refuses(scratch: Path) -> None:
    with pytest.raises(AssertionError, match="fresh_runtime_root"):
        _admit_root(scratch)


def test_shared_ancestor_refuses(scratch: Path) -> None:
    scratch.chmod(0o777)
    try:
        with pytest.raises(AssertionError, match="private_runtime_root_ancestor"):
            _admit_root(scratch / "new")
    finally:
        scratch.chmod(0o700)


def test_overlong_postgres_socket_refuses_before_effects(scratch: Path) -> None:
    root = scratch / ("x" * 100)
    with pytest.raises(AssertionError, match="PG_AF_UNIX_path_length"):
        _admit_root(root)
    assert not root.exists()


def test_maintained_native_receipt_binds_current_critical_source() -> None:
    root = Path(__file__).resolve().parents[3]
    receipt = json.loads((root / "tests/fixtures/archive/native-semantic-runtime-2026-10-19.json").read_text())
    assert receipt["record_kind"] == "sanitized_public_maintained_native_archive_semantic_qualification"
    assert receipt["status"] == receipt["cleanup"] == "passed"
    assert receipt["source_sha256"] == _sources()
    assert receipt["artifact_unchanged_after_cleanup"] is True
    assert receipt["proof"]["operations"] == 8
    assert receipt["proof"]["query_operations"] == 5
    assert receipt["proof"]["derivation_operations"] == 3
    assert receipt["proof"]["embedding_POSTs"] == receipt["proof"]["readiness_GETs"] == 16
    assert 0 < receipt["proof"]["native_input_tokens"] <= receipt["limits"]["native_input_tokens"]
    assert receipt["proof"]["leases_remaining"] == 0
    assert receipt["proof"]["all_observed_clients_closed"] is True
    assert receipt["independent_terminal_audit"]["physical_settled"] is True
    assert receipt["independent_terminal_audit"]["workflow_passed"] is True


def test_maintained_native_answer_receipt_binds_current_critical_source() -> None:
    root = Path(__file__).resolve().parents[3]
    receipt = json.loads((root / "tests/fixtures/archive/native-semantic-answer-runtime-2026-10-19.json").read_text())
    assert receipt["record_kind"] == "sanitized_public_maintained_native_semantic_answer_qualification"
    assert receipt["status"] == receipt["cleanup"] == "passed"
    assert receipt["source_sha256"] == _sources(proof.ANSWER_PROFILE)
    assert receipt["qualification"] == proof.ANSWER_PROFILE
    assert receipt["case_profile"] == "generated-answer-and-empty-recall-v2"
    assert receipt["outer_limits"]["chat_POSTs"] == 2
    assert receipt["active_limits"]["chat_POSTs"] == receipt["proof"]["chat_POSTs"] == 1
    assert receipt["proof"]["operations"] == 4
    assert receipt["proof"]["embedding_POSTs"] == 8
    assert 0 < receipt["proof"]["native_input_tokens"] <= receipt["active_limits"]["native_input_tokens"]
    assert receipt["proof"]["leases_remaining"] == 0
    assert receipt["proof"]["all_observed_clients_closed"] is True
    cases = receipt["cases"]
    assert cases["A"]["response"]["finish_reason"] == cases["A"]["response"]["provider_finish_reason"] == "stop"
    assert cases["B"]["typed_refusal"]["code"] == "source_integrity"
    for case in (cases["B"], cases["C"]):
        assert case["chat_attempts"] == case["chat_grants"] == case["chat_POSTs"] == 0
        assert case["context_assembly_attempts"] == 0
    assert cases["C"]["deterministic_empty_recall"] is True
    assert cases["C"]["new_SDK_messages"] == cases["C"]["replay_new_compute"] == 0
    assert cases["C"]["normal_terminal_release_recorded"] is True
    assert receipt["artifact_unchanged_after_cleanup"] is True
    assert receipt["independent_terminal_audit"]["physical_settled"] is True
    assert receipt["independent_terminal_audit"]["workflow_passed"] is True
    assert receipt["independent_visible_review"]["verdict"] == "passed_fixed_cases_and_recorded_operations"
