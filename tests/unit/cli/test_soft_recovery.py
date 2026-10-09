from __future__ import annotations

from typing import TYPE_CHECKING

from click.testing import CliRunner

from lychd.cli.commands import reactor_group

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_recover_soft_requires_exact_occurrence_without_path_override(mocker: MockerFixture) -> None:
    recover = mocker.patch("lychd.cli.commands._recover_soft_activation", new_callable=mocker.AsyncMock)
    runner = CliRunner()
    assert runner.invoke(reactor_group, ["recover-soft", "../other"]).exit_code == 2
    assert runner.invoke(reactor_group, ["recover-soft", "a" * 32, "--path", "/elsewhere/other"]).exit_code == 2
    recover.assert_not_awaited()


def test_recover_soft_delegates_exact_id_and_reports_refusal(mocker: MockerFixture) -> None:
    recover = mocker.patch("lychd.cli.commands._recover_soft_activation", new_callable=mocker.AsyncMock)
    runner = CliRunner()
    result = runner.invoke(reactor_group, ["recover-soft", "a" * 32])
    assert result.exit_code == 0
    recover.assert_awaited_once_with("a" * 32)
    recover.side_effect = RuntimeError("Vessel still active")
    result = runner.invoke(reactor_group, ["recover-soft", "a" * 32])
    assert result.exit_code == 1
    assert "Vessel still active" in result.output
    assert "Retired soft activation" not in result.output
