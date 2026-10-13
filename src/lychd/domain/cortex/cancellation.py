"""Loop-confined coordination for API cancellation and in-process run workers.

Topology A executes the SAQ worker and the web request on one event loop. This
coordinator elects one local cancellation writer; the Run ledger's ``CANCELLING``
state fences the exact delivery generation before broker containment. An interrupted
worker releases its resources once it sees that election. Before election it may
await its durable election instead of racing cancellation with ``FAILED``.
Workers never wait for full cancellation settlement: broker abort waits for them.

The coordinator is process-local and does not replace the durable Run ledger.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

__all__ = ["RunCancellationCoordinator", "RunEffectFence", "RunEffectLifecycle"]


class RunEffectLifecycle(Protocol):
    """Fence host-owned handoffs synchronously, then prove owned work stopped.

    The same process-owned instance serves HTTP, workers and shutdown. Fences
    are monotonic and reasons are content-free labels. Containment must raise
    when sockets or children may remain live; it never reopens a fence.
    Exact-hop refusal may precede an ownership read; Run-wide worker refusal
    and containment require current Run ownership. Cancellation is Run-wide.
    """

    def fence(self, run_id: str, reason: str) -> None: ...

    def fence_claim(self, run_id: str, enqueue_seq: int, reason: str) -> None: ...

    def fence_all(self, reason: str) -> None: ...

    async def contain(self, run_id: str) -> None: ...

    async def contain_all(self) -> None: ...


class RunEffectFence:
    """A monotonic loop-owned handoff fence, without fabricated containment.

    Construct on the owning loop. Consumers inspect ``reason`` for the exact
    retained claim immediately before a non-yielding handoff. Shutdown also fences
    future Run identities.
    This volatile guard is not a durable claim or recovery authorization.
    """

    def __init__(self) -> None:
        """Bind the guard to this process's active control loop."""
        self._loop = asyncio.get_running_loop()
        self._runs: dict[str, str] = {}
        self._claims: dict[tuple[str, int], str] = {}
        self._all: str | None = None

    def _require_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            msg = "Run effect fence belongs to another control loop."
            raise RuntimeError(msg)

    def fence(self, run_id: str, reason: str) -> None:
        """Retain the first fence; later cancellation settlement cannot clear it."""
        self._require_loop()
        self._runs.setdefault(run_id, reason)

    def fence_all(self, reason: str) -> None:
        """Permanently refuse every current and future Run on this guard."""
        self._require_loop()
        if self._all is None:
            self._all = reason

    def fence_claim(self, run_id: str, enqueue_seq: int, reason: str) -> None:
        """Refuse one exact worker hop without fencing an already admitted successor."""
        self._require_loop()
        self._claims.setdefault((run_id, enqueue_seq), reason)

    def reason(self, run_id: str, enqueue_seq: int) -> str | None:
        """Join process, Run and exact-hop refusal on the owning control loop."""
        self._require_loop()
        refusal = self._runs.get(run_id, self._all)
        if refusal is not None:
            return refusal
        return self._claims.get((run_id, enqueue_seq))


class RunCancellationCoordinator:
    """Coordinate cancellation writers on the single Topology-A event loop."""

    __slots__ = ("_elections", "_pending")

    def __init__(self) -> None:
        """Create an empty run-to-cancellation map."""
        self._pending: dict[str, asyncio.Event] = {}
        self._elections: dict[str, asyncio.Event] = {}

    def begin(self, run_id: str) -> bool:
        """Elect one cancellation writer; concurrent callers become waiters."""
        if run_id in self._pending:
            return False
        self._pending[run_id] = asyncio.Event()
        self._elections[run_id] = asyncio.Event()
        return True

    def finish(self, run_id: str) -> None:
        """Release the elected writer and wake its API/worker waiters."""
        pending = self._pending.pop(run_id, None)
        if pending is None:
            return
        pending.set()
        election = self._elections.pop(run_id)
        election.set()

    def election_finished(self, run_id: str) -> None:
        """Wake interrupted workers once the durable election has returned."""
        election = self._elections.get(run_id)
        if election is not None:
            election.set()

    async def wait_election(self, run_id: str) -> None:
        """Wait for durable election or leader failure, then re-read Run truth."""
        election = self._elections.get(run_id)
        if election is not None:
            await election.wait()

    def active(self, run_id: str) -> bool:
        """Return whether an API cancellation is currently settling this run."""
        return run_id in self._pending

    async def wait(self, run_id: str) -> None:
        """Wait for current cancellation writers, if any, to finish settling."""
        pending = self._pending.get(run_id)
        if pending is not None:
            await pending.wait()
