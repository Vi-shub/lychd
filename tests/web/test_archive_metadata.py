"""Guarded metadata transport has no source or execution work in its read path."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from litestar import Litestar
from litestar.connection import ASGIConnection
from litestar.datastructures import State
from litestar.middleware import DefineMiddleware
from litestar.middleware.authentication import AbstractAuthenticationMiddleware, AuthenticationResult

from lychd.domain.archive import (
    ArchiveProfile,
    ArchiveRefused,
    ArchiveService,
    ArchiveSubject,
    Lifecycle,
    NoteListPage,
    NoteRecord,
    RecordRef,
)
from lychd.domain.archive.contracts import ArchiveAuthorization, digest
from lychd.domain.codex.sigil import Sigil, default_local_sigil
from lychd.interface.web.archive import ArchiveController
from lychd.interface.web.archive_access import LocalArchivePolicy

pytestmark = pytest.mark.asyncio

PROFILE = ArchiveProfile(
    namespace_id=uuid4(), owner_subject=ArchiveSubject(identity="test-owner"), policy_revision="one"
)


def client(store: AsyncMock, *, configured: bool = True, caller: Sigil | None = None) -> httpx.AsyncClient:
    policy = LocalArchivePolicy(profile=PROFILE, caller=default_local_sigil())
    archive = ArchiveService(profile=PROFILE, store=store, authorizer=policy)

    class CallerMiddleware(AbstractAuthenticationMiddleware):
        async def authenticate_request(self, connection: ASGIConnection[Any, Any, Any, Any]) -> AuthenticationResult:
            _ = connection
            return AuthenticationResult(user=caller or default_local_sigil(), auth=None)

    services = SimpleNamespace(
        archive=archive if configured else None,
        substrate=SimpleNamespace(archive_subject_resolver=policy.subject_for if configured else None),
    )
    app = Litestar(
        route_handlers=[ArchiveController],
        middleware=[DefineMiddleware(CallerMiddleware)],
        state=State({"services": services}),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=cast("Any", app)), base_url="http://testserver.local")


def record() -> NoteRecord:
    return NoteRecord(
        namespace_id=PROFILE.namespace_id,
        reference=RecordRef(record_id=uuid4(), revision_digest="1" * 64, lifecycle_version=3),
        source_key="fox",
        source_revision="one",
        source_digest="2" * 64,
        producer="operator",
        observed_at=datetime.now(UTC),
        lifecycle=Lifecycle.REVOKED,
        created_at=datetime.now(UTC),
    )


async def test_profile_discovery_is_only_configuration() -> None:
    store = AsyncMock()
    async with client(store) as http:
        response = await http.get("/api/v1/archive/profile")
    assert response.status_code == 200
    assert response.json() == PROFILE.model_dump(mode="json")
    assert store.mock_calls == []


async def test_metadata_get_and_list_expose_current_tombstone_without_content() -> None:
    store = AsyncMock()
    note = record()
    store.get_note.return_value = note
    store.list_notes.return_value = NoteListPage(records=(note,))
    async with client(store) as http:
        response = await http.get("/api/v1/archive/notes", params={"limit": 2, "source_key": "fox"})
        exact = await http.get(f"/api/v1/archive/notes/{note.reference.record_id}")
    assert response.status_code == exact.status_code == 200
    assert response.json()["records"] == [exact.json()]
    assert exact.json()["lifecycle"] == "revoked"
    assert "text" not in exact.json()
    assert "content" not in exact.json()
    query = store.list_notes.call_args.args[1]
    assert (query.limit, query.source_key) == (2, "fox")


@pytest.mark.parametrize(
    "params", [{"limit": 51}, {"limit": 0}, {"cursor": "%"}, {"cursor": "x" * 2049}, {"source_key": "bad\x00key"}]
)
async def test_invalid_metadata_query_never_reaches_storage(params: dict[str, Any]) -> None:
    store = AsyncMock()
    async with client(store) as http:
        response = await http.get("/api/v1/archive/notes", params=params)
    assert response.status_code == 400
    assert store.mock_calls == []


@pytest.mark.parametrize("path", ["/profile", "/notes", f"/notes/{uuid4()}"])
async def test_metadata_unconfigured_and_wrong_caller_refuse(path: str) -> None:
    store = AsyncMock()
    async with client(store, configured=False) as http:
        assert (await http.get("/api/v1/archive" + path)).status_code == 404
    async with client(store, caller=Sigil(name="other", scopes=default_local_sigil().scopes)) as http:
        assert (await http.get("/api/v1/archive" + path)).status_code == 403
    async with client(store, caller=Sigil(name="magus", scopes=frozenset())) as http:
        assert (await http.get("/api/v1/archive" + path)).status_code == 403
    assert store.mock_calls == []


async def test_storage_failure_is_typed_and_distinct_from_an_empty_page() -> None:
    store = AsyncMock()
    store.list_notes.side_effect = ArchiveRefused("storage")
    async with client(store) as http:
        response = await http.get("/api/v1/archive/notes")
    assert response.status_code == 409
    assert response.json()["extra"]["code"] == "storage"


@pytest.mark.parametrize("action", ["profile_discover", "inspect"])
async def test_metadata_policy_needs_explicit_management_purpose(action: str) -> None:
    policy = LocalArchivePolicy(profile=PROFILE, caller=default_local_sigil())
    request = ArchiveAuthorization.model_validate(
        {
            "subject": PROFILE.owner_subject,
            "namespace_id": PROFILE.namespace_id,
            "owner_subject": PROFILE.owner_subject,
            "policy_revision": PROFILE.policy_revision,
            "action": action,
            "purpose": "archive:answer",
            "object_id": None,
            "binding_digest": digest(PROFILE),
        }
    )
    assert policy.authorize(request) is False
    assert policy.authorize(request.model_copy(update={"purpose": "archive:manage"})) is True


@pytest.mark.parametrize("operation", ["discover_profile", "get_note", "list_notes"])
async def test_recall_purpose_never_supplies_metadata_storage_authority(operation: str) -> None:
    from lychd.domain.archive import NoteListRequest

    store = AsyncMock()
    policy = LocalArchivePolicy(profile=PROFILE, caller=default_local_sigil())
    archive = ArchiveService(profile=PROFILE, store=store, authorizer=policy)
    args = {
        "discover_profile": (),
        "get_note": (uuid4(),),
        "list_notes": (NoteListRequest(namespace_id=PROFILE.namespace_id),),
    }
    with pytest.raises(ArchiveRefused, match="authority"):
        await getattr(archive, operation)(PROFILE.owner_subject, *args[operation], purpose="archive:answer")
    assert store.mock_calls == []
