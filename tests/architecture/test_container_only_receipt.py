"""Seal dated OCI observations without qualifying the merged production closure."""

from __future__ import annotations

import json
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECEIPT = ROOT / "tests/fixtures/runtime/container-only-2026-10-20.json"
RECEIPT_SHA256 = "487fd1dbc3764050e8d6eb2476f17e163f401967718a6788db36137be37a7b28"
RECHECK_RECEIPT = ROOT / "tests/fixtures/runtime/container-recheck-2026-10-21.json"
RECHECK_RECEIPT_SHA256 = "97832fa27168e3e6b03069346ffa7ae0699df8b21e8c9c01ace03abd3fb5f32c"
PRODUCTION_ROOTS = frozenset(
    {
        ".containerignore",
        "Containerfile",
        "LICENSE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        "uv.lock",
        "clients/web/static/THIRD_PARTY_NOTICES.txt",
        "scripts/generate_python_third_party_notices.py",
    }
)


def _read_historical_receipt(path: Path, expected: str) -> dict[str, Any]:
    raw = path.read_bytes()
    assert sha256(raw).hexdigest() == expected, "historical receipt bytes changed"
    return cast("dict[str, Any]", json.loads(raw))


def _source_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    assert not path.is_absolute()
    assert not {"..", ".venv"}.intersection(path.parts)
    source = (root / path).resolve()
    assert source.is_relative_to(root.resolve())
    assert source.is_file(), f"source inventory changed: {relative} is missing"
    return source


