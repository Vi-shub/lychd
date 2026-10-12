"""Independent Archive configuration retains the existing Bridge schema and snapshot law."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lychd.config.settings import SettingsSnapshot
from lychd.domain.archive import ArchiveProfile, ArchiveSubject


def profile() -> dict[str, object]:
    return ArchiveProfile(
        namespace_id=uuid4(), owner_subject=ArchiveSubject(identity="local-owner"), policy_revision="one"
    ).model_dump(mode="json")


@pytest.mark.parametrize("with_bridge", [False, True])
def test_independent_archive_uses_one_effective_binding(*, with_bridge: bool) -> None:
    binding = profile()
    values: dict[str, object] = {"archive": {"profile": binding}}
    if with_bridge:
        values["weaver"] = {"bridge": {"revision": "3", "capability_key": "local:chat:model", "archive": binding}}
    settings = SettingsSnapshot(payload=json.dumps(values)).materialize()
    assert settings.archive_profile == ArchiveProfile.model_validate(binding)
    if not with_bridge:
        assert settings.weaver.bridge is None
    restored = SettingsSnapshot.capture(settings).materialize()
    assert restored.archive_profile == settings.archive_profile


def test_legacy_bridge_only_binding_remains_valid() -> None:
    binding = profile()
    settings = SettingsSnapshot(
        payload=json.dumps(
            {"weaver": {"bridge": {"revision": "3", "capability_key": "local:chat:model", "archive": binding}}}
        )
    ).materialize()
    assert settings.archive is None
    assert settings.archive_profile == ArchiveProfile.model_validate(binding)


def test_conflicting_bindings_and_arbitrary_derivation_manifest_refuse() -> None:
    binding = profile()
    with pytest.raises(ValidationError, match="must match exactly"):
        SettingsSnapshot(
            payload=json.dumps(
                {
                    "archive": {"profile": binding},
                    "weaver": {"bridge": {"revision": "3", "capability_key": "local:chat:model", "archive": profile()}},
                }
            )
        ).materialize()
    with pytest.raises(ValidationError, match="Extra inputs"):
        SettingsSnapshot(
            payload=json.dumps(
                {
                    "archive": {
                        "profile": binding,
                        "derivation": {
                            "generation_id": str(uuid4()),
                            "binding": {"capability_key": "local:embedding:model", "profile_digest": "a" * 64},
                            "manifest": {"endpoint": "https://arbitrary.example"},
                        },
                    }
                }
            )
        ).materialize()
