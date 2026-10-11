from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from lychd.domain.animation.capabilities import (
    CapabilityGrant,
    CapabilityPhase,
    CapabilitySpec,
    CapabilityState,
    GrantLease,
    SourceKind,
)
from lychd.domain.animation.schemas.capability_family import CapabilityFamily
from lychd.domain.cortex.leases import AnimatorAdmission, LeaseAdmissionChanged, LeaseAdmissionClosed, LeaseLedger


def _grant(*, grant_id: str, animator_name: str = "titan") -> CapabilityGrant:
    spec = CapabilitySpec(
        key=f"{animator_name}:chat:model",
        animator_name=animator_name,
        runtime="llamacpp",
        source_kind=SourceKind.SOULSTONE,
        family=CapabilityFamily.CHAT,
        model_id="model",
    )
    state = CapabilityState(
        capability_key=spec.key,
        phase=CapabilityPhase.WARM,
    )
    return CapabilityGrant(
        spec=spec,
        state=state,
        lease=GrantLease(grant_id=grant_id, holder="run:r1", issued_at=datetime.now(UTC)),
        model=None,
    )


def test_acquire_then_active_by_animator() -> None:
    ledger = LeaseLedger()
    ledger.acquire(_grant(grant_id="g1", animator_name="titan"), priority=50, admission_generation=0)

    rows = ledger.active(animator_name="titan")
    assert len(rows) == 1
    assert rows[0].grant_id == "g1"
    assert rows[0].capability_key == "titan:chat:model"
    assert ledger.active(animator_name="coding") == []
    assert len(ledger.active()) == 1


def test_duplicate_grant_id_raises() -> None:
    ledger = LeaseLedger()
    ledger.acquire(_grant(grant_id="dup"), priority=50, admission_generation=0)
    with pytest.raises(RuntimeError) as exc_info:
        ledger.acquire(_grant(grant_id="dup"), priority=50, admission_generation=0)
    assert not isinstance(exc_info.value, LeaseAdmissionClosed)


def test_release_is_idempotent() -> None:
    ledger = LeaseLedger()
    ledger.acquire(_grant(grant_id="g1"), priority=50, admission_generation=0)
    ledger.release("g1")
    ledger.release("g1")  # no raise
    ledger.release("never-existed")  # no raise
    assert ledger.active() == []


def test_drain_admission_refuses_new_lease_then_reopens() -> None:
    ledger = LeaseLedger()
    existing = _grant(grant_id="existing", animator_name="titan")
    before_drain = ledger.admission_generation("titan")
    ledger.acquire(existing, priority=50, admission_generation=before_drain)

    ledger.begin_drain(["titan"])

    assert ledger.admission("titan") is AnimatorAdmission.DRAINING
    assert ledger.active(animator_name="titan")[0].grant_id == "existing"
    with pytest.raises(LeaseAdmissionClosed, match="is draining") as exc_info:
        ledger.acquire(_grant(grant_id="new", animator_name="titan"), priority=50, admission_generation=before_drain)
    assert exc_info.value.animator_name == "titan"
    ledger.acquire(_grant(grant_id="other", animator_name="coding"), priority=50, admission_generation=0)

    ledger.end_drain(["titan"])

    assert ledger.admission("titan") is AnimatorAdmission.OPEN
    with pytest.raises(LeaseAdmissionChanged, match="admission changed") as changed:
        ledger.acquire(_grant(grant_id="new", animator_name="titan"), priority=50, admission_generation=before_drain)
    assert changed.value.animator_name == "titan"
    ledger.acquire(
        _grant(grant_id="new", animator_name="titan"),
        priority=50,
        admission_generation=ledger.admission_generation("titan"),
    )


@pytest.mark.asyncio
async def test_drained_true_immediately_when_clear() -> None:
    ledger = LeaseLedger()
    assert await ledger.drained(["titan"], timeout=5.0) is True


@pytest.mark.asyncio
async def test_drained_wakes_on_release() -> None:
    ledger = LeaseLedger()
    ledger.acquire(_grant(grant_id="g1", animator_name="titan"), priority=50, admission_generation=0)

    async def _release_soon() -> None:
        await asyncio.sleep(0.01)
        ledger.release("g1")

    task = asyncio.create_task(_release_soon())
    assert await ledger.drained(["titan"], timeout=5.0) is True
    await task


@pytest.mark.asyncio
async def test_drained_false_on_timeout_with_survivor() -> None:
    ledger = LeaseLedger()
    ledger.acquire(_grant(grant_id="g1", animator_name="titan"), priority=50, admission_generation=0)
    assert await ledger.drained(["titan"], timeout=0.05) is False


@pytest.mark.asyncio
async def test_drain_handoff_is_absent_when_clear_and_exact_when_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = LeaseLedger()
    loop = asyncio.get_running_loop()
    now = loop.time()
    monkeypatch.setattr(loop, "time", lambda: now)
    cutoffs: list[float] = []
    assert await ledger.drained(["titan"], timeout=5, on_wait_deadline=cutoffs.append)
    assert cutoffs == []
    ledger.acquire(_grant(grant_id="g1"), priority=50, admission_generation=0)
    draining = asyncio.create_task(ledger.drained(["titan"], timeout=5, on_wait_deadline=cutoffs.append))
    await asyncio.sleep(0)
    assert cutoffs == [now + 5]
    # Release and resume before the native timeout callback gets a turn. The
    # nominal deadline still wins over newly empty accounting.
    now += 5
    ledger.release("g1")
    assert await draining is False
    assert ledger.active() == []
