"""Preserve the exact trusted OCI observation and its current source boundary."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
RECEIPT = ROOT / "tests/fixtures/runtime/container-only-2026-10-20.json"
RECEIPT_SHA256 = "487fd1dbc3764050e8d6eb2476f17e163f401967718a6788db36137be37a7b28"


def test_exact_container_observation_and_current_inputs() -> None:
    raw = RECEIPT.read_bytes()
    assert sha256(raw).hexdigest() == RECEIPT_SHA256
    receipt = cast("dict[str, Any]", json.loads(raw))
    assert receipt["schema"] == "lychd.container-only-qualification.v1"
    assert receipt["observed_on"] == "2026-10-20"
    build = receipt["production_inputs"]
    paths = {ROOT / path for path in build["roots"]}
    paths.update(
        path
        for path in (ROOT / "src").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    manifest = {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest() for path in paths}
    assert len(manifest) == build["count"] == 553
    assert sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == build["sha256"]
    assert build["clean_checkout_attested"] is False
    assert build["atomic_transitive_closure_attested"] is False
    for name, expected in receipt["ward_candidate"]["source_fingerprints"].items():
        assert sha256((ROOT / name).read_bytes()).hexdigest() == expected, name


def test_container_observation_keeps_runtime_and_effect_limits() -> None:
    receipt = cast("dict[str, Any]", json.loads(RECEIPT.read_bytes()))
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
    assert set(receipt["effects"].values()) == {0}
    assert len(receipt["unproved"]) == 6
    assert "Public CLI active container start/stop completion" in receipt["unproved"]
