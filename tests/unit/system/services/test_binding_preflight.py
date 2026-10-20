from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import SecretStr

from lychd.config.components import build_csrf_config, build_saq_config
from lychd.config.settings import DatabaseSettings, ServerSettings, Settings, WebSettings
from lychd.system.binding_sites import AttestedBindingSite
from lychd.system.host_tools import TrustedExecutable
from lychd.system.readiness import (
    HostFoundationInspection,
    HostReadinessItem,
    HostReadinessReport,
    HostReadinessTools,
    ReadinessSection,
    ReadinessState,
)
from lychd.system.services.binding_preflight import (
    BindingPreflightError,
    BindingPreflightService,
)


@dataclass
class _Readiness:
    inspection: HostFoundationInspection

    def inspect(self) -> HostFoundationInspection:
        return self.inspection


def _host_ready(*, root: Path | None = None) -> _Readiness:
    root = root or Path.home()
    quadlet_site = root / ".config" / "containers" / "systemd"
    systemd_site = root / ".config" / "systemd" / "user"
    quadlet_identity = AttestedBindingSite(
        path=quadlet_site,
        device=1,
        inode=4,
    )
    systemd_identity = AttestedBindingSite(
        path=systemd_site,
        device=1,
        inode=5,
    )
    return _Readiness(
        HostFoundationInspection(
            report=HostReadinessReport(
                items=(
                    HostReadinessItem(
                        key="systemd-user",
                        label="systemd user manager",
                        section=ReadinessSection.FOUNDATION,
                        state=ReadinessState.VERIFIED,
                        detail="verified",
                        required_for_bind=True,
                    ),
                    HostReadinessItem(
                        key="podman-quadlet",
                        label="Podman / Quadlet",
                        section=ReadinessSection.FOUNDATION,
                        state=ReadinessState.VERIFIED,
                        detail="verified",
                        required_for_bind=True,
                    ),
                    HostReadinessItem(
                        key="quadlet-sources",
                        label="Quadlet sources",
                        section=ReadinessSection.BINDING_SITES,
                        state=ReadinessState.VERIFIED,
                        detail="prepared",
                        required_for_bind=True,
                        target=quadlet_site,
                        site_identity=quadlet_identity,
                    ),
                    HostReadinessItem(
                        key="systemd-user-units",
                        label="systemd user units",
                        section=ReadinessSection.BINDING_SITES,
                        state=ReadinessState.VERIFIED,
                        detail="prepared",
                        required_for_bind=True,
                        target=systemd_site,
                        site_identity=systemd_identity,
                    ),
                ),
            ),
            tools=HostReadinessTools(
                systemctl=TrustedExecutable(
                    path="/usr/bin/systemctl",
                    device=1,
                    inode=1,
                ),
                podman=TrustedExecutable(
                    path="/usr/bin/podman",
                    device=1,
                    inode=2,
                ),
                quadlet_user_generator=TrustedExecutable(
                    path="/usr/lib/systemd/user-generators/podman-user-generator",
                    device=1,
                    inode=3,
                ),
                findmnt=None,
                btrfs=None,
                chattr=None,
                lsattr=None,
                getenforce=None,
            ),
        )
    )


def _host_blocked() -> _Readiness:
    return _Readiness(
        HostFoundationInspection(
            report=HostReadinessReport(
                items=(
                    HostReadinessItem.failed(
                        key="systemd-user",
                        label="systemd user manager",
                        detail="not reachable",
                        required=True,
                    ),
                ),
            ),
            tools=HostReadinessTools(
                systemctl=None,
                podman=None,
                quadlet_user_generator=None,
                findmnt=None,
                btrfs=None,
                chattr=None,
                lsattr=None,
                getenforce=None,
            ),
        )
    )


def _private_file(path: Path) -> None:
    path.write_text("[server]\n", encoding="utf-8")
    path.chmod(0o600)


def _private_reactor(settings: Settings, tmp_path: Path) -> None:
    inbox = tmp_path / "triggers" / "inbox"
    journal = inbox.parent / "journal"
    inbox.mkdir(parents=True, mode=0o700)
    journal.mkdir(mode=0o700)
    inbox.chmod(0o700)
    journal.chmod(0o700)
    settings.orchestration.switching.host_reactor_dir = inbox


