"""Current inert admission boundaries and sealed pre-removal Archive observations."""

# ruff: noqa: INP001 - focused tests use the existing support namespace

import json
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.support import archive_semantic_runtime as proof
from tests.support.archive_semantic_runtime import _admit_root  # pyright: ignore[reportPrivateUsage]
from tests.support.local_bridge_receipt_freshness import HISTORICAL_RETIREMENT


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


def test_historical_native_receipt_preserves_dated_source_lineage() -> None:
    root = Path(__file__).resolve().parents[3]
    raw = (root / "tests/fixtures/archive/native-semantic-runtime-2026-10-19.json").read_bytes()
    assert sha256(raw).hexdigest() == "1959f5ce81490ea0401b03661b4fc90407c66ae2e574267b05ef0db38acd42c5"
    receipt = json.loads(raw)
    assert receipt["observed_on"] == "2026-10-20"
    assert HISTORICAL_RETIREMENT["no_current_qualification"] is True
    assert set(receipt["source_sha256"]) == {
        "domain/animation/embeddings.py",
        "domain/animation/services/registry.py",
        "domain/cortex/dispatcher.py",
        "domain/cortex/leases.py",
        "extensions/builtin/animator/llamacpp/connector.py",
        "extensions/builtin/animator/llamacpp/embedding.py",
        "extensions/builtin/animator/llamacpp/embedding_profile.py",
        "extensions/builtin/animator/runtimes/llamacpp.py",
        "extensions/builtin/animator/soulstones/llamacpp.py",
        "pyproject.toml",
        "src/lychd/config/settings/__init__.py",
        "src/lychd/config/settings/archive.py",
        "src/lychd/config/settings/root.py",
        "src/lychd/config/settings/server.py",
        "src/lychd/db/archive.py",
        "src/lychd/db/archive_derivation.py",
        "src/lychd/db/archive_semantic.py",
        "src/lychd/db/models/archive.py",
        "src/lychd/db/models/archive_derivation.py",
        "src/lychd/domain/archive/contracts.py",
        "src/lychd/domain/archive/derivation.py",
        "src/lychd/domain/archive/derivation_service.py",
        "src/lychd/domain/archive/semantic.py",
        "src/lychd/domain/archive/semantic_service.py",
        "src/lychd/domain/codex/guards.py",
        "src/lychd/domain/codex/local_access.py",
        "src/lychd/domain/codex/middleware.py",
        "src/lychd/domain/codex/sigil.py",
        "src/lychd/interface/web/archive.py",
        "src/lychd/interface/web/archive_access.py",
        "src/lychd/interface/web/archive_derivation.py",
        "src/lychd/interface/web/archive_derivation_factory.py",
        "src/lychd/interface/web/archive_generation_factory.py",
        "src/lychd/interface/web/archive_response.py",
        "src/lychd/interface/web/archive_semantic.py",
        "src/lychd/interface/web/archive_semantic_factory.py",
        "src/lychd/interface/web/openapi.py",
        "tests/conftest.py",
        "tests/integration/test_archive_semantic_native_runtime.py",
        "tests/scout_postgres_fixture.py",
        "tests/support/archive_semantic_runtime.py",
        "tests/support/embedding_runtime.py",
        "uv.lock",
    }
    canonical = json.dumps(receipt["source_sha256"], sort_keys=True, separators=(",", ":")).encode()
    assert sha256(canonical).hexdigest() == "23b8faf49fd05497640489903c3b08e996961cf891e437f409c72e50acad5ad3"
    assert receipt["record_kind"] == "sanitized_public_maintained_native_archive_semantic_qualification"
    assert receipt["status"] == receipt["cleanup"] == "passed"
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


