"""Retained case disclosure requires current authority through an awaited read."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from lychd.compositions.creation.schemas import CaseView, CreationRefused, Criterion, InvestigationRequest, Source
from lychd.compositions.creation.services import InvestigationService, LocalCreationPolicy
from lychd.domain.animation.capabilities import CapabilityPhase
from lychd.domain.codex.local_access import LocalOperatorAccess
from lychd.domain.codex.sigil import Sigil

_ACCESS_VALUE = "inert-creation-read-test-password"


def _reader(store: SimpleNamespace) -> tuple[InvestigationService, LocalOperatorAccess, object, Sigil]:
    access = LocalOperatorAccess(access_password=_ACCESS_VALUE)
    policy = LocalCreationPolicy(access)
    service = InvestigationService(store=store, policy=policy, registry=None, leases=None, capability_key="unused")
    actor = access.authenticate(username=b"magus", password=_ACCESS_VALUE.encode())
    assert actor is not None
    principal = Sigil(name="case-reader", scopes=frozenset({"altar:read"}))
    return service, access, actor, principal


@pytest.mark.asyncio
async def test_case_read_requires_current_authority_and_preserves_retained_result() -> None:
    retained = cast("CaseView", Mock(spec=CaseView))
    store = SimpleNamespace(read=AsyncMock(return_value=retained))
    service, access, actor, principal = _reader(store)
    case_id = uuid4()
    assert await service.read(case_id, authentication=actor, principal=principal) is retained
    store.read.assert_awaited_once_with(case_id)
    access.revoke()
    with pytest.raises(CreationRefused, match="local_authority"):
        await service.read(case_id, authentication=actor, principal=principal)
    assert store.read.await_count == 1


@pytest.mark.asyncio
async def test_case_read_refuses_disclosure_when_authority_revoked_during_storage_await() -> None:
    retained = cast("CaseView", Mock(spec=CaseView))

    async def read(_case_id: object) -> CaseView:
        await asyncio.sleep(0)
        access.revoke()
        return retained

    service, access, actor, principal = _reader(SimpleNamespace(read=read))
    with pytest.raises(CreationRefused, match="local_authority"):
        await service.read(uuid4(), authentication=actor, principal=principal)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [CreationRefused("custody_digest"), RuntimeError("case_store_unavailable")])
async def test_case_read_preserves_storage_errors(error: Exception) -> None:
    service, _access, actor, principal = _reader(SimpleNamespace(read=AsyncMock(side_effect=error)))
    with pytest.raises(type(error), match=str(error)):
        await service.read(uuid4(), authentication=actor, principal=principal)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True], ids=["new-admission", "retained-replay"])
@pytest.mark.parametrize("revoke", [False, True], ids=["current", "revoked-during-read"])
async def test_submission_case_reply_requires_current_submit_authority(
    monkeypatch: pytest.MonkeyPatch, *, existing: bool, revoke: bool
) -> None:
    request = InvestigationRequest(
        request_id=uuid4(),
        public_material=True,
        repository="https://example.invalid/public-source",
        base_revision="declared-base",
        tree_or_patch_digest="a" * 40,
        question="What statement is present?",
        non_goals=(),
        omissions=(),
        sources=(Source(name="one.py", text="pass\n"),),
        criteria=(Criterion(id="support", predicate="Cite the supplied statement."),),
    )
    retained = CaseView(
        case_id=request.request_id,
        request=request,
        request_digest="a" * 64,
        profile={},
        profile_digest="b" * 64,
        deadline="2030-01-01T00:00:00+00:00",
        status="reserved",
        slot_encumbered=True,
        admissions=({"admission_id": str(request.request_id), "run_id": str(uuid4())},),
        attempts=(),
        candidate=None,
        candidate_digest=None,
        verdict=None,
        reason=None,
    )
    reads = 0

    async def read(_case_id: object) -> CaseView:
        nonlocal reads
        reads += 1
        if reads == 1:
            if existing:
                return retained
            code = "unknown_case"
            raise CreationRefused(code)
        await asyncio.sleep(0)
        if revoke:
            access.revoke()
        return retained

    service, access, actor, _principal = _reader(
        SimpleNamespace(read=read, reserve=AsyncMock(return_value=request.request_id))
    )
    monkeypatch.setattr(service, "profile", dict)
    monkeypatch.setattr(service, "_submit_run", AsyncMock())
    service.registry = SimpleNamespace(
        refresh_capability_state=AsyncMock(return_value=SimpleNamespace(phase=CapabilityPhase.WARM))
    )
    sessions = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id="inert-session")))
    # Submit keeps its original action scope; no extra read scope is inferred.
    principal = Sigil(name="case-submitter", scopes=frozenset({"runs:submit"}))
    if revoke:
        with pytest.raises(CreationRefused, match="local_authority"):
            await service.submit(request, authentication=actor, principal=principal, engine=None, sessions=sessions)
    else:
        assert (
            await service.submit(request, authentication=actor, principal=principal, engine=None, sessions=sessions)
        ) is retained
    assert reads == 2
