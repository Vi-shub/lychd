"""The offline proof exposes no source material or local evidence digests."""

import json
from unittest.mock import patch
from uuid import UUID

from click.testing import CliRunner
from scripts.privacy_probe import cli

from lychd.domain.privacy.contracts import PrivacyRefused


def test_probe_default_exposes_only_an_opaque_event() -> None:
    result = CliRunner().invoke(cli)
    assert result.exit_code == 0, result.exception
    value = json.loads(result.output)
    assert set(value) == {"event_id"}
    assert UUID(value["event_id"]).version == 4
    assert "SYNTHETIC-PERSON" not in result.output


def test_probe_can_show_only_the_sanitized_synthetic_candidate() -> None:
    result = CliRunner().invoke(cli, ["--show-candidate"])
    assert result.exit_code == 0, result.exception
    event, candidate = (json.loads(line) for line in result.output.splitlines())
    assert set(event) == {"event_id"}
    assert candidate["profile"] == "closed-diagnostic@1"
    assert all("[redacted]" in candidate[lane][0] for lane in candidate if lane != "profile")
    assert "SYNTHETIC-PERSON" not in result.output


def test_probe_verification_failure_exposes_no_candidate_or_exception_data() -> None:
    with patch("scripts.privacy_probe.CutAuthority.verify_seal", side_effect=PrivacyRefused("synthetic-private-error")):
        result = CliRunner().invoke(cli, ["--show-candidate"])
    assert result.exit_code != 0
    assert "Synthetic privacy preparation refused" in result.output
    assert "synthetic-private-error" not in result.output
    assert "[redacted]" not in result.output
