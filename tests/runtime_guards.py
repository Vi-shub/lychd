"""Explicit non-durable guard for inert tests with no external runtime effects."""

from lychd.domain.orchestration.containment import SoftActivationRecord


class InertSoftActivationGuard:
    """Keep the write-ahead contract while substituting process-local test storage."""

    def __init__(self) -> None:
        self.pending: SoftActivationRecord | None = None

    def assert_clear(self) -> None:
        if self.pending is not None:
            msg = "Soft activation recovery is required."
            raise RuntimeError(msg)

    def arm(self, record: SoftActivationRecord) -> None:
        self.assert_clear()
        self.pending = record

    def clear(self, record: SoftActivationRecord) -> None:
        if self.pending != record:
            msg = "Soft activation record does not match."
            raise RuntimeError(msg)
        self.pending = None
