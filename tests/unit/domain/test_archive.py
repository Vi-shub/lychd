"""Archive's explicit subject, namespace, bounds and pre-storage authority floor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lychd.domain.archive import (
    ArchiveAuthorization,
    ArchiveProfile,
    ArchiveRefused,
    ArchiveService,
    ArchiveSubject,
    NoteProposal,
    RecallRequest,
    text_digest,
)

SUBJECT = ArchiveSubject(identity="local-operator")
PROFILE = ArchiveProfile(namespace_id=uuid4(), owner_subject=SUBJECT, policy_revision="public-notes.1")


def proposal(**updates: Any) -> NoteProposal:
    values: dict[str, Any] = {
        "namespace_id": PROFILE.namespace_id,
        "request_id": uuid4(),
        "source_key": "operator-note",
        "source_revision": "1",
        "source_digest": text_digest("red fox"),
        "text": "red fox",
        "producer": "operator",
        "observed_at": datetime(2026, 10, 11, tzinfo=UTC),
    }
    values.update(updates)
    return NoteProposal(**values)


class Authority:
    def __init__(self) -> None:
        self.allowed = True
        self.calls: list[ArchiveAuthorization] = []

    def authorize(self, request: ArchiveAuthorization) -> bool:
        self.calls.append(request)
        return self.allowed and request.subject == SUBJECT and request.namespace_id == PROFILE.namespace_id


@pytest.mark.parametrize(
    "updates",
    [
        {"classification": "private"},
        {"text": "other"},
        {"text": ""},
        {"text": "\x00"},
        {"observed_at": datetime(2026, 10, 11, tzinfo=UTC).replace(tzinfo=None)},
        {"source_key": "bad\x00key"},
        {"unknown": "input"},
    ],
)
def test_note_rejects_unadmitted_material(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        proposal(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"max_results": 21},
        {"char_budget": 32_001},
        {"min_score": float("nan")},
        {"query": "\x00"},
        {"query": " "},
        {"query": "x" * 2_001},
    ],
)
def test_recall_has_finite_validated_inputs(updates: dict[str, Any]) -> None:
    values = {"namespace_id": PROFILE.namespace_id, "request_id": uuid4(), "query": "fox", **updates}
    with pytest.raises(ValidationError):
        RecallRequest.model_validate(values)


@pytest.mark.asyncio
async def test_missing_authority_never_reaches_storage() -> None:
    authority = Authority()
    authority.allowed = False
    store = AsyncMock()
    service = ArchiveService(profile=PROFILE, store=store, authorizer=authority)
    with pytest.raises(ArchiveRefused, match="authority"):
        await service.admit(SUBJECT, proposal(), purpose="retain-note")
    store.admit.assert_not_called()


@pytest.mark.asyncio
async def test_namespace_and_arbitrary_identity_do_not_supply_authority() -> None:
    store = AsyncMock()
    authority = Authority()
    service = ArchiveService(profile=PROFILE, store=store, authorizer=authority)
    with pytest.raises(ArchiveRefused, match="namespace"):
        await service.admit(SUBJECT, proposal(namespace_id=uuid4()), purpose="retain-note")
    assert authority.calls == []
    with pytest.raises(ArchiveRefused, match="authority"):
        await service.admit(ArchiveSubject(identity="magus"), proposal(), purpose="retain-note")
    store.admit.assert_not_called()


@pytest.mark.asyncio
async def test_store_receives_a_current_check_not_a_reusable_grant() -> None:
    authority = Authority()
    store = AsyncMock()
    service = ArchiveService(profile=PROFILE, store=store, authorizer=authority)
    note = proposal()
    from lychd.domain.archive import Lifecycle, NoteRecord, RecordRef

    store.admit.return_value = NoteRecord(
        namespace_id=PROFILE.namespace_id,
        reference=RecordRef(record_id=uuid4(), revision_digest="1" * 64, lifecycle_version=1),
        source_key=note.source_key,
        source_revision=note.source_revision,
        source_digest=note.source_digest,
        producer=note.producer,
        observed_at=note.observed_at,
        lifecycle=Lifecycle.CANDIDATE,
        created_at=datetime.now(UTC),
    )
    await service.admit(SUBJECT, note, purpose="retain-note")
    check = store.admit.call_args.args[2]
    authority.allowed = False
    with pytest.raises(ArchiveRefused, match="authority"):
        check(note.request_id, text_digest(note.text))
    request = authority.calls[-1]
    assert (request.subject, request.owner_subject, request.action, request.purpose, request.policy_revision) == (
        SUBJECT,
        SUBJECT,
        "admit",
        "retain-note",
        PROFILE.policy_revision,
    )
    assert note.text not in request.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_on_exit", [False, True])
async def test_resolve_rechecks_current_authority_after_store_handoff(*, revoke_on_exit: bool) -> None:
    """A store's final SQL check cannot cover an awaited transaction/session exit."""
    from lychd.domain.archive import RecallProjection, RecallReceipt

    authority = Authority()
    receipt = RecallReceipt(
        receipt_id=uuid4(),
        request_id=uuid4(),
        namespace_id=PROFILE.namespace_id,
        subject=SUBJECT,
        purpose="answer",
        policy_revision=PROFILE.policy_revision,
        query_digest="1" * 64,
        request_digest="2" * 64,
        run_id=None,
        max_results=1,
        char_budget=100,
        min_score=0.0,
        selected=(),
        omitted_for_budget=0,
        created_at=datetime.now(UTC),
        receipt_digest="3" * 64,
    )
    projection = RecallProjection(receipt=receipt, items=())

    async def exit_store(*args: Any) -> RecallProjection:
        check = args[-1]
        check(receipt.receipt_id, receipt.receipt_digest)
        # Equivalent to a revocation while transaction or connection cleanup awaits.
        authority.allowed = not revoke_on_exit
        return projection

    store = AsyncMock()
    store.resolve.side_effect = exit_store
    archive = ArchiveService(profile=PROFILE, store=store, authorizer=authority)
    if revoke_on_exit:
        with pytest.raises(ArchiveRefused, match="authority"):
            await archive.resolve_receipt(SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose="answer")
    else:
        assert (
            await archive.resolve_receipt(SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose="answer")
            is projection
        )
    assert len(authority.calls) == 3
    assert authority.calls[-1].object_id == receipt.receipt_id
    assert authority.calls[-1].binding_digest == receipt.receipt_digest
