from __future__ import annotations

import json
import logging
import threading

import pytest
import structlog

from lychd.config.logging import build_log_config
from lychd.config.settings.root import SettingsSnapshot


def configure_exception_logging(*, as_json: bool) -> None:
    """Use explicit I/O-free settings through the ordinary shared builder."""
    settings = SettingsSnapshot(payload="{}").materialize()
    config = build_log_config(render_as_json=as_json, settings=settings).structlog_logging_config
    standard = config.standard_lib_logging_config
    assert standard is not None
    standard.configure()
    config.configure()


def _raise_runtime_error(message: str) -> None:
    """Create a real traceback for each diagnostic producer."""
    raise RuntimeError(message)


@pytest.mark.parametrize("producer", ["stdlib-thread", "structlog"])
def test_json_exception_survives_shared_formatter(producer: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Foreign worker exceptions and direct events remain one JSON stderr record."""
    configure_exception_logging(as_json=True)

    def emit() -> None:
        try:
            _raise_runtime_error("native heartbeat stopped")
        except RuntimeError:
            if producer == "stdlib-thread":
                logging.getLogger("lychd.test.foreign-exception").exception("Worker heartbeat failed")
            else:
                structlog.get_logger("lychd.test.direct-exception").exception("Worker heartbeat failed")

    if producer == "stdlib-thread":
        thread = threading.Thread(target=emit, name="owned-exception-regression", daemon=False)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
    else:
        emit()
    captured = capsys.readouterr()
    assert captured.out == ""
    record = json.loads(captured.err)
    assert record["message"] == "Worker heartbeat failed"
    assert record["level"] == "error"
    assert "RuntimeError" in json.dumps(record["exception"])
    assert "native heartbeat stopped" in json.dumps(record["exception"])
    assert "exc_info" not in record
    assert "--- Logging error ---" not in captured.err


def test_console_exception_remains_human_readable(capsys: pytest.CaptureFixture[str]) -> None:
    """The repair must preserve the existing console exception renderer."""
    configure_exception_logging(as_json=False)
    try:
        _raise_runtime_error("native console stopped")
    except RuntimeError:
        logging.getLogger("lychd.test.console-exception").exception("Worker console failed")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Worker console failed" in captured.err
    assert "RuntimeError" in captured.err
    assert "--- Logging error ---" not in captured.err
