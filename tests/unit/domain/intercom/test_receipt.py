"""Preserve dated peer observations while refusing changed-source qualification."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

_ROOT = Path(__file__).resolve().parents[4]
_RECEIPT = _ROOT / "tests/fixtures/intercom/operator-public-source.json"
_HISTORICAL_DEPENDENCIES = {
    "http-message-signatures": "2.0.1",
    "h11": "0.16.0",
    "cryptography": "50.0.2",
}


def _receipt() -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        json.loads(_RECEIPT.read_text()),
    )


def _source_pins(receipt: dict[str, Any]) -> dict[str, str]:
    """Validate the bounded inventory without substituting current source bytes."""
    pins = cast("dict[str, str]", receipt["source_pins"])
    assert pins
    for relative, expected in pins.items():
        path = Path(relative)
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert ".venv" not in path.parts
        assert len(expected) == 64
        assert set(expected) <= set("0123456789abcdef")
    return pins


def _require_current_qualification(receipt: dict[str, Any], *, root: Path = _ROOT) -> None:
    """Require exact current files and dependencies before reusing bounded proof."""
    root = root.resolve()
    for relative, expected in _source_pins(receipt).items():
        source = (root / relative).resolve()
        assert source.is_relative_to(root)
        assert source.is_file(), f"Source inventory is stale: {relative} is missing"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, f"Source inventory is stale: {relative}"
    dependencies = cast("dict[str, str]", receipt["production_dependencies"])
    assert dependencies
    for name, expected in dependencies.items():
        assert version(name) == expected, f"Dependency inventory is stale: {name}"


def test_historical_bounded_qualification_keeps_original_bytes_and_inventory() -> None:
    assert hashlib.sha256(_RECEIPT.read_bytes()).hexdigest() == (
        "8d15dddc446427337f6aae37672994942eae87d69c901a2701e0d1dadf14268d"
    )
    receipt = _receipt()
    assert receipt["schema_version"] == 1
    assert receipt["profile"] == "outbound-public-text@1"
    assert receipt["qualification"] == "operator-public-source-2026-10-22-r11"
    assert receipt["requalification"]["date"] == "2026-10-22"
    assert receipt["requalification"]["base_revision_declared"] == "0e0dc859699edf279aecc17d0eff0a9ac97945ed"
    assert receipt["requalification"]["clean_checkout_attested"] is False
    pins = _source_pins(receipt)
    assert len(pins) == 26
    assert {
        "src/lychd/db/models/run.py",
        "src/lychd/db/migrations/versions/0016_intercom.py",
        "src/lychd/db/migrations/versions/0017_native_peer_graph.py",
        "tests/postgres_model_tables.py",
    } <= pins.keys()
    assert receipt["production_dependencies"] == _HISTORICAL_DEPENDENCIES


def test_historical_peer_observation_cannot_qualify_current_sources() -> None:
    with pytest.raises(AssertionError, match="Source inventory is stale"):
        _require_current_qualification(_receipt())


@pytest.mark.parametrize("change", ["modified", "missing", "dependency"])
def test_current_qualification_requires_exact_source_and_dependency_inventory(tmp_path: Path, change: str) -> None:
    source = tmp_path / "probe.py"
    source.write_text("original bounded source\n")
    pins = {"probe.py": hashlib.sha256(source.read_bytes()).hexdigest()}
    dependencies = {"h11": version("h11")}
    receipt: dict[str, Any] = {"source_pins": pins, "production_dependencies": dependencies}
    _require_current_qualification(receipt, root=tmp_path)
    if change == "modified":
        source.write_text("changed bounded source\n")
    elif change == "missing":
        source.unlink()
    else:
        dependencies["h11"] = "not-the-installed-version"
    with pytest.raises(AssertionError, match="inventory is stale"):
        _require_current_qualification(receipt, root=tmp_path)


@pytest.mark.parametrize("relative", ["../outside.py", "/outside.py", ".venv/probe.py"])
def test_current_qualification_refuses_unsafe_source_inventory(relative: str) -> None:
    with pytest.raises(AssertionError):
        _source_pins({"source_pins": {relative: "0" * 64}})


def test_current_qualification_refuses_source_symlink_outside_checkout(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside the admitted checkout\n")
    (root / "probe.py").symlink_to(outside)
    receipt: dict[str, Any] = {
        "source_pins": {"probe.py": hashlib.sha256(outside.read_bytes()).hexdigest()},
        "production_dependencies": {"h11": version("h11")},
    }
    with pytest.raises(AssertionError):
        _require_current_qualification(receipt, root=root)


def test_manual_peer_evidence_does_not_promote_private_or_graph_operation() -> None:
    receipt = _receipt()
    assert receipt["independent_peer"]["sdk"] == "a2a-sdk==1.2.3"
    assert receipt["observed"]["production_sdk_cases"] == 4
    assert receipt["observed"]["actual_postgresql_tests"] == 15
    assert receipt["observed"]["no_uncertain_resubmit"] is True
    limits = receipt["limits"]
    for boundary in (
        "native_peer_graph",
        "run_cut_transport",
        "private_voice_context_egress",
        "incoming_ward_or_a2a",
        "automatic_fallback",
        "normal_boot_activation",
    ):
        assert limits[boundary] is False
    evidence = cast("dict[str, str]", receipt["evidence_ids"])
    assert len({UUID(value) for value in evidence.values()}) == len(evidence) == 4
