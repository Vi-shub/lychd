"""Credential-backed local request identity, separate from a Sigil or permission.

The authentication generation lives for one application startup. Its public
fields support attribution; only the exact in-process marker authenticates a
request. Reconstructing those fields, including from a retained Run, cannot
authenticate another request. This is not remote Ward enrollment or a session.
"""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass
from uuid import UUID, uuid4

__all__ = ["LocalOperatorAccess", "LocalOperatorAuthentication"]


@dataclass(frozen=True, slots=True)
class LocalOperatorAuthentication:
    """Secret-free attribution; field equality does not establish possession."""

    subject: str
    authentication_generation: UUID


class LocalOperatorAccess:
    """Own one startup credential and monotonically revocable request identity.

    Composition shares this instance with middleware and protected local
    consumers. Authentication establishes possession only: each consumer still
    needs its own explicit current policy for the requested object and action.
    Trusted same-process code is outside this containment boundary.
    """

    __slots__ = ("_authentication", "_loop", "_password", "_revoked")

    def __init__(self, *, access_password: str) -> None:
        """Bind an explicitly supplied password without reading ambient state."""
        if not access_password:
            msg = "Required local access password is unavailable."
            raise ValueError(msg)
        self._password = access_password.encode("utf-8")
        self._authentication = LocalOperatorAuthentication(subject="local-operator", authentication_generation=uuid4())
        self._revoked = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_control_loop(self) -> None:
        """Serialize protected consumers and revocation on one active loop."""
        self._require_loop()
        self._loop = asyncio.get_running_loop()

    def _require_loop(self) -> None:
        if self._loop is not None and asyncio.get_running_loop() is not self._loop:
            msg = "Local operator access belongs to another control loop."
            raise RuntimeError(msg)

    def authenticate(self, *, username: bytes, password: bytes) -> LocalOperatorAuthentication | None:
        """Return the actual marker only after current credential possession."""
        self._require_loop()
        matched = hmac.compare_digest(password, self._password)
        if self._revoked or username != b"magus" or not matched:
            return None
        return self._authentication

    def current(self, authentication: object) -> bool:
        """Reject reconstructed, foreign-startup and revoked request markers."""
        self._require_loop()
        return not self._revoked and authentication is self._authentication

    def current_binding(self, subject: str, authentication_generation: UUID) -> bool:
        """Check retained attribution against this startup, without authenticating.

        Only trusted domain code may use this after validating a retained
        declaration and its object/action policy. These fields are not a token.
        """
        self._require_loop()
        return (
            not self._revoked
            and subject == self._authentication.subject
            and authentication_generation == self._authentication.authentication_generation
        )

    def revoke(self) -> None:
        """Invalidate this generation permanently, including later HTTP admission."""
        self._require_loop()
        self._revoked = True
