"""The TransitionArbiter — one physical transition at a time (wave3 §5.2).

At most ONE physical transition runs at a time. Contenders are admitted by
``(-priority, arrival seq)`` (higher priority first, FIFO within a priority). A second
caller for the SAME capability key at the SAME priority coalesces onto the in-flight
plan. Different priorities remain separate contenders so a low-priority owner can
never cause a qualifying high-priority follower to be declined. v1 orders admission
only — there is NO preemption of an in-flight transition.
"""

from __future__ import annotations

import asyncio
import heapq
import math
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio

from lychd.domain.animation.errors import ActivationTimeout
from lychd.lib.asyncio import complete_under_cancellation

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator

    from lychd.domain.orchestration.schema import TransitionPlan

__all__ = ["TransitionAdmissionExpired", "TransitionAdmissionWaitExpired", "TransitionArbiter", "TransitionDeclined"]


class TransitionAdmissionExpired(RuntimeError):  # noqa: N818 - typed admission refusal
    """The owner could not admit its transition before the caller's cutoff."""

    code = "transition_admission_expired"

    def __init__(self) -> None:
        """Describe a pre-admission refusal without claiming physical submission."""
        super().__init__("Transition admission expired before invocation or readiness confirmation.")


class TransitionAdmissionWaitExpired(TransitionAdmissionExpired):
    """Only this follower's wait expired; the shared owner retains its authority."""

    code = "transition_admission_wait_expired"

    def __init__(self) -> None:
        """Keep the detached wait distinct from a shared no-effect refusal."""
        RuntimeError.__init__(self, "Transition admission wait expired; the shared transition may continue.")


