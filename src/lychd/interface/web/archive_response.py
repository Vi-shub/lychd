"""Shared trusted local caller resolution and safe domain refusal projection."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from litestar import Request
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException, PermissionDeniedException

from lychd.domain.archive import ArchiveRefused, ArchiveService, ArchiveSubject
from lychd.domain.codex.sigil import Sigil
from lychd.interface.web.altar_services import AltarServices


def require_archive_access(request: Request[Any, Any, Any], state: State) -> tuple[ArchiveService, ArchiveSubject]:
    services = cast("AltarServices", state.services)
    resolver = services.substrate.archive_subject_resolver
    if services.archive is None or resolver is None:
        raise NotFoundException(detail="The public-note Archive profile is not configured.")
    try:
        return services.archive, resolver(cast("Sigil", request.user))
    except ArchiveRefused as exc:
        raise PermissionDeniedException(detail="Archive caller is outside the configured local binding.") from exc


async def archive_result[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except ArchiveRefused as exc:
        status = 403 if exc.code in {"authority", "namespace"} else 409
        raise HTTPException(status_code=status, detail=str(exc), extra={"code": exc.code}) from exc
