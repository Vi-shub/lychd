"""Successor intake and replay authority without a database, provider or queue."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lychd.compositions.creation.schemas import (
    CaseView,
    CreationRefused,
    LineageEntry,
    OwnerDecision,
    SuccessorRequest,
    canonical,
    digest,
    feedback_target,
)
from lychd.domain.codex.sigil import Sigil, default_local_sigil
from tests.support.creation import assessment_document, request_document, successor_document
from tests.unit.compositions.test_creation import StageStore, stage_fixture


def packet() -> SuccessorRequest:
    assessment = assessment_document(feedback_target(canonical(request_document()), b"{}", b"{}", b"{}"))
    decision = OwnerDecision(
        decision_id=uuid4(),
        assessment_id=assessment.assessment_id,
        assessment_digest=digest(canonical(assessment)),
        target=assessment.target,
        action="hold",
        reason="Retain gap.",
    )
    return successor_document(assessment, decision)


def entry(request: SuccessorRequest) -> LineageEntry:
    return LineageEntry(
        parent_case_id=uuid4(),
        child_case_id=request.request.request_id,
        body_digest=digest(canonical(request)),
        body=request,
        attribution={"authentication_generation": str(uuid4())},
        created_at="2026-10-17T00:00:00+00:00",
    )


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_successor_requires_literal_explicit_authorization(value: Any) -> None:
    body = packet().model_dump(mode="json")
    body["authorize_one_child"] = value
    with pytest.raises((CreationRefused, ValidationError)):
        SuccessorRequest.model_validate(body)


@pytest.mark.parametrize("value", ["", "   ", "č" * 257])
def test_successor_hypothesis_has_nonempty_utf8_bound(value: str) -> None:
    body = packet().model_dump(mode="json")
    body["changed_hypothesis"] = value
    with pytest.raises((CreationRefused, ValidationError), match="text_bounds"):
        SuccessorRequest.model_validate(body)


def test_successor_canonical_outer_bound_includes_json_escaping() -> None:
    body = packet().model_dump(mode="json")
    body["request"]["sources"][0]["text"] = "\x01" * 3000
    with pytest.raises((CreationRefused, ValidationError), match="successor_body_bytes"):
        SuccessorRequest.model_validate(body)


@pytest.mark.asyncio
async def test_completed_successor_replay_keeps_attribution_without_readiness_or_session() -> None:
    service, _common = stage_fixture(StageStore())
    request = packet()
    retained = entry(request)
    child = cast("CaseView", service.store.view).model_copy(update={"case_id": retained.child_case_id})
    store = SimpleNamespace(
        successor_replay=AsyncMock(return_value=retained),
        successor_publication_required=AsyncMock(return_value=False),
        read=AsyncMock(return_value=child),
        reserve_successor=AsyncMock(),
    )
    service.store = store
    service.registry = SimpleNamespace(get_capability=Mock(side_effect=AssertionError("no current profile")))
    sessions, engine = SimpleNamespace(create_session=AsyncMock()), SimpleNamespace(submit=AsyncMock())
    actor = service.policy.access.authenticate(username=b"magus", password=b"inert-test-password")
    result = await service.successor(
        retained.parent_case_id,
        request,
        authentication=actor,
        principal=default_local_sigil(),
        engine=engine,
        sessions=sessions,
    )
    assert result.lineage == retained
    assert result.child == child
    store.read.assert_awaited_once_with(retained.child_case_id, expire=False)
    sessions.create_session.assert_not_awaited()
    engine.submit.assert_not_awaited()
    store.reserve_successor.assert_not_awaited()
    service.policy.access.revoke()
    with pytest.raises(CreationRefused, match="local_authority"):
        await service.successor(
            retained.parent_case_id,
            request,
            authentication=actor,
            principal=default_local_sigil(),
            engine=engine,
            sessions=sessions,
        )
    assert store.successor_replay.await_count == 1


@pytest.mark.asyncio
async def test_held_replay_rechecks_original_execution_authority_after_binding_wait() -> None:
    service, _common = stage_fixture(StageStore())
    request, run_id = packet(), str(uuid4())
    retained = entry(request)
    actor = service.policy.access.authenticate(username=b"magus", password=b"inert-test-password")
    assert actor is not None
    binding = service.binding(
        case_id=retained.child_case_id,
        admission_id=retained.child_case_id,
        session_id=str(uuid4()),
        authentication=actor,
        principal=default_local_sigil(),
    )
    released: list[str] = []

    async def bind(_admission: Any, _run: str) -> None:
        service.policy.access.revoke()

    async def submit(_intent: Any, **kwargs: Any) -> None:
        assert kwargs["idempotency_key"] == f"creation:{retained.child_case_id}"
        await kwargs["retain_before_publish"](run_id)
        released.append(run_id)

    service.store = SimpleNamespace(
        successor_replay=AsyncMock(return_value=retained),
        successor_publication_required=AsyncMock(return_value=True),
        admission=AsyncMock(return_value={**binding, "run_id": run_id}),
        bind_run=AsyncMock(side_effect=bind),
    )
    with pytest.raises(CreationRefused, match="current_binding"):
        await service.successor(
            retained.parent_case_id,
            request,
            authentication=actor,
            principal=default_local_sigil(),
            engine=SimpleNamespace(submit=submit),
            sessions=None,
        )
    assert released == []
    service.store.bind_run.assert_awaited_once_with(retained.child_case_id, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", ["readiness", "session"])
async def test_fresh_successor_rechecks_authority_after_preparation_wait(wait: str) -> None:
    service, _common = stage_fixture(StageStore())
    request = packet()
    service.store = SimpleNamespace(successor_replay=AsyncMock(return_value=None))

    async def reserve(_case: Any, _body: Any, prepare: Any, **_kwargs: Any) -> Any:
        return await prepare(object())

    service.store.reserve_successor = reserve
    actor = service.policy.access.authenticate(username=b"magus", password=b"inert-test-password")
    original_refresh = service.registry.refresh_capability_state

    async def refresh(key: str) -> Any:
        state = await original_refresh(key)
        if wait == "readiness":
            service.policy.access.revoke()
        return state

    async def create(_transaction: Any, **_kwargs: Any) -> Any:
        service.policy.access.revoke()
        return SimpleNamespace(id=str(uuid4()))

    service.registry.refresh_capability_state = refresh
    sessions = SimpleNamespace(create_session_in_transaction=AsyncMock(side_effect=create))
    with pytest.raises(CreationRefused, match="local_authority"):
        await service.successor(
            uuid4(), request, authentication=actor, principal=default_local_sigil(), engine=None, sessions=sessions
        )
    assert sessions.create_session_in_transaction.await_count == (wait == "session")


@pytest.mark.parametrize(("action", "scope"), [("successor", "runs:submit"), ("lineage", "altar:read")])
def test_successor_actions_require_the_actual_marker_and_action_scope(action: str, scope: str) -> None:
    service, _common = stage_fixture(StageStore())
    actor = service.policy.access.authenticate(username=b"magus", password=b"inert-test-password")
    assert service.policy.http(actor, Sigil(name="scoped", scopes=frozenset({scope})), action) is actor
    with pytest.raises(CreationRefused, match="local_authority"):
        service.policy.http(actor, Sigil(name="scoped", scopes=frozenset()), action)


def test_retired_execution_binding_is_readable_but_malformed_binding_is_not_repair_eligible() -> None:
    service, _common = stage_fixture(StageStore())
    actor = service.policy.access.authenticate(username=b"magus", password=b"inert-test-password")
    assert actor is not None
    case_id = uuid4()
    binding = service.binding(
        case_id=case_id,
        admission_id=case_id,
        session_id=str(uuid4()),
        authentication=actor,
        principal=default_local_sigil(),
    )
    assert service.policy.execution_binding_current(binding, case_id)
    service.policy.access.revoke()
    assert not service.policy.execution_binding_current(binding, case_id)
    changes: tuple[dict[str, Any], ...] = ({"case_id": str(uuid4())}, {"role": "scope-only"}, {"scopes": []})
    for change in changes:
        with pytest.raises(CreationRefused, match="current_binding"):
            service.policy.execution_binding_current({**binding, **change}, case_id)
