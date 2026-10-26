"""Seal dated pre-removal observations without qualifying current container code.

The 20 October container-only decision retired host-native Core deployment and
its qualification drivers. Source maps and dated facts remain historical evidence;
no live file or installed dependency comparison renews them. The two 19 October
receipts use a 26 October
representation-only privacy redaction; their replacement byte seals attest that
redacted representation, never a new runtime qualification.
"""

# ruff: noqa: INP001 - existing tests/support namespace is intentional

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

_HISTORICAL_SHA256 = {
    "local-bridge-vllm-native-tcp.json": "e63e321998d70d2a8901d610c687e95c289d7d667fb9986c6d1052020fd68831",
    "local-bridge-vllm-runtime.json": "a15c5eed284eb101fce340d697e25dd975d72b4891723429d0a3d47cfeec6c44",
    "local-bridge-vllm-process-restart.json": "ec32ecb083efacddab98d8cc772be317e455b22ed2a75f44b3b328250bc5a579",
}

# Deliberate evidence-contract retirement; this is not a fingerprint renewal.
HISTORICAL_RETIREMENT = {
    "source_base": "7057210a24cb0e777fb8d2af034a0baffeaa349c",
    "retired_on": "2026-10-20",
    "reason": "container-only-Core-production-and-testing-removes-native-deployment",
    "no_current_qualification": True,
}

HISTORICAL_NATIVE_SOURCES = frozenset(
    {
        "src/lychd/__main__.py",
        "src/lychd/cli/base.py",
        "src/lychd/cli/commands.py",
        "src/lychd/config/settings/server.py",
        "src/lychd/cli/operator.py",
        "src/lychd/system/operator/__init__.py",
        "src/lychd/system/operator/models.py",
        "src/lychd/system/operator/composition.py",
        "src/lychd/system/operator/control.py",
        "src/lychd/interface/server.py",
        "src/lychd/interface/web/altar.py",
        "src/lychd/interface/web/orb.py",
        "tests/integration/test_local_bridge_process_restart.py",
        "tests/integration/test_local_bridge_native_tcp.py",
        "tests/integration/test_server_process.py",
        "tests/unit/domain/animation/test_local_bridge_native_tcp.py",
    }
)


def read_historical_receipt(path: Path) -> dict[str, Any]:
    """Seal the complete retained representation before validating its dated facts."""
    expected = _HISTORICAL_SHA256[path.name]
    raw = path.read_bytes()
    assert sha256(raw).hexdigest() == expected, "historical receipt bytes changed"
    return cast("dict[str, Any]", json.loads(raw))


def require_historical_source_map(receipt: dict[str, Any], sources: set[str], digest: str) -> None:
    """Check a frozen map's closure and bytes, never the current source tree."""
    assert HISTORICAL_RETIREMENT["no_current_qualification"] is True
    fingerprints = receipt["source_fingerprints"]
    assert set(fingerprints) == sources
    assert all(len(value) == 64 and set(value) <= set("0123456789abcdef") for value in fingerprints.values())
    canonical = json.dumps(fingerprints, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert sha256(canonical).hexdigest() == digest


def require_historical_tcp_coverage(root: Path, shared_sources: set[str], required_sources: set[str]) -> dict[str, Any]:
    """Preserve the dated 75-path TCP lineage without claiming current execution."""
    assert HISTORICAL_RETIREMENT == {
        "source_base": "7057210a24cb0e777fb8d2af034a0baffeaa349c",
        "retired_on": "2026-10-20",
        "reason": "container-only-Core-production-and-testing-removes-native-deployment",
        "no_current_qualification": True,
    }
    expected = set(shared_sources) | HISTORICAL_NATIVE_SOURCES
    assert len(expected) == 75
    assert required_sources <= expected
    receipt = read_historical_receipt(root / "tests/fixtures/animator/local-bridge-vllm-native-tcp.json")
    assert receipt["schema"] == "local-bridge-vllm-native-tcp@1"
    assert receipt["qualification_date"] == "2026-10-20"
    assert receipt["status"] == "passed"
    require_historical_source_map(receipt, expected, "0a104613c035ea6020d8bbf8a0ab365fdd8ab773b1e14a973895b53263c49473")
    assert (
        receipt["qualification_evidence"]["native_test_sha256_before_trial"]
        == receipt["source_fingerprints"]["tests/integration/test_local_bridge_native_tcp.py"]
    )
    return receipt
