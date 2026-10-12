"""Host command producers preserve literal executable identity without a shell."""

import re
import sys
from pathlib import Path

import pytest

from lychd.config.runes.registry import RuneRegistry
from lychd.config.settings.root import Settings
from lychd.extensions.host import assemble_extensions
from lychd.system.services.bind_compilation import compile_bind_request
from lychd.system.services.reactor import render_reactor_service_unit


def _render(prefix: str, *, reactor: bool, monkeypatch: pytest.MonkeyPatch) -> str:
    if reactor:
        return render_reactor_service_unit(executable=Path(prefix) / "bin" / "lychd", environment={})
    monkeypatch.setattr(sys, "prefix", prefix)
    settings = Settings()
    request = compile_bind_request(
        settings=settings,
        extensions=assemble_extensions(settings),
        runes=RuneRegistry(()),
        soulstones=(),
        portals=(),
        uncaged=True,
    )
    return request.plain_unit_mapping()["lychd-uncaged-vessel.service"]


@pytest.mark.parametrize("reactor", [False, True], ids=["uncaged", "reactor"])
@pytest.mark.parametrize("prefix", ["/opt/venv", "/opt/venv with spaces"])
def test_host_command_preserves_executable_identity(
    prefix: str, *, reactor: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered = _render(prefix, reactor=reactor, monkeypatch=monkeypatch)
    commands = [line for line in rendered.splitlines() if line.startswith("ExecStart=")]
    assert len(commands) == 1
    command = re.fullmatch(r'ExecStart="([^"]+)" (.+)', commands[0])
    assert command is not None
    assert Path(command.group(1)) == Path(prefix) / "bin" / "lychd"
    assert command.group(2) == ("reactor consume" if reactor else "serve --host 127.0.0.1 --port 7134")


@pytest.mark.parametrize("reactor", [False, True], ids=["uncaged", "reactor"])
@pytest.mark.parametrize(
    "prefix",
    [
        "relative/venv",
        '/opt/venv"quoted',
        "/opt/venv'quoted",
        r"/opt/venv\escaped",
        "/opt/$VENV",
        "/opt/%h",
        "/opt/%%literal",
        "/opt/venv ; /other",
        "/opt/venv\nExecStart=/other",
        "/opt/venv\rreturn",
        "/opt/venv\ttab",
        "/opt/venv\x00nul",
    ],
)
def test_host_command_refuses_unsupported_executable_syntax(
    prefix: str, *, reactor: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="systemd executable"):
        _render(prefix, reactor=reactor, monkeypatch=monkeypatch)
