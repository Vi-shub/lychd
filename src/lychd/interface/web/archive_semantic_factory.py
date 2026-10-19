"""Compose only explicitly selected semantic queries, without preparing their runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lychd.domain.archive import ArchiveRefused
from lychd.domain.archive.semantic_service import ArchiveSemanticService
from lychd.interface.web.archive_access import LocalArchivePolicy
from lychd.interface.web.archive_generation_factory import configured_archive_generation

if TYPE_CHECKING:
    from lychd.config.settings import Settings
    from lychd.domain.animation.services.registry import AnimatorRegistry
    from lychd.domain.codex.sigil import Sigil
    from lychd.domain.cortex.leases import LeaseLedger


def build_archive_semantic(
    settings: Settings, registry: AnimatorRegistry, leases: LeaseLedger, caller: Sigil
) -> ArchiveSemanticService | None:
    if settings.archive is None or not settings.archive.semantic:
        return None
    configured = configured_archive_generation(settings, registry, "embed_query")
    if configured is None:
        msg = "configuration"
        raise ArchiveRefused(msg)
    generation_id, manifest = configured
    from lychd.db.archive_semantic import DbArchiveSemanticStore
    from lychd.db.engine import get_session_factory
    from lychd.domain.cortex.dispatcher import Dispatcher

    return ArchiveSemanticService(
        generation_id=generation_id,
        manifest=manifest,
        store=DbArchiveSemanticStore(get_session_factory(), generation_id=generation_id),
        authorizer=LocalArchivePolicy(profile=manifest.archive, caller=caller, semantic=True),
        dispatcher=Dispatcher(registry=registry, leases=leases, events=None),
    )