class _TransitionAdmission:
    """Private cohort timing, independent of mutable projection traces.

    One timer wakes the owner at its earliest live cutoff. The nominal cutoff and
    cause are retained before cancellation enters registry or gate cleanup.
    """

    def __init__(self, deadline: float | None = None) -> None:
        self.loop = asyncio.get_running_loop()
        if deadline is not None:
            try:
                valid = not isinstance(deadline, bool) and math.isfinite(deadline)
            except (TypeError, OverflowError):
                valid = False
            if not valid:
                message = "admission_deadline must be a finite event-loop timestamp."
                raise ValueError(message)
            if self.loop.time() >= deadline:
                raise TransitionAdmissionExpired
        self.deadline = deadline
        self.admitted_at: float | None = None
        self.expired_at: float | None = None
        self.expired_error: Exception | None = None
        self.changed = asyncio.Event()
        self._phase: tuple[float, Exception] | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._task = asyncio.current_task()
        self._initial_cancels = self._task.cancelling() if self._task is not None else 0
        self._cancelled_by_deadline = False
        self._stopped = False

    def start(self) -> None:
        self._arm()

    def stop(self) -> None:
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def next_cutoff(self) -> float | None:
        selected = self._next()
        return selected[0] if selected is not None else None

    def _next(self) -> tuple[float, Exception] | None:
        # A local budget retains its category at an exact tie.
        if self._phase is not None and (
            self.admitted_at is not None or self.deadline is None or self._phase[0] <= self.deadline
        ):
            return self._phase
        if self.admitted_at is None and self.deadline is not None:
            return self.deadline, TransitionAdmissionExpired()
        return None

    def _notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def _arm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        cutoff = self.next_cutoff()
        if not self._stopped and self.expired_error is None and cutoff is not None:
            self._timer = self.loop.call_at(cutoff, self.expire_owner)

    def _select_expiry(self, now: float) -> None:
        selected = self._next()
        if self.expired_error is None and selected is not None and now >= selected[0]:
            self.expired_at, self.expired_error = selected
            self.stop()
            self._notify()

    def expire_owner(self) -> None:
        """Freeze before cancelling, unless explicit cancellation already won."""
        if self._stopped or self._task is None:
            return
        if self._task.cancelling() > self._initial_cancels:
            self.stop()
            return
        self._select_expiry(self.loop.time())
        if self.expired_error is not None:
            self._cancelled_by_deadline = True
            self._task.cancel()
        else:
            self._arm()

    def check(self) -> None:
        self._select_expiry(self.loop.time())
        if self.expired_error is not None:
            raise self.expired_error

    def admit(self) -> None:
        """Confirm the boundary atomically, without observers or suspension."""
        now = self.loop.time()
        self._select_expiry(now)
        if self.expired_error is not None:
            raise self.expired_error
        self.admitted_at = now
        self._arm()
        self._notify()

    def cancellation_cause(self, exc: BaseException) -> BaseException:
        if self._cancelled_by_deadline:
            self._cancelled_by_deadline = False
            if self._task is not None:
                self._task.uncancel()
            # A simultaneously due registry warm-up timer can turn the child's
            # cancellation into ActivationTimeout during settlement. It cannot
            # replace the deadline already selected by this cohort. Ordinary
            # observation errors keep their own classification.
            if isinstance(exc, (asyncio.CancelledError, ActivationTimeout)) and self.expired_error is not None:
                return self.expired_error
        return exc

    async def settle_observation[T](self, operation: Awaitable[T]) -> T:
        """Cancel an observation once, then finish its required cleanup.

        Shielding from the first await prevents repeated caller cancellation from
        abandoning a probe's own awaited finalizer before the registry can settle
        it. A non-cancellation error already unwinding keeps its original cause.
        """
        task = asyncio.ensure_future(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.stop()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await complete_under_cancellation(task)
            raise

    def begin_phase(self, cutoff: float, error: Exception) -> None:
        self._phase = cutoff, error
        self._arm()

    @contextmanager
    def observing(self, cutoff: float | None = None, error: Exception | None = None) -> Generator[None]:
        """Retire a completed local budget; preserve the error starting cleanup."""
        if cutoff is not None and error is not None:
            self.begin_phase(cutoff, error)
        try:
            self.check()
            yield
            self.check()
        except BaseException as exc:
            cause = self.cancellation_cause(exc)
            self.stop()
            if cause is not exc:
                raise cause from exc
            # An ordinary error may have entered registry abandonment before a
            # timer ran. That original error is not converted into expiry.
            if exc is not self.expired_error:
                self.expired_at = None
                self.expired_error = None
            raise
        finally:
            self._phase = None
            self._arm()


@dataclass
class _Cohort:
    future: asyncio.Future[TransitionPlan]
    admission: _TransitionAdmission


class TransitionDeclined(RuntimeError):  # noqa: N818 - brief-mandated name (design §5.2)
    """A computed HARD_SWAP was refused (priority below the hard-swap gate)."""

    def __init__(self, plan: TransitionPlan, priority: float, threshold: int) -> None:
        """Record the refused plan and the priority/threshold that gated it."""
        super().__init__(
            f"Transition declined for '{'/'.join(plan.launch_coven_ids) or 'target'}': "
            f"priority {priority} < hard-swap threshold {threshold}."
        )
        self.plan = plan
        self.priority = priority
        self.threshold = threshold


def _swallow(future: asyncio.Future[TransitionPlan]) -> None:
    """Retrieve a settled future's exception to silence 'never retrieved' warnings."""
    if not future.cancelled():
        future.exception()


class TransitionArbiter:
    """Serialize physical transitions; coalesce same-key contenders onto one plan."""

    def __init__(self) -> None:
        """Initialize an idle arbiter with no waiters and no in-flight transitions."""
        self._busy = False
        self._seq = 0
        self._waiters: list[tuple[float, int, anyio.Event]] = []
        self._inflight: dict[tuple[str, float], _Cohort] = {}

    def follow_inflight(
        self, key: str, priority: float, *, admission_deadline: float | None = None
    ) -> Awaitable[TransitionPlan] | None:
        """Return an isolated wait for an existing same-key, same-priority plan.

        Callers use this before performing their own asynchronous preflight. That
        keeps a follower from failing on a redundant registry probe while its
        cohort owner is already changing the runtime world.
        """
        existing = self._inflight.get((key, priority))
        if existing is None:
            return None
        return self._follow(existing, admission_deadline)

    async def _follow(self, cohort: _Cohort, deadline: float | None) -> TransitionPlan:
        if deadline is None:
            return await asyncio.shield(cohort.future)
        admission = cohort.admission
        while True:
            if admission.admitted_at is not None:
                if admission.admitted_at >= deadline:
                    raise TransitionAdmissionWaitExpired
                return await asyncio.shield(cohort.future)
            cutoff = admission.expired_at
            if cutoff is None:
                cutoff = admission.next_cutoff()
                if cutoff is not None and cutoff <= deadline and admission.loop.time() >= cutoff:
                    admission.expire_owner()
                cutoff = admission.expired_at
            if cutoff is not None and cutoff <= deadline:
                return await asyncio.shield(cohort.future)
            if admission.loop.time() >= deadline:
                raise TransitionAdmissionWaitExpired
            if cohort.future.done():
                return cohort.future.result()
            changed = asyncio.create_task(admission.changed.wait())
            try:
                await asyncio.wait(
                    (cohort.future, changed),
                    timeout=max(0.0, deadline - admission.loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                changed.cancel()
                with suppress(asyncio.CancelledError):
                    await changed

    async def run(
        self,
        key: str,
        priority: float,
        executor: Callable[[], Awaitable[TransitionPlan]],
        *,
        resolve_before_acquire: Callable[[], Awaitable[TransitionPlan | None]] | None = None,
        admission: _TransitionAdmission | None = None,
    ) -> TransitionPlan:
        """Resolve one cohort, entering the physical section only when required.

        A second caller for the same ``key`` awaits the first's result (the executor
        runs once); it does NOT enqueue a waiter. An executor exception releases the
        section and propagates to ALL same-key waiters. When supplied,
        ``resolve_before_acquire`` runs after the cohort future is registered but
        before global arbitration. A non-``None`` result is final, preserving the
        warm no-op fast path without exposing an asynchronous preflight race.
        """
        cohort = (key, priority)
        admission = admission or _TransitionAdmission()
        existing = self.follow_inflight(key, priority, admission_deadline=admission.deadline)
        if existing is not None:
            # A follower owns only its wait, not the shared transition. Without
            # shielding, cancelling one follower cancels the shared Future and
            # poisons the owner plus every other same-key follower.
            return await existing

        future: asyncio.Future[TransitionPlan] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_swallow)
        self._inflight[cohort] = _Cohort(future, admission)
        acquired = False
        admission.start()
        try:
            result = await resolve_before_acquire() if resolve_before_acquire is not None else None
            if result is None:
                await self._acquire(priority)
                acquired = True
                admission.check()
                result = await executor()
        except BaseException as exc:  # relayed to coalesced waiters via the future
            cause = admission.cancellation_cause(exc)
            if not future.done():
                future.set_exception(cause)
            if cause is not exc:
                raise cause from exc
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            admission.stop()
            self._inflight.pop(cohort, None)
            if acquired:
                self._release()

    async def _acquire(self, priority: float) -> None:
        """Enter the critical section, or park in the priority heap until admitted.

        Cancellation-safe: a contender cancelled while parked withdraws its own heap
        entry so a later ``_release`` can't ghost-hand the section to a dead waiter
        (which would wedge ``_busy`` True forever); a contender cancelled AFTER the
        handoff already reached it passes the section straight on (F2).
        """
        if not self._busy:
            self._busy = True
            return
        self._seq += 1
        event = anyio.Event()
        entry = (-priority, self._seq, event)
        heapq.heappush(self._waiters, entry)
        try:
            await event.wait()  # admitted by _release (which keeps _busy True — a handoff)
        except BaseException:
            if event.is_set():
                # The handoff already reached us — we now own the section; pass it on.
                self._release()
            else:
                # Still parked: withdraw so _release won't set a dead waiter's event.
                self._waiters.remove(entry)
                heapq.heapify(self._waiters)
            raise

    def _release(self) -> None:
        """Hand the section to the highest-priority waiter, or go idle."""
        if self._waiters:
            _, _, event = heapq.heappop(self._waiters)
            event.set()
        else:
            self._busy = False
