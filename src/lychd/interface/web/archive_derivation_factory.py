"""Compose one configured derivation without registering rows or preparing a runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lychd.domain.archive import ArchiveRefused
from lychd.domain.archive.derivation import GenerationManifest
from lychd.domain.archive.derivation_service import ArchiveDerivationService
from lychd.interface.web.archive_access import LocalArchivePolicy

if TYPE_CHECKING:
    from lychd.config.settings import Settings
    from lychd.domain.animation.services.registry import AnimatorRegistry
    from lychd.domain.codex.sigil import Sigil
    from lychd.domain.cortex.leases import LeaseLedger


def build_archive_derivation(
    settings: Settings, registry: AnimatorRegistry, leases: LeaseLedger, caller: Sigil
) -> ArchiveDerivationService | None:
    if settings.archive is None or settings.archive.derivation is None:
        return None
    from lychd.domain.cortex.dispatcher import Dispatcher

    selection = settings.archive.derivation
    binding = settings.archive_profile
    if binding is None:
        msg = "configuration"
        raise ArchiveRefused(msg)
    spec = registry.require_embedding(selection.binding, "embed_document")
    if spec.embedding_profile is None:
        msg = "embedding_profile"
        raise ArchiveRefused(msg)
    from lychd.db.archive_derivation import DbArchiveDerivationStore
    from lychd.db.engine import get_session_factory

    return ArchiveDerivationService(
        generation_id=selection.generation_id,
        manifest=GenerationManifest(archive=binding, binding=selection.binding, embedding=spec.embedding_profile),
        store=DbArchiveDerivationStore(get_session_factory()),
        authorizer=LocalArchivePolicy(profile=binding, caller=caller, derivations=True),
        dispatcher=Dispatcher(registry=registry, leases=leases, events=None),
    )