def test_historical_native_answer_receipt_preserves_dated_source_lineage() -> None:
    root = Path(__file__).resolve().parents[3]
    raw = (root / "tests/fixtures/archive/native-semantic-answer-runtime-2026-10-19.json").read_bytes()
    assert sha256(raw).hexdigest() == "f443bbe7ecaa8034fd43c24edb7bd048f2e5a1d46ad4f54d7e0cbd6fc7a16aa6"
    receipt = json.loads(raw)
    assert receipt["observed_on"] == "2026-10-20"
    assert HISTORICAL_RETIREMENT["no_current_qualification"] is True
    assert set(receipt["source_sha256"]) == {
        "domain/animation/embeddings.py",
        "domain/animation/services/registry.py",
        "domain/cortex/dispatcher.py",
        "domain/cortex/leases.py",
        "extensions/builtin/animator/llamacpp/connector.py",
        "extensions/builtin/animator/llamacpp/embedding.py",
        "extensions/builtin/animator/llamacpp/embedding_profile.py",
        "extensions/builtin/animator/runtimes/llamacpp.py",
        "extensions/builtin/animator/soulstones/llamacpp.py",
        "pyproject.toml",
        "scripts/local_model_probe.py",
        "src/lychd/agents/archive_context.py",
        "src/lychd/agents/context_limits.py",
        "src/lychd/agents/workflows/semantic_chat.py",
        "src/lychd/app.py",
        "src/lychd/config/settings/__init__.py",
        "src/lychd/config/settings/archive.py",
        "src/lychd/config/settings/root.py",
        "src/lychd/config/settings/server.py",
        "src/lychd/db/archive.py",
        "src/lychd/db/archive_derivation.py",
        "src/lychd/db/archive_semantic.py",
        "src/lychd/db/bootstrap.py",
        "src/lychd/db/checkpoints.py",
        "src/lychd/db/engine.py",
        "src/lychd/db/factory.py",
        "src/lychd/db/models/archive.py",
        "src/lychd/db/models/archive_derivation.py",
        "src/lychd/db/sessions.py",
        "src/lychd/domain/animation/model_factory.py",
        "src/lychd/domain/archive/contracts.py",
        "src/lychd/domain/archive/derivation.py",
        "src/lychd/domain/archive/derivation_service.py",
        "src/lychd/domain/archive/semantic.py",
        "src/lychd/domain/archive/semantic_service.py",
        "src/lychd/domain/codex/guards.py",
        "src/lychd/domain/codex/local_access.py",
        "src/lychd/domain/codex/middleware.py",
        "src/lychd/domain/codex/sigil.py",
        "src/lychd/domain/cortex/context.py",
        "src/lychd/domain/cortex/dispatcher.py",
        "src/lychd/domain/cortex/engine.py",
        "src/lychd/domain/cortex/leases.py",
        "src/lychd/domain/cortex/ledger.py",
        "src/lychd/domain/cortex/runs.py",
        "src/lychd/domain/cortex/semantic_answer.py",
        "src/lychd/ghouls/runs.py",
        "src/lychd/interface/web/altar_services.py",
        "src/lychd/interface/web/archive.py",
        "src/lychd/interface/web/archive_access.py",
        "src/lychd/interface/web/archive_derivation.py",
        "src/lychd/interface/web/archive_derivation_factory.py",
        "src/lychd/interface/web/archive_generation_factory.py",
        "src/lychd/interface/web/archive_response.py",
        "src/lychd/interface/web/archive_semantic.py",
        "src/lychd/interface/web/archive_semantic_factory.py",
        "src/lychd/interface/web/lifespan.py",
        "src/lychd/interface/web/openapi.py",
        "src/lychd/system/services/queues.py",
        "tests/conftest.py",
        "tests/integration/test_archive_semantic_native_runtime.py",
        "tests/integration/test_local_bridge_runtime.py",
        "tests/integration/test_production_wiring.py",
        "tests/integration/test_semantic_answer_native_runtime.py",
        "tests/scout_postgres_fixture.py",
        "tests/support/archive_semantic_runtime.py",
        "tests/support/embedding_runtime.py",
        "uv.lock",
    }
    canonical = json.dumps(receipt["source_sha256"], sort_keys=True, separators=(",", ":")).encode()
    assert sha256(canonical).hexdigest() == "a22dac7df7c0917908c1d8c77be16875848550bce5a3ecce982fa6e2736ff15a"
    assert receipt["record_kind"] == "sanitized_public_maintained_native_semantic_answer_qualification"
    assert receipt["status"] == receipt["cleanup"] == "passed"
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
