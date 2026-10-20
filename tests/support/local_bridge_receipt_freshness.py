"""Offline shared-chain freshness; historical occurrences remain immutable.

Root accepted the dated TCP03 occurrence after its independent physical audit
and review of the sanitized fixture and complete scenario owner.
This module never imports metadata test modules or opens runtime surfaces.
"""

# ruff: noqa: INP001 - existing tests/support namespace is intentional

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

_HISTORICAL_SHA256 = {
    "local-bridge-vllm-runtime.json": "20d5a529338815bd43588e87fac91f1041aacaf2f470fe62db4d414d8f0033e2",
    "local-bridge-vllm-process-restart.json": "a6571c0f111ba3178dc10c0ee0d962e13bfd236c634a187abee038fbaec38a24",
}

# Exact reviewed artifact from the accepted 20 October occurrence.
ACCEPTED_CURRENT_TCP_FIXTURE_SHA256: str | None = "e63e321998d70d2a8901d610c687e95c289d7d667fb9986c6d1052020fd68831"

NATIVE_SOURCES = frozenset(
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


def require_current_tcp_coverage(root: Path, shared_sources: set[str], required_sources: set[str]) -> dict[str, Any]:
    """Join explicit historical input coverage to all75 current TCP critical paths.

    The native metadata module retains the complete scenario/provenance/privacy/
    resource/dependency validators. This function owns only the approved whole
    artifact identity, exact source closure and current path-byte comparison.
    """
    expected = set(shared_sources) | NATIVE_SOURCES
    assert len(expected) == 75, "review a critical-source closure change explicitly"
    assert required_sources <= expected, "historical critical input missing from TCP closure"
    pin = ACCEPTED_CURRENT_TCP_FIXTURE_SHA256
    assert isinstance(pin, str), "actual accepted TCP fixture whole SHA256 is required"
    assert len(pin) == 64, "accepted TCP fixture SHA256 must contain 64 characters"
    assert set(pin) <= set("0123456789abcdef"), "accepted TCP fixture SHA256 must be lowercase hexadecimal"
    path = root / "tests/fixtures/animator/local-bridge-vllm-native-tcp.json"
    raw = path.read_bytes()
    assert sha256(raw).hexdigest() == pin, "current TCP fixture differs from exact reviewed accepted artifact"
    receipt = cast("dict[str, Any]", json.loads(raw))
    assert receipt["schema"] == "local-bridge-vllm-native-tcp@1"
    assert receipt["status"] == "passed"
    fingerprints = receipt["source_fingerprints"]
    assert set(fingerprints) == expected
    assert len(fingerprints) == 75
    assert required_sources <= fingerprints.keys()
    for relative, digest in fingerprints.items():
        source = root / relative
        assert source.resolve() == source, relative
        assert source.is_relative_to(root), relative
        assert source.is_file(), relative
        assert sha256(source.read_bytes()).hexdigest() == digest, relative
    assert (
        receipt["qualification_evidence"]["native_test_sha256_before_trial"]
        == fingerprints["tests/integration/test_local_bridge_native_tcp.py"]
    )
    return receipt
