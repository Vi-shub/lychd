"""A reconstructed local identity cannot replace authenticated possession."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from lychd.domain.codex.local_access import LocalOperatorAccess
from lychd.domain.codex.sigil import default_local_sigil

_PASSWORD = "synthetic-local-operator-password"  # noqa: S105 - public test credential


def test_actual_marker_and_retained_binding_have_different_roles() -> None:
    access = LocalOperatorAccess(access_password=_PASSWORD)
    marker = access.authenticate(username=b"magus", password=_PASSWORD.encode())
    assert marker is not None
    assert access.current(marker)
    assert access.current_binding(marker.subject, marker.authentication_generation)
    assert not access.current(replace(marker))
    assert not access.current(default_local_sigil())
    assert not access.current(None)
    assert _PASSWORD not in repr(access)
    assert _PASSWORD not in repr(marker)


def test_same_password_new_startup_does_not_restore_old_authentication() -> None:
    first = LocalOperatorAccess(access_password=_PASSWORD)
    marker = first.authenticate(username=b"magus", password=_PASSWORD.encode())
    assert marker is not None
    restarted = LocalOperatorAccess(access_password=_PASSWORD)
    assert not restarted.current(marker)
    assert not restarted.current_binding(marker.subject, marker.authentication_generation)
    assert restarted.authenticate(username=b"magus", password=_PASSWORD.encode()) is not None


def test_revocation_is_monotonic_and_refuses_new_possession() -> None:
    access = LocalOperatorAccess(access_password=_PASSWORD)
    marker = access.authenticate(username=b"magus", password=_PASSWORD.encode())
    assert marker is not None
    access.revoke()
    access.revoke()
    assert not access.current(marker)
    assert not access.current_binding(marker.subject, marker.authentication_generation)
    assert access.authenticate(username=b"magus", password=_PASSWORD.encode()) is None


def test_wrong_credentials_never_return_marker() -> None:
    access = LocalOperatorAccess(access_password=_PASSWORD)
    assert access.authenticate(username=b"magus", password=b"wrong") is None
    assert access.authenticate(username=b"admin", password=_PASSWORD.encode()) is None


@pytest.mark.asyncio
async def test_bound_access_refuses_foreign_loop_revocation() -> None:
    access = LocalOperatorAccess(access_password=_PASSWORD)
    access.bind_control_loop()
    marker = access.authenticate(username=b"magus", password=_PASSWORD.encode())
    assert marker is not None

    async def foreign() -> None:
        with pytest.raises(RuntimeError, match="another control loop"):
            access.revoke()
        with pytest.raises(RuntimeError, match="another control loop"):
            access.bind_control_loop()

    await asyncio.to_thread(lambda: asyncio.run(foreign()))
    assert access.current(marker)
    access.revoke()
    assert not access.current(marker)