def _require_current_production_inputs(receipt: dict[str, Any], *, root: Path = ROOT) -> None:
    """Check all declared production inputs offline; this does not replay a boot."""
    build = receipt["production_inputs"]
    assert set(build["roots"]) == PRODUCTION_ROOTS
    assert len(build["roots"]) == len(PRODUCTION_ROOTS)
    paths = set(PRODUCTION_ROOTS)
    paths.update(
        str(path.relative_to(root))
        for path in (root / "src").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    manifest = {relative: sha256(_source_file(root, relative).read_bytes()).hexdigest() for relative in paths}
    assert len(manifest) == build["count"], "production input count changed"
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    assert sha256(canonical).hexdigest() == build["sha256"], "production input digest changed"


def _require_current_ward_inputs(receipt: dict[str, Any], *, root: Path = ROOT) -> None:
    """Require every bound Ward source and installed dependency before reuse."""
    ward = receipt["ward_candidate"]
    assert ward["source_fingerprints"]
    for relative, expected in ward["source_fingerprints"].items():
        assert sha256(_source_file(root, relative).read_bytes()).hexdigest() == expected, "source inventory changed"
    assert ward["dependency_versions"]
    for name, expected in ward["dependency_versions"].items():
        assert version(name) == expected, f"dependency inventory changed: {name}"


def test_historical_container_observation_preserves_original_ward_inventory() -> None:
    receipt = _read_historical_receipt(RECEIPT, RECEIPT_SHA256)
    assert receipt["schema"] == "lychd.container-only-qualification.v1"
    assert receipt["observed_on"] == "2026-10-20"
    build = receipt["production_inputs"]
    assert build["count"] == 553
    assert build["sha256"] == "b03f0df6e78572391227d34b46e69d8d0922bc3134d33504b332ccd5b1cabe5b"
    assert build["clean_checkout_attested"] is False
    assert build["atomic_transitive_closure_attested"] is False
    assert set(receipt["ward_candidate"]["source_fingerprints"]) == {
        "tests/conftest.py",
        "tests/integration/test_ward_pg.py",
        "tests/integration/test_native_candidate_authority_pg.py",
        "tests/integration/test_database_authority_pg.py",
        "tests/integration/test_archive_migrations_pg.py",
        "tests/unit/system/services/test_native_candidate_authority.py",
    }


def test_historical_container_recheck_preserves_original_production_inventory() -> None:
    receipt = _read_historical_receipt(RECHECK_RECEIPT, RECHECK_RECEIPT_SHA256)
    assert receipt["schema"] == "lychd.container-production-recheck.v1"
    assert receipt["observed_on"] == "2026-10-21"
    assert receipt["predecessor_receipt"] == {
        "path": str(RECEIPT.relative_to(ROOT)),
        "sha256": RECEIPT_SHA256,
    }
    build = receipt["production_inputs"]
    assert build["count"] == 553
    assert build["sha256"] == "99b79af19ab0250deba4a18af81bb6de65197a044f47e5856bc98e1129768e44"
    assert build["clean_checkout_attested"] is False
    assert build["atomic_transitive_closure_attested"] is False


@pytest.mark.parametrize("receipt_path", [RECEIPT, RECHECK_RECEIPT])
def test_historical_production_observations_cannot_qualify_current_inputs(receipt_path: Path) -> None:
    receipt = cast("dict[str, Any]", json.loads(receipt_path.read_bytes()))
    with pytest.raises(AssertionError, match=r"production input (count|digest) changed"):
        _require_current_production_inputs(receipt)


def test_historical_ward_observation_cannot_qualify_changed_inputs() -> None:
    receipt = _read_historical_receipt(RECEIPT, RECEIPT_SHA256)
    with pytest.raises(AssertionError, match="source inventory changed"):
        _require_current_ward_inputs(receipt)


@pytest.mark.parametrize("change", ["modified-source", "added-source", "missing-source", "dependency-lock"])
def test_production_input_oracle_requires_exact_source_and_dependency_closure(tmp_path: Path, change: str) -> None:
    files = {relative: f"synthetic input: {relative}\n".encode() for relative in PRODUCTION_ROOTS | {"src/probe.py"}}
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {relative: sha256(content).hexdigest() for relative, content in files.items()}
    receipt: dict[str, Any] = {
        "production_inputs": {
            "roots": sorted(PRODUCTION_ROOTS),
            "count": len(manifest),
            "sha256": sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
    }
    _require_current_production_inputs(receipt, root=tmp_path)
    if change == "modified-source":
        (tmp_path / "src/probe.py").write_text("changed source\n")
    elif change == "added-source":
        (tmp_path / "src/added.py").write_text("unqualified source\n")
    elif change == "missing-source":
        (tmp_path / "src/probe.py").unlink()
    else:
        (tmp_path / "uv.lock").write_text("changed dependency closure\n")
    with pytest.raises(AssertionError, match=r"production input (count|digest) changed"):
        _require_current_production_inputs(receipt, root=tmp_path)


@pytest.mark.parametrize("change", ["source", "dependency"])
def test_ward_input_oracle_requires_exact_source_and_installed_dependencies(tmp_path: Path, change: str) -> None:
    path = tmp_path / "probe.py"
    path.write_text("synthetic Ward input\n")
    ward = {
        "source_fingerprints": {"probe.py": sha256(path.read_bytes()).hexdigest()},
        "dependency_versions": {"pytest": version("pytest")},
    }
    receipt: dict[str, Any] = {"ward_candidate": ward}
    _require_current_ward_inputs(receipt, root=tmp_path)
    if change == "source":
        path.write_text("changed Ward input\n")
    else:
        ward["dependency_versions"]["pytest"] = "not-the-installed-version"
    with pytest.raises(AssertionError, match=r"(source|dependency) inventory changed"):
        _require_current_ward_inputs(receipt, root=tmp_path)


def test_input_oracles_reject_source_symlink_outside_checkout(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside the bounded checkout\n")
    (root / "probe.py").symlink_to(outside)
    with pytest.raises(AssertionError):
        _source_file(root, "probe.py")


@pytest.mark.parametrize(
    ("receipt_path", "expected"), [(RECEIPT, RECEIPT_SHA256), (RECHECK_RECEIPT, RECHECK_RECEIPT_SHA256)]
)
def test_historical_receipt_seal_rejects_altered_observations(
    tmp_path: Path, receipt_path: Path, expected: str
) -> None:
    altered = tmp_path / receipt_path.name
    receipt = cast("dict[str, Any]", json.loads(receipt_path.read_bytes()))
    receipt["production"]["alembic_head"] = "0023"
    altered.write_text(json.dumps(receipt))
    with pytest.raises(AssertionError, match="historical receipt bytes changed"):
        _read_historical_receipt(altered, expected)


@pytest.mark.parametrize("receipt_path", [RECEIPT, RECHECK_RECEIPT])
def test_container_observation_keeps_runtime_and_effect_limits(receipt_path: Path) -> None:
    receipt = cast("dict[str, Any]", json.loads(receipt_path.read_bytes()))
    production = receipt["production"]
    assert production["command"] == ["python", "-m", "lychd.interface.server", "--caged"]
    assert production["runtime_uid"] == 1001
    assert production["database_bootstrap_exit_code"] == 0
    assert production["alembic_head"] == "0020"
    assert production["read_only_rootfs"] is True
    assert production["cap_drop"] == ["ALL"]
    assert production["no_new_privileges"] is True
    assert production["kernel"] == {
        "memory.max": "2147483648",
        "memory.swap.max": "0",
        "cpu.max": "200000 100000",
        "pids.max": "256",
    }
    assert len(production["boots"]) == 2
    for boot in production["boots"]:
        assert boot["authenticated_status"] == 200
        assert boot["http"] == {
            "schema": 200,
            "no_auth": 401,
            "wrong_auth": 401,
            "foreign_host": 400,
            "foreign_origin": 403,
            "missing_csrf": 403,
        }
        assert boot["exit_code"] == 0
        assert boot["oom_killed"] is False
    assert production["all_owned_containers_volumes_network_absent"] is True
    assert set(receipt["effects"].values()) == {0}
    assert len(receipt["unproved"]) == 6
    assert "Public CLI active container start/stop completion" in receipt["unproved"]


def test_container_ward_observation_keeps_its_dated_limits() -> None:
    receipt = cast("dict[str, Any]", json.loads(RECEIPT.read_bytes()))
    ward = receipt["ward_candidate"]
    assert (ward["passed"], ward["failed"], ward["skipped"], ward["pytest_exit_code"]) == (23, 0, 0, 0)
    assert len(set(ward["test_nodes"])) == 23
    assert sum("test_ward_pg.py::" in node for node in ward["test_nodes"]) == 18
    assert ward["postgres_objects"] == 3
    assert ward["postgres_each_kernel"] == {
        "memory.max": "536870912",
        "memory.swap.max": "0",
        "cpu.max": "100000 100000",
        "pids.max": "128",
    }
    for key in (
        "kernel_readback_before_readiness",
        "all_objects_and_anonymous_volumes_absent",
        "root_cgroups_absent",
        "parent_removed",
    ):
        assert ward[key] is True
