"""Shared host-unit quoting remains available after native Vessel retirement."""

from pathlib import Path

import pytest

from lychd.system.schemas import systemd_environment_assignment, systemd_executable_token


def test_systemd_environment_assignment_quotes_one_complete_value() -> None:
    assert systemd_environment_assignment("LABEL", 'two words and "quoted" ${LITERAL}') == (
        r'"LABEL=two words and \"quoted\" ${LITERAL}"'
    )


@pytest.mark.parametrize("key", ["NOT SAFE", "../foreign", "A=B"])
def test_systemd_environment_assignment_refuses_unsafe_names(key: str) -> None:
    with pytest.raises(ValueError, match="environment variable name"):
        systemd_environment_assignment(key, "value")


@pytest.mark.parametrize("value", ["swallow-next-directive\\", r"value\x22 MALICE=1", "value\nNext=directive"])
def test_systemd_environment_assignment_refuses_directive_escape(value: str) -> None:
    with pytest.raises(ValueError, match=r"backslash|single-line"):
        systemd_environment_assignment("SAFE", value)


def test_systemd_executable_quotes_one_literal_absolute_token() -> None:
    assert systemd_executable_token(Path("/venv with spaces/bin/lychd")) == '"/venv with spaces/bin/lychd"'


@pytest.mark.parametrize("value", ["relative/bin/lychd", "/venv/%n/lychd", "/venv/quoted'path/lychd"])
def test_systemd_executable_refuses_unsupported_path_authority(value: str) -> None:
    with pytest.raises(ValueError, match=r"absolute|specifier|quotes"):
        systemd_executable_token(Path(value))
