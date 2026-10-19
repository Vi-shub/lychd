"""Compose one configured derivation without registering rows or preparing a runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lychd.domain.archive.derivation_service import ArchiveDerivationService
from lychd.interface.web.archive_access import LocalArchivePolicy
from lychd.interface.web.archive_generation_factory import configured_archive_generation

if TYPE_CHECKING:
    from lychd.config.settings import Settings
    from lychd.domain.animation.services.registry import AnimatorRegistry
    from lychd.domain.codex.sigil import Sigil
    from lychd.domain.cortex.leases import LeaseLedger


def build_archive_derivation(
    settings: Settings, registry: AnimatorRegistry, leases: LeaseLedger, caller: Sigil
) -> ArchiveDerivationService | None:
    configured = configured_archive_generation(settings, registry, "embed_document")
    if configured is None:
        return None
    generation_id, manifest = configured
    from lychd.db.archive_derivation import DbArchiveDerivationStore
    from lychd.db.engine import get_session_factory
    from lychd.domain.cortex.dispatcher import Dispatcher

    return ArchiveDerivationService(
        generation_id=generation_id,
        manifest=manifest,
        store=DbArchiveDerivationStore(get_session_factory()),
        authorizer=LocalArchivePolicy(profile=manifest.archive, caller=caller, derivations=True),
        dispatcher=Dispatcher(registry=registry, leases=leases, events=None),
    )
