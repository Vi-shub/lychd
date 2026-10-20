"""Seal dated pre-removal observations without qualifying current container code.

The 20 October container-only decision retired host-native Core deployment and
its qualification drivers. Original receipt bytes, source maps and facts remain
historical evidence; no live file or installed dependency comparison renews them.
"""

# ruff: noqa: INP001 - existing tests/support namespace is intentional

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

_HISTORICAL_SHA256 = {
    "local-bridge-vllm-native-tcp.json": "e63e321998d70d2a8901d610c687e95c289d7d667fb9986c6d1052020fd68831",
    "local-bridge-vllm-runtime.json": "20d5a529338815bd43588e87fac91f1041aacaf2f470fe62db4d414d8f0033e2",
    "local-bridge-vllm-process-restart.json": "a6571c0f111ba3178dc10c0ee0d962e13bfd236c634a187abee038fbaec38a24",
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
    """Seal the complete original bytes before validating their dated facts."""
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
