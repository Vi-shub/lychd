"""Shared binary staging and bounded attestation regression tests."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from lychd.system.services import file_publication_recovery as recovery
from lychd.system.services import file_publication_transaction as publication
from lychd.system.services.file_publication_models import FileIdentity, PublicationRollbackError


@pytest.fixture
def parent_fd(tmp_path: Path) -> Iterator[int]:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("payload", [b"", bytes(range(256)), bytes(range(256)) * 4096], ids=["empty", "binary", "1MiB"])
def test_exact_binary_publication(payload: bytes, tmp_path: Path, parent_fd: int) -> None:
    target = tmp_path / "raw"
    staged = publication.stage_bytes_file(parent_fd=parent_fd, path=target, content=payload, mode=0o600)
    assert not target.exists()
    assert (tmp_path / staged.staging_name).read_bytes() == payload
    assert publication.publish_file(staged)
    publication.attest_public_file(staged, owner_uid=os.getuid(), mode=0o600, links=2, size=len(payload))
    recovery.remove_staging(staged)
    publication.attest_public_file(staged, links=1)
    assert target.read_bytes() == payload
    assert set(tmp_path.iterdir()) == {target}


def test_builder_binds_actual_new_inode_before_any_byte_is_written(tmp_path: Path, parent_fd: int) -> None:
    target = tmp_path / "bound"
    captured: list[FileIdentity] = []

    def build(identity: FileIdentity) -> bytes:
        captured.append(identity)
        (candidate,) = tmp_path.iterdir()
        metadata = candidate.stat()
        assert metadata.st_size == 0
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert (identity.device, identity.inode) == (metadata.st_dev, metadata.st_ino)
        return f"{identity.device}:{identity.inode}".encode() + b"\x00\xff"

    staged = publication.stage_bytes_file(parent_fd=parent_fd, path=target, content=build, mode=0o600)
    assert captured == [staged.identity]
    assert (tmp_path / staged.staging_name).read_bytes().endswith(b"\x00\xff")
    recovery.remove_staging(staged)


def test_failed_builder_preserves_foreign_stage_replacement(tmp_path: Path, parent_fd: int) -> None:
    def replace_and_fail(identity: FileIdentity) -> bytes:
        (candidate,) = tmp_path.iterdir()
        candidate.rename(tmp_path / "original")
        candidate.write_bytes(b"foreign")
        message = "builder refused"
        raise ValueError(message)

    with pytest.raises(PublicationRollbackError) as caught:
        publication.stage_bytes_file(
            parent_fd=parent_fd, path=tmp_path / "public", content=replace_and_fail, mode=0o600
        )
    assert caught.value.outcome == "recovery"
    assert any(path.read_bytes() == b"foreign" for path in tmp_path.iterdir())
    assert not (tmp_path / "public").exists()


def test_exclusive_stage_collisions_are_bounded_and_preserved(
    tmp_path: Path,
    parent_fd: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = uuid4()
    foreign = tmp_path / f".lychd-create-{fixed.hex}"
    foreign.write_bytes(b"foreign")
    monkeypatch.setattr(publication, "uuid4", lambda: fixed)
    with pytest.raises(RuntimeError, match="allocate"):
        publication.stage_bytes_file(parent_fd=parent_fd, path=tmp_path / "public", content=b"owned", mode=0o600)
    assert set(tmp_path.iterdir()) == {foreign}
    assert foreign.read_bytes() == b"foreign"


def test_existing_public_name_never_clobbered(tmp_path: Path, parent_fd: int) -> None:
    target = tmp_path / "public"
    target.write_bytes(b"foreign")
    original = target.stat()
    staged = publication.stage_bytes_file(parent_fd=parent_fd, path=target, content=b"owned", mode=0o600)
    assert not publication.publish_file(staged)
    assert target.read_bytes() == b"foreign"
    assert target.stat().st_ino == original.st_ino
    assert set(tmp_path.iterdir()) == {target}


def test_fifo_attestation_finishes_without_writer_and_preserves_fifo(tmp_path: Path) -> None:
    script = """
import os, sys
from pathlib import Path
from lychd.system.services import file_publication_transaction as p
from lychd.system.services import file_publication_recovery as r
from lychd.system.services.file_publication_models import PublicationRollbackError
root = Path(sys.argv[1])
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    staged = p.stage_bytes_file(parent_fd=fd, path=root / 'public', content=b'owned', mode=0o600)
    os.mkfifo(root / 'public', 0o600)
    try:
        p.attest_public_file(staged)
    except PublicationRollbackError:
        pass
    else:
        raise AssertionError('special file accepted')
    r.remove_staging(staged)
finally:
    os.close(fd)
"""
    result = subprocess.run(  # noqa: S603 - fixed local interpreter and source, isolated fixture path
        [sys.executable, "-B", "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert stat.S_ISFIFO((tmp_path / "public").stat().st_mode)
