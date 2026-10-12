"""Configured-generation discovery authorizes metadata without touching storage or Core."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import Mock
from uuid import uuid4

import pytest

from lychd.domain.animation.embeddings import EmbeddingBinding
from lychd.domain.archive import ArchiveAuthorization, ArchiveProfile, ArchiveRefused, ArchiveSubject
from lychd.domain.archive.contracts import digest
from lychd.domain.archive.derivation import GenerationManifest
from lychd.domain.archive.derivation_ports import DerivationStore, EmbeddingLeaser
from lychd.domain.archive.derivation_service import ArchiveDerivationService
from lychd.domain.codex.sigil import default_local_sigil
from lychd.extensions.builtin.animator.llamacpp.embedding_profile import PROFILE
from lychd.interface.web.archive_access import LocalArchivePolicy


@dataclass
class Authority:
    allowed: bool = True
    observed: list[ArchiveAuthorization] = field(default_factory=list)

    def authorize(self, request: ArchiveAuthorization) -> bool:
        self.observed.append(request)
        return self.allowed


@pytest.mark.asyncio
async def test_generation_discovery_rechecks_policy_without_storage_or_runtime_effects() -> None:
    subject = ArchiveSubject(identity="operator")
    profile = ArchiveProfile(namespace_id=uuid4(), owner_subject=subject, policy_revision="one")
    authority = Authority()
    store = Mock(spec=DerivationStore)
    dispatcher = Mock(spec=EmbeddingLeaser)
    service = ArchiveDerivationService(
        generation_id=uuid4(),
        manifest=GenerationManifest(
            archive=profile,
            binding=EmbeddingBinding(capability_key="embedding:fixture:model", profile_digest=PROFILE.digest),
            embedding=PROFILE,
        ),
        store=store,
        authorizer=authority,
        dispatcher=dispatcher,
    )
    reference = await service.discover(subject, purpose="archive:derive:manage")
    assert reference == service.reference
    request = authority.observed[-1]
    assert request == ArchiveAuthorization(
        subject=subject,
        namespace_id=profile.namespace_id,
        owner_subject=subject,
        policy_revision=profile.policy_revision,
        action="generation_discover",
        purpose="archive:derive:manage",
        object_id=reference.generation_id,
        binding_digest=digest(reference),
    )
    policy = LocalArchivePolicy(profile=profile, caller=default_local_sigil(), derivations=True)
    assert policy.authorize(request)
    assert not policy.authorize(request.model_copy(update={"purpose": "archive:answer"}))
    assert not policy.authorize(request.model_copy(update={"subject": ArchiveSubject(identity="foreign")}))
    assert not LocalArchivePolicy(profile=profile, caller=default_local_sigil()).authorize(request)
    authority.allowed = False
    with pytest.raises(ArchiveRefused, match="authority"):
        await service.discover(subject, purpose="archive:derive:manage")
    assert len(authority.observed) == 2
    assert store.mock_calls == []
    assert dispatcher.mock_calls == []
