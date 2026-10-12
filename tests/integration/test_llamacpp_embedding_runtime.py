"""Explicit public-model proof: real registered driver, externally started runtime.

No model/image downloads, production units/configuration, host listeners or GPU.
The ordinary rootless cache/session must already support the fixed image.
"""

from pathlib import Path

import pytest

from lychd.extensions.builtin.animator.llamacpp.embedding_profile import PROFILE
from tests.support.embedding_runtime import run_registered_embedding_proof

pytestmark = [pytest.mark.integration, pytest.mark.container]


def test_registered_embedding_runtime(tmp_path: Path) -> None:
    receipt = run_registered_embedding_proof(tmp_path / "embedding-runtime")
    assert receipt["status"] == "passed"
    assert receipt["cleanup"] == "passed"
    proof = receipt["proof"]
    assert proof["profile_digest"] == PROFILE.digest
    assert proof["driver_calls_completed"] == 3
    assert proof["source_request_authorizations"] == [3, 3, 3]
    assert proof["leases_remaining"] == 0
    assert proof["negative_controls"] == [
        "input_digest_or_size",
        "input_tokens",
        "escaped_driver",
        "authority_before_embedding",
    ]