def test_caged_preflight_accepts_private_roots_without_existing_unit_files(
    tmp_path: Path,
) -> None:
    codex = tmp_path / "lychd.toml"
    _private_file(codex)
    settings = Settings.model_construct()
    _private_reactor(settings, tmp_path)

    report = BindingPreflightService(
        codex_path=codex,
        legacy_vessel_unit_path=tmp_path / "systemd" / "lychd-vessel.service",
        host_readiness=_host_ready(root=tmp_path),
    ).inspect(settings)

    assert report.ready is True
    assert report.issues == ()
    assert report.require_ready().systemctl_bin == "/usr/bin/systemctl"
    assert not tuple(tmp_path.rglob("*.service"))
    assert not tuple(tmp_path.rglob("*.path"))


def test_preflight_rejects_legacy_vessel_unit_and_rechecks_it(
    tmp_path: Path,
) -> None:
    """Legacy shadow policy belongs to the repeatable host preflight."""
    codex = tmp_path / "lychd.toml"
    _private_file(codex)
    settings = Settings()
    _private_reactor(settings, tmp_path)
    legacy = tmp_path / "systemd" / "lychd-vessel.service"
    service = BindingPreflightService(
        codex_path=codex,
        legacy_vessel_unit_path=legacy,
        host_readiness=_host_ready(root=tmp_path),
    )

    assert service.inspect(settings).ready is True

    legacy.parent.mkdir()
    legacy.symlink_to(tmp_path / "missing-static-unit")
    report = service.inspect(settings)

    assert [(issue.code, issue.target) for issue in report.issues] == [
        ("legacy-vessel-unit", str(legacy)),
    ]
    with pytest.raises(BindingPreflightError, match="legacy static unit shadows"):
        report.require_ready()


def test_caged_preflight_returns_every_structured_host_issue(tmp_path: Path) -> None:
    codex = tmp_path / "lychd.toml"
    codex.write_text("", encoding="utf-8")
    codex.chmod(0o644)
    settings = Settings()
    settings.orchestration.switching.actuator = "systemd"
    settings.orchestration.switching.host_reactor_dir = tmp_path / "missing" / "inbox"

    report = BindingPreflightService(
        codex_path=codex,
        current_uid=os.getuid() + 1,
        host_readiness=_host_blocked(),
    ).inspect(settings)

    assert {issue.code for issue in report.issues} == {
        "codex-mode",
        "codex-owner",
        "host-foundation",
        "caged-actuator",
        "reactor-shape",
    }
    assert sum(issue.code == "reactor-shape" for issue in report.issues) == 2
    with pytest.raises(BindingPreflightError, match="Binding preflight failed"):
        report.require_ready()


def test_caged_preflight_rejects_symlinked_reactor_boundary(tmp_path: Path) -> None:
    codex = tmp_path / "lychd.toml"
    _private_file(codex)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    inbox = tmp_path / "triggers" / "inbox"
    inbox.parent.mkdir()
    inbox.symlink_to(real, target_is_directory=True)
    journal = inbox.parent / "journal"
    journal.mkdir(mode=0o700)
    settings = Settings()
    settings.orchestration.switching.host_reactor_dir = inbox

    report = BindingPreflightService(
        codex_path=codex,
        host_readiness=_host_ready(root=tmp_path),
    ).inspect(settings)

    assert [(issue.code, issue.target) for issue in report.issues] == [
        ("reactor-shape", str(inbox)),
    ]


def test_preflight_rejects_symlinked_codex_parent(tmp_path: Path) -> None:
    real = tmp_path / "real-codex"
    real.mkdir()
    codex = real / "lychd.toml"
    _private_file(codex)
    alias = tmp_path / "codex-alias"
    alias.symlink_to(real, target_is_directory=True)
    settings = Settings()

    report = BindingPreflightService(
        codex_path=alias / "lychd.toml",
        host_readiness=_host_ready(root=tmp_path),
    ).inspect(settings)

    issue = next(issue for issue in report.issues if issue.code == "codex-shape")
    assert issue.target == str(alias / "lychd.toml")
    assert str(alias) in issue.detail


