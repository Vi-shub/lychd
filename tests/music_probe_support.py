"""Synthetic probe files stay outside checkout even under Make's in-tree basetemp."""

from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture
def probe_path() -> Iterator[Path]:
    with TemporaryDirectory(prefix="lychd-music-test-") as directory:
        yield Path(directory)
