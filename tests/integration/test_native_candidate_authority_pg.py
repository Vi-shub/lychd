"""Declared real PostgreSQL/current-authority passage. Only Lead runs this profile."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Table, event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from lychd.compositions.creation.native_candidate import (
    NativeAssessment,
    NativeBinding,
    NativeDecision,
    NativeObservation,
    NativeRequest,
    NativeView,
)
from lychd.compositions.creation.schemas import CreationRefused, canonical, digest
from lychd.db.creation import DbCreationStore
from lychd.db.models.creation import (
    CreationAdmission,
    CreationAttempt,
    CreationCase,
    CreationFeedback,
    CreationSuccessor,
)
from lychd.db.models.ward import WardAudit, WardPermission, WardPrincipal, WardSession
from lychd.db.ward import DbWardStore
from lychd.domain.artifact_custody import Continuity, CustodyRefused
from lychd.domain.ward.service import Action, Authentication, Operation, PrincipalKind, WardRefusedError, WardService
from lychd.system.services import artifact_custody as physical
from lychd.system.services.native_candidate import NativeCandidateService
from tests.integration import test_ward_pg as ward_proof
from tests.unit.system.services.test_native_candidate_authority import paths, plan

pytestmark = [pytest.mark.integration, pytest.mark.container]
pg_url = ward_proof.pg_url
ISSUER = uuid4()
CREATION_TABLES = cast(
    "list[Table]",
    [
        model.__table__
        for model in (
            CreationCase,
            CreationAdmission,
            CreationAttempt,
            CreationFeedback,
            CreationSuccessor,
        )
    ],
)


@pytest_asyncio.fixture
async def native_factory(pg_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_url)
    tables = ward_proof.TABLES + CREATION_TABLES
    async with engine.begin() as connection:
        await connection.run_sync(WardPrincipal.metadata.drop_all, tables=tables)
        await connection.run_sync(WardPrincipal.metadata.create_all, tables=tables)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def setup(
    root: Path,
    factory: async_sessionmaker[AsyncSession],
) -> tuple[NativeCandidateService, DbCreationStore, WardService, NativeRequest, Authentication, Authentication]:
    dbward = DbWardStore(factory, issuer_id=ISSUER)
    ward = WardService(dbward)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    proposer = await ward.enroll(PrincipalKind.HUMAN, expires_at=expiry)
    reviewer = await ward.enroll(PrincipalKind.HUMAN, expires_at=expiry)
    first = await ward.open_session(proposer.credential, expires_at=expiry - timedelta(minutes=1))
    second = await ward.open_session(reviewer.credential, expires_at=expiry - timedelta(minutes=1))
    known, content = plan(proposer.principal_id, reviewer.principal_id)
    # Even giving the proposer ASSESS cannot bypass the independently assigned role.
    permissions = (
        (Action.ADMIT, "native:import"),
        (Action.ADMIT, "native:structure"),
        (Action.ADMIT, "native:bind"),
        (Action.INSPECT, "native:inspect"),
        (Action.READ, "native:bytes"),
        (Action.READ, "native:view"),
        (Action.DECIDE, "native:assess"),
        (Action.DECIDE, "native:decide"),
    )
    generations: list[tuple[UUID, int]] = []
    for identity in (proposer.principal_id, reviewer.principal_id):
        generation = 1
        for action, purpose in permissions:
            generation = await ward.assign(identity, Operation(action, known.candidate_id, purpose), expires_at=expiry)
        generations.append((identity, generation))
    known = known.model_copy(update={"policy_generations": tuple(generations)})
    creation = DbCreationStore(factory, policy=None)
    host = NativeCandidateService.create(root, ward=dbward, creation=creation, plan=known, content=content)
    request = NativeRequest(case_id=uuid4(), admission_id=uuid4(), plan=known, deadline=expiry)
    return host, creation, ward, request, first, second


def generation(request: NativeRequest, authentication: Authentication) -> int:
    return dict(request.plan.policy_generations)[authentication.principal_id]


async def bound(host: NativeCandidateService, request: NativeRequest, actor: Authentication) -> NativeBinding:
    current = generation(request, actor)
    await host.import_candidate(actor.secret, expected_policy_generation=current)
    await host.bind_inactive_case(actor.secret, request, expected_policy_generation=current)
    views: list[NativeView] = []
    await host.project_case(
        actor.secret,
        request,
        expected_policy_generation=current,
        publication=lambda value, _attribution: views.append(value),
    )
    return views[-1].binding


def assessment(binding: NativeBinding) -> NativeAssessment:
    return NativeAssessment(
        assessment_id=uuid4(),
        target_digest=binding.digest,
        report="Exact selected bytes inspected by the assigned reviewer. Compatibility was not executed.",
        limitations=("No constructor containment or compatibility receipt",),
        observations=(NativeObservation(criterion="exact-bytes", outcome="unknown", receipt_digest=None),),
    )


@pytest.mark.asyncio
async def test_independent_attributed_hold_reject_replay_restart_and_legacy_refusal(
    native_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    host, creation, _ward, request, proposer, reviewer = await setup(tmp_path, native_factory)
    try:
        binding = await bound(host, request, proposer)
        review = assessment(binding)
        with pytest.raises(CreationRefused, match="authority"):
            await host.append_assessment(
                proposer.secret, request, review, expected_policy_generation=generation(request, proposer)
            )
        changed = review.model_copy(update={"target_digest": "sha256:" + "0" * 64})
        with pytest.raises(CreationRefused, match="authority"):
            await host.append_assessment(
                reviewer.secret, request, changed, expected_policy_generation=generation(request, reviewer)
            )
        await host.append_assessment(
            reviewer.secret, request, review, expected_policy_generation=generation(request, reviewer)
        )
        hold = NativeDecision(
            decision_id=uuid4(),
            target_digest=binding.digest,
            assessment_id=review.assessment_id,
            assessment_digest=digest(canonical(review)),
            action="hold",
            reason="Containment and mandatory compatibility evidence remain unavailable.",
        )
        await host.decide_inactive(
            proposer.secret, request, hold, expected_policy_generation=generation(request, proposer)
        )
        await host.decide_inactive(
            proposer.secret, request, hold, expected_policy_generation=generation(request, proposer)
        )
        conflict = hold.model_copy(update={"reason": "Different semantic attempt"})
        with pytest.raises(CreationRefused, match="replay"):
            await host.decide_inactive(
                proposer.secret, request, conflict, expected_policy_generation=generation(request, proposer)
            )
        rejected = hold.model_copy(update={"decision_id": uuid4(), "action": "reject", "reason": "Proposal refused."})
        await host.decide_inactive(
            proposer.secret, request, rejected, expected_policy_generation=generation(request, proposer)
        )
        async with native_factory() as session:
            case = await session.get(CreationCase, request.case_id)
            assert case is not None
            assert case.slot is None
            assert case.verdict_bytes is None
            assert (
                await session.scalar(select(CreationAttempt.id).where(CreationAttempt.case_id == request.case_id))
                is None
            )
            records = list(
                await session.scalars(
                    select(CreationFeedback)
                    .where(CreationFeedback.case_id == request.case_id)
                    .order_by(CreationFeedback.sequence)
                )
            )
            assert len(records) == 3
            assert str(reviewer.principal_id).encode() in records[0].attribution_bytes
            assert b"independent-reviewer" in records[0].attribution_bytes
            assert str(proposer.principal_id).encode() in records[1].attribution_bytes
        with pytest.raises(CreationRefused, match="native_owner"):
            await creation.read(request.case_id)
        root_binding = host.binding
        await host.close()
        _, content = plan(request.plan.proposer, request.plan.verifier)
        reopened = NativeCandidateService.open(
            tmp_path,
            ward=DbWardStore(native_factory, issuer_id=ISSUER),
            creation=creation,
            plan=request.plan,
            content=content,
            binding=root_binding,
            record=binding.record,
            continuity=Continuity.CONTINUOUS,
        )
        try:
            views: list[NativeView] = []
            await reopened.project_case(
                proposer.secret,
                request,
                expected_policy_generation=generation(request, proposer),
                publication=lambda value, _context: views.append(value),
            )
            assert [entry.body.action for entry in views[-1].entries if isinstance(entry.body, NativeDecision)] == [
                "hold",
                "reject",
            ]
        finally:
            await reopened.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_database_clock_expiry_after_real_stage_prevents_publication(
    native_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, _creation, _ward, request, proposer, _reviewer = await setup(tmp_path, native_factory)
    original_decision = DbWardStore._decision_sync  # pyright: ignore[reportPrivateUsage]
    original_stage = physical.publication.stage_bytes_file
    observed: list[Session] = []
    staged: list[bool] = []

    def decision(self: DbWardStore, session: Session, *arguments: Any) -> Any:
        observed.append(session)
        return original_decision(self, session, *arguments)

    def stage(*arguments: Any, **keywords: Any) -> Any:
        result = original_stage(*arguments, **keywords)
        staged.append(True)
        session = observed[-1]
        session.execute(
            update(WardPermission)
            .where(
                WardPermission.principal_id == proposer.principal_id,
                WardPermission.object_id == request.plan.candidate_id,
                WardPermission.purpose == "native:import",
            )
            .values(expires_at=func.clock_timestamp() + text("interval '0.03 second'"))
        )
        session.execute(text("SELECT pg_sleep(0.06)"))
        return result

    monkeypatch.setattr(DbWardStore, "_decision_sync", decision)
    monkeypatch.setattr(physical.publication, "stage_bytes_file", stage)
    try:
        with pytest.raises(CustodyRefused, match="denied"):
            await host.import_candidate(proposer.secret, expected_policy_generation=generation(request, proposer))
        assert staged == [True]
        assert {path.name for path in paths(tmp_path)} == {"custody.lock", "custody.root"}
        async with native_factory() as session:
            assert await session.scalar(select(WardAudit.id).where(WardAudit.outcome == "unknown")) is not None
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_actual_principal_lock_wait_observes_revocation_and_shutdown_serializes(
    native_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    host, _creation, _ward, request, proposer, _reviewer = await setup(tmp_path, native_factory)
    task: asyncio.Task[None] | None = None
    closing: asyncio.Task[None] | None = None
    try:
        async with native_factory() as blocker, blocker.begin():
            blocking_pid = await blocker.scalar(select(func.pg_backend_pid()))
            await blocker.scalar(
                select(WardPrincipal).where(WardPrincipal.id == proposer.principal_id).with_for_update()
            )
            task = asyncio.create_task(
                host.import_candidate(proposer.secret, expected_policy_generation=generation(request, proposer))
            )
            async with native_factory() as observer:
                waiting = False
                for _ in range(100):
                    waiting = await observer.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))"),
                        {"pid": blocking_pid},
                    )
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
                assert waiting is True
            closing = asyncio.create_task(host.close())
            await asyncio.sleep(0)
            assert not closing.done()
            await blocker.execute(
                update(WardSession)
                .where(WardSession.id == proposer.session_id)
                .values(
                    revoked_at=func.clock_timestamp(),
                    generation=WardSession.generation + 1,
                )
            )
        with pytest.raises(WardRefusedError):
            await task
        await closing
        assert {path.name for path in paths(tmp_path)} == {"custody.lock", "custody.root"}
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if closing is not None:
            await closing
        await host.close()


@pytest.mark.asyncio
async def test_lost_decision_commit_ack_reconciles_exact_retained_identity(
    native_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, _creation, _ward, request, proposer, reviewer = await setup(tmp_path, native_factory)
    binding = await bound(host, request, proposer)
    review = assessment(binding)
    await host.append_assessment(
        reviewer.secret, request, review, expected_policy_generation=generation(request, reviewer)
    )
    body = NativeDecision(
        decision_id=uuid4(),
        target_digest=binding.digest,
        assessment_id=review.assessment_id,
        assessment_digest=digest(canonical(review)),
        action="hold",
        reason="Missing mandatory evidence.",
    )
    original = DbCreationStore._native_feedback  # pyright: ignore[reportPrivateUsage]
    pending = True

    def append(self: DbCreationStore, session: Session, *arguments: Any) -> None:
        nonlocal pending
        original(self, session, *arguments)
        if pending:
            session.info["native_test_lose_ack"] = True
            pending = False

    def after_commit(session: Session) -> None:
        if session.info.pop("native_test_lose_ack", False):
            message = "Lost native decision acknowledgement after actual database commit."
            raise ConnectionError(message)

    monkeypatch.setattr(DbCreationStore, "_native_feedback", append)
    event.listen(Session, "after_commit", after_commit)
    try:
        with pytest.raises(ConnectionError, match="after actual database commit"):
            await host.decide_inactive(
                proposer.secret, request, body, expected_policy_generation=generation(request, proposer)
            )
        async with native_factory() as observer:
            retained = await observer.get(CreationFeedback, body.decision_id)
            assert retained is not None
            assert retained.body_bytes == canonical(body)
        await host.decide_inactive(
            proposer.secret, request, body, expected_policy_generation=generation(request, proposer)
        )
        async with native_factory() as observer:
            decisions = list(
                await observer.scalars(
                    select(CreationFeedback).where(
                        CreationFeedback.case_id == request.case_id, CreationFeedback.kind == "decision"
                    )
                )
            )
            assert len(decisions) == 1
            assert decisions[0].id == body.decision_id
    finally:
        event.remove(Session, "after_commit", after_commit)
        await host.close()


@pytest.mark.asyncio
async def test_native_creation_mapper_table_and_prebound_connection_escapes_refuse_before_effect(
    native_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Includes AUTOCOMMIT alternates and same-engine explicit Connection escapes."""
    preparation = tmp_path / "preparation"
    preparation.mkdir(mode=0o700)
    host, _creation, _ward, request, proposer, _reviewer = await setup(preparation, native_factory)
    await host.close()
    _, content = plan(request.plan.proposer, request.plan.verifier)
    admitted = native_factory.kw["bind"]
    async with native_factory() as observer:
        audit_count = await observer.scalar(select(func.count()).select_from(WardAudit))
    occurrence = 0
    for isolation in ("READ COMMITTED", "AUTOCOMMIT"):
        alternate = create_async_engine(admitted.url, isolation_level=isolation)
        try:
            for model in (CreationCase, CreationAdmission, CreationFeedback):
                # The connection escape uses A itself: equal engine is insufficient.
                async with admitted.connect() as prebound:
                    for route in ("mapper", "table", "masked-table", "connection"):
                        occurrence += 1
                        root = tmp_path / str(occurrence)
                        root.mkdir(mode=0o700)
                        binds: dict[Any, Any] = {model: alternate}
                        if route == "table":
                            binds = {model.__table__: alternate}
                        elif route == "masked-table":
                            binds = {model: admitted, model.__table__: alternate}
                        elif route == "connection":
                            binds = {model: prebound}
                        factory = async_sessionmaker(admitted, binds=binds, expire_on_commit=False)
                        consumer = NativeCandidateService.create(
                            root,
                            ward=DbWardStore(factory, issuer_id=ISSUER),
                            creation=DbCreationStore(factory, policy=None),
                            plan=request.plan,
                            content=content,
                        )
                        before = paths(root)
                        try:
                            with pytest.raises(WardRefusedError, match="exact AsyncEngine"):
                                await consumer.import_candidate(
                                    proposer.secret,
                                    expected_policy_generation=generation(request, proposer),
                                )
                            with pytest.raises(WardRefusedError, match="exact AsyncEngine"):
                                await consumer.bind_inactive_case(
                                    proposer.secret,
                                    request,
                                    expected_policy_generation=generation(request, proposer),
                                )
                            assert paths(root) == before
                        finally:
                            await consumer.close()
        finally:
            await alternate.dispose()
    async with native_factory() as observer:
        assert await observer.scalar(select(func.count()).select_from(WardAudit)) == audit_count
        assert await observer.get(CreationCase, request.case_id) is None