def test_preflight_rejects_symlinked_reactor_parent(tmp_path: Path) -> None:
    codex = tmp_path / "lychd.toml"
    _private_file(codex)
    real = tmp_path / "real-triggers"
    inbox = real / "inbox"
    journal = real / "journal"
    inbox.mkdir(parents=True, mode=0o700)
    journal.mkdir(mode=0o700)
    alias = tmp_path / "triggers"
    alias.symlink_to(real, target_is_directory=True)
    settings = Settings()
    settings.orchestration.switching.host_reactor_dir = alias / "inbox"

    report = BindingPreflightService(
        codex_path=codex,
        host_readiness=_host_ready(root=tmp_path),
    ).inspect(settings)

    assert [(issue.code, issue.target) for issue in report.issues if issue.code == "reactor-shape"] == [
        ("reactor-shape", str(alias / "inbox")),
        ("reactor-shape", str(alias / "journal")),
    ]


def test_preflight_does_not_require_or_recapture_runtime_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container credentials remain mounted at runtime, outside bind preflight."""
    from unittest.mock import Mock

    codex = tmp_path / "lychd.toml"
    _private_file(codex)
    settings = Settings.model_construct()
    _private_reactor(settings, tmp_path)
    store = Mock(side_effect=AssertionError("preflight attempted a credential read"))
    monkeypatch.setattr("lychd.system.services.secrets.PodmanSecretStore", store)
    service = BindingPreflightService(
        codex_path=codex,
        legacy_vessel_unit_path=tmp_path / "legacy.service",
        host_readiness=_host_ready(root=tmp_path),
    )
    assert service.inspect(settings).ready
    assert service.inspect(settings).ready
    store.assert_not_called()
    assert settings.server.database.runtime_password is None
    assert settings.server.web.secret_key is None
    assert settings.server.web.access_password is None


@pytest.mark.parametrize("source", ["plain", "quadlet", "dangling"])
def test_preflight_refuses_retired_vessel_sources_and_rechecks_without_mutation(
    tmp_path: Path,
    source: str,
) -> None:
    codex = tmp_path / "lychd.toml"
    _private_file(codex)
    settings = Settings.model_construct()
    _private_reactor(settings, tmp_path)
    readiness = _host_ready(root=tmp_path)
    foundation = readiness.inspection.require_ready_for_bind()
    service = BindingPreflightService(
        codex_path=codex,
        legacy_vessel_unit_path=tmp_path / "legacy.service",
        host_readiness=readiness,
    )
    assert service.inspect(settings).ready
    path = (
        foundation.sites.quadlet.path / "lychd-uncaged-vessel.container"
        if source == "quadlet"
        else foundation.sites.systemd_user.path / "lychd-uncaged-vessel.service"
    )
    path.parent.mkdir(parents=True, mode=0o700)
    if source == "dangling":
        path.symlink_to(tmp_path / "absent-retired-source")
    else:
        path.write_text("retained-native-source", encoding="utf-8")
    before = set(tmp_path.rglob("*"))
    report = service.inspect(settings)
    assert set(tmp_path.rglob("*")) == before
    assert [issue.code for issue in report.issues] == ["retired-vessel-binding"]
    with pytest.raises(BindingPreflightError, match="no conversion, removal, or service control"):
        report.require_ready()
    if source == "dangling":
        assert path.is_symlink()
    else:
        assert path.read_text(encoding="utf-8") == "retained-native-source"


@pytest.mark.parametrize(
    "units",
    [
        ("lychd-uncaged-vessel.service",),
        ("lychd-vessel.service", "lychd-uncaged-vessel.service"),
    ],
)
def test_preflight_refuses_receipt_owned_retired_or_mixed_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    units: tuple[str, ...],
) -> None:
    from unittest.mock import Mock, call

    from lychd.system.services.scribe import OwnedBindings

    codex = tmp_path / "lychd.toml"
    _private_file(codex)
    settings = Settings.model_construct()
    _private_reactor(settings, tmp_path)
    scribe = Mock()
    scribe.inspect_owned_bindings.return_value = OwnedBindings(receipt_present=True, runtime_units=units)
    monkeypatch.setattr("lychd.system.services.scribe.ScribeService", Mock(return_value=scribe))
    report = BindingPreflightService(
        codex_path=codex,
        legacy_vessel_unit_path=tmp_path / "legacy.service",
        host_readiness=_host_ready(root=tmp_path),
    ).inspect(settings)
    assert [issue.code for issue in report.issues] == ["retired-vessel-binding"]
    assert scribe.method_calls == [call.inspect_owned_bindings()]


def test_runtime_components_require_and_use_loaded_credentials() -> None:
    """Credential consumers use their admitted Settings values without source I/O."""
    settings = Settings.model_construct(
        server=ServerSettings(
            database=DatabaseSettings(runtime_password=SecretStr("synthetic-runtime-credential")),
            web=WebSettings(secret_key=SecretStr("synthetic-signing-credential")),
        )
    )
    assert all(
        queue.dsn and "synthetic-runtime-credential" in queue.dsn for queue in build_saq_config(settings).queue_configs
    )
    assert build_csrf_config(settings).secret == "synthetic-signing-credential"  # noqa: S105
    settings.server.database = settings.server.database.model_copy(update={"runtime_password": None})
    with pytest.raises(ValueError, match="Required runtime database password"):
        build_saq_config(settings)


def test_locked_apply_rechecks_retired_binding_before_any_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext
    from unittest.mock import Mock

    from lychd.config.settings import SettingsSnapshot
    from lychd.system.services.bind import BindRequest
    from lychd.system.services.bind_assembly import assemble_bind_use_case
    from lychd.system.services.scribe import BindingReconcilePlan, OwnedBindings

    codex = tmp_path / "lychd.toml"
    _private_file(codex)
    settings = Settings.model_construct()
    _private_reactor(settings, tmp_path)
    readiness = _host_ready(root=tmp_path)
    foundation = readiness.inspection.require_ready_for_bind()
    scribe = Mock()
    scribe.inspect_owned_bindings.return_value = OwnedBindings(receipt_present=False)
    scribe.plan_reconcile_all.return_value = BindingReconcilePlan(
        changes=(),
        observed_generation="before",
        desired_generation="desired",
    )
    monkeypatch.setattr("lychd.system.services.scribe.ScribeService", Mock(return_value=scribe))
    monkeypatch.setattr("lychd.system.services.scribe.facade.ScribeService", Mock(return_value=scribe))
    secrets = Mock()
    secrets.exists.return_value = False
    monkeypatch.setattr("lychd.system.services.secrets.PodmanSecretStore", Mock(return_value=secrets))
    systemd = Mock()
    monkeypatch.setattr("lychd.system.services.systemd.SystemdUserManager", systemd)
    monkeypatch.setattr("lychd.system.services.lifecycle.lock.LifecycleLock", nullcontext)
    service = BindingPreflightService(
        codex_path=codex,
        legacy_vessel_unit_path=tmp_path / "legacy.service",
        host_readiness=readiness,
    )
    assert service.inspect(settings).ready
    use_case = assemble_bind_use_case(
        foundation=foundation,
        preflight_service=service,
        settings_snapshot=SettingsSnapshot.capture(settings),
    )
    request = BindRequest.compile(
        manifests=(),
        plain_units={},
        core_secret_factories={"planned-core": lambda: "unused"},
        required_secret_names=(),
    )
    approved = use_case.plan(request)
    retired = foundation.sites.systemd_user.path / "lychd-uncaged-vessel.service"
    retired.parent.mkdir(parents=True, mode=0o700)
    retired.write_text("retained-native-source", encoding="utf-8")

    with pytest.raises(BindingPreflightError, match="no conversion, removal, or service control"):
        use_case.apply(request, approved)

    secrets.ensure_present.assert_not_called()
    scribe.reconcile_all.assert_not_called()
    systemd.assert_not_called()
    assert retired.read_text(encoding="utf-8") == "retained-native-source"
