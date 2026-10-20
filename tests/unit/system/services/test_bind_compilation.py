"""Single-snapshot contracts for pure bind request compilation."""

import os
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from lychd.config import QuadletConfig
from lychd.config.runes.registry import RuneRegistry
from lychd.config.settings.root import Settings, get_settings
from lychd.config.settings.server import ServerSettings
from lychd.domain.animation.conflicts import ConflictTopologyError
from lychd.domain.animation.schemas import GenericSoulstoneConfig, OpenAIPortalConfig, SoulstoneConfig
from lychd.domain.animation.transmute import QuadletContribution, TransmutationContext
from lychd.extensions.context import ExtensionContext
from lychd.extensions.host import AssembledExtensions, assemble_extensions
from lychd.system.binding_sites import AttestedBindingSite, AttestedBindingSites
from lychd.system.constants import CONTAINER_LYCHD_PORT
from lychd.system.host_tools import TrustedExecutable
from lychd.system.readiness import BindingFoundation
from lychd.system.schemas import QuadletContainer, QuadletPod
from lychd.system.services.bind import (
    BindApplyError,
    BindingCommitState,
    BindingPlanDriftError,
    BindingRequirementError,
    BindRequest,
    BindUseCase,
)
from lychd.system.services.bind_compilation import compile_bind_request
from lychd.system.services.binding_preflight import BindingPreflightReport
from lychd.system.services.scribe import BindingReconcilePlan


def test_compile_uses_only_its_injected_settings_snapshot() -> None:
    """Quadlets, units, and core secrets derive from the same explicit Settings."""
    global_port = get_settings().server.port
    injected_port = global_port + 1000
    settings = Settings(server=ServerSettings(port=injected_port))

    request = compile_bind_request(
        settings=settings,
        extensions=assemble_extensions(settings),
        runes=RuneRegistry(()),
        soulstones=(),
        portals=(),
        uncaged=False,
    )

    pod = next(manifest for manifest in request.manifests if isinstance(manifest, QuadletPod))
    assert f"127.0.0.1:{injected_port}:{CONTAINER_LYCHD_PORT}" in pod.publish_ports
    assert f"127.0.0.1:{global_port}:{CONTAINER_LYCHD_PORT}" not in pod.publish_ports
    assert tuple(secret.name for secret in request.core_secrets) == tuple(
        sorted(settings.server.privileged_secret_names)
    )


def test_compile_emits_uncaged_vessel_unit_from_injected_settings() -> None:
    settings = Settings(server=ServerSettings(port=9999))

    request = compile_bind_request(
        settings=settings,
        extensions=assemble_extensions(settings),
        runes=RuneRegistry(()),
        soulstones=(),
        portals=(),
        uncaged=True,
    )

    rendered = request.plain_unit_mapping()["lychd-uncaged-vessel.service"]
    executable = Path(sys.prefix) / "bin" / "lychd"
    assert f'ExecStart="{executable}" serve --host 127.0.0.1 --port 9999' in rendered
    assert 'Environment="LYCHD_MODE=uncaged"' in rendered


def test_bind_rejects_a_soulstone_without_an_advertised_capability() -> None:
    """An unadvertised local service cannot enter capability-derived runtime truth."""
    settings = get_settings()
    stone = GenericSoulstoneConfig(
        name="crawler-sidecar",
        quadlet=QuadletConfig(image="example/crawler:latest"),
        runtime="crawler",
    )

    with pytest.raises(
        ConflictTopologyError,
        match=r"at least one capability.*crawler-sidecar",
    ):
        compile_bind_request(
            settings=settings,
            extensions=assemble_extensions(settings),
            runes=RuneRegistry(()),
            soulstones=(stone,),
            portals=(),
            uncaged=False,
        )


@dataclass(frozen=True)
class _SecretContributor:
    containers: tuple[QuadletContainer, ...]

    def contribute(self, ctx: TransmutationContext) -> QuadletContribution:
        del ctx
        return QuadletContribution(containers=self.containers)


def _contributor_request(
    *container_secrets: tuple[str, ...],
    settings: Settings | None = None,
    portals: tuple[OpenAIPortalConfig, ...] = (),
) -> BindRequest:
    context = ExtensionContext()
    context.registration_view("crypt:secret-test").transmutation.add_contributor(
        _SecretContributor(
            containers=tuple(
                QuadletContainer(
                    image="example/extension:latest",
                    description="Extension secret regression",
                    container_name=f"extension-{index}",
                    secrets=list(specs),
                )
                for index, specs in enumerate(container_secrets)
            ),
        )
    )
    return compile_bind_request(
        settings=settings or Settings(),
        extensions=AssembledExtensions(context=context),
        runes=RuneRegistry(()),
        soulstones=(),
        portals=portals,
        uncaged=False,
    )


def _bind_ports(present: set[str]) -> tuple[BindUseCase, MagicMock, MagicMock, MagicMock]:
    """Run the real transaction with inert host ports and explicit secret state."""
    foundation = BindingFoundation(
        systemctl=TrustedExecutable(path="/usr/bin/systemctl", device=1, inode=1),
        podman=TrustedExecutable(path="/usr/bin/podman", device=1, inode=2),
        quadlet_user_generator=TrustedExecutable(path="/usr/libexec/podman/quadlet", device=1, inode=3),
        sites=AttestedBindingSites(
            quadlet=AttestedBindingSite(path=Path("/bindings/quadlet"), device=1, inode=4),
            systemd_user=AttestedBindingSite(path=Path("/bindings/systemd"), device=1, inode=5),
        ),
    )
    scribe = MagicMock()
    scribe.plan_reconcile_all.return_value = BindingReconcilePlan(
        changes=(),
        observed_generation="before",
        desired_generation="desired",
    )
    scribe.reconcile_all.return_value = "committed"
    secrets = MagicMock()
    secrets.exists.side_effect = present.__contains__

    def ensure(name: str, value: str) -> bool:
        assert value
        created = name not in present
        present.add(name)
        return created

    secrets.ensure_present.side_effect = ensure
    systemd = MagicMock()
    use_case = BindUseCase(
        scribe=scribe,
        secrets=secrets,
        systemd_factory=lambda: systemd,
        foundation=foundation,
        foundation_probe=lambda: foundation,
        lock_factory=nullcontext,
    )
    return use_case, secrets, scribe, systemd


@pytest.mark.parametrize(
    "spec",
    [
        "extension-token",
        "extension-token,type=mount,target=/run/secrets/renamed,mode=0400",
        "extension-token,type=env,target=EXTENSION_TOKEN",
    ],
)
def test_contributor_secret_absence_refuses_before_effects(spec: str) -> None:
    request = _contributor_request((spec,))
    use_case, secrets, scribe, systemd = _bind_ports(set())
    approved = use_case.plan(request)

    with pytest.raises(BindingRequirementError, match="extension-token"):
        use_case.apply(request, approved)

    assert request.required_secret_names == ("extension-token",)
    assert approved.missing_required_secrets == ("extension-token",)
    secrets.ensure_present.assert_not_called()
    scribe.reconcile_all.assert_not_called()
    systemd.daemon_reload.assert_not_called()


@pytest.mark.parametrize(
    "spec",
    [
        "extension-token",
        "extension-token,type=mount,target=/run/secrets/renamed,mode=0400",
        "extension-token,type=env,target=EXTENSION_TOKEN",
    ],
)
def test_present_contributor_secret_allows_core_generation_and_commit(spec: str) -> None:
    request = _contributor_request((spec,))
    present = {"extension-token"}
    use_case, secrets, scribe, systemd = _bind_ports(present)
    approved = use_case.plan(request)

    result = use_case.apply(request, approved)

    assert request.required_secret_names == ("extension-token",)
    assert approved.missing_required_secrets == ()
    assert approved.missing_core_secrets == tuple(secret.name for secret in request.core_secrets)
    assert result.created_secrets == approved.missing_core_secrets
    assert {call.args[0] for call in secrets.ensure_present.call_args_list} == set(result.created_secrets)
    assert result.binding_generation == "committed"
    scribe.reconcile_all.assert_called_once_with(
        request.manifests,
        plain_units=request.plain_unit_mapping(),
        expected_generation="before",
        expected_desired_generation="desired",
    )
    systemd.daemon_reload.assert_called_once_with()
    container = next(
        manifest
        for manifest in request.manifests
        if isinstance(manifest, QuadletContainer) and manifest.container_name == "extension-0"
    )
    assert container.secrets == [spec]


def test_contributor_secret_disappearing_after_plan_refuses_before_effects() -> None:
    request = _contributor_request(("extension-token",))
    present = {"extension-token"}
    use_case, secrets, scribe, systemd = _bind_ports(present)
    approved = use_case.plan(request)
    present.remove("extension-token")

    with pytest.raises(BindingPlanDriftError, match="secret state changed after planning"):
        use_case.apply(request, approved)

    secrets.ensure_present.assert_not_called()
    scribe.reconcile_all.assert_not_called()
    systemd.daemon_reload.assert_not_called()


def test_contributor_secret_disappearing_before_commit_preserves_core_progress() -> None:
    request = _contributor_request(("extension-token",))
    present = {"extension-token"}
    use_case, secrets, scribe, systemd = _bind_ports(present)
    approved = use_case.plan(request)

    def ensure(name: str, value: str) -> bool:
        assert value
        present.add(name)
        present.discard("extension-token")
        return True

    secrets.ensure_present.side_effect = ensure
    with pytest.raises(BindApplyError, match="disappeared before binding commit: extension-token") as failure:
        use_case.apply(request, approved)

    assert failure.value.progress.created_secrets == approved.missing_core_secrets
    assert failure.value.progress.binding_commit_state is BindingCommitState.NOT_ATTEMPTED
    scribe.reconcile_all.assert_not_called()
    systemd.daemon_reload.assert_not_called()


def test_contributor_requirements_deduplicate_and_keep_exact_configured_core_names() -> None:
    settings = Settings.model_validate(
        {
            "server": {
                "web": {"secret_key_secret": "custom-signing", "access_password_secret": "custom-access"},
                "database": {
                    "password_secret": "custom-db",
                    "runtime_password_secret": "custom-runtime",
                    "phoenix_password_secret": "custom-phoenix",
                },
            }
        }
    )
    portal = OpenAIPortalConfig(
        name="remote",
        api_key_secret_name="portal-token",  # noqa: S106 - reference name, not a credential
    )
    request = _contributor_request(
        ("z-token", "lychd_db_password", "a-token,type=env,target=TOKEN", "custom-phoenix"),
        ("a-token", "z-token,target=/run/secrets/z", "portal-token"),
        settings=settings,
        portals=(portal,),
    )

    assert request.required_secret_names == ("a-token", "lychd_db_password", "portal-token", "z-token")
    use_case, _, _, _ = _bind_ports(set(request.required_secret_names))
    approved = use_case.plan(request)
    assert approved.missing_core_secrets == tuple(sorted(settings.server.privileged_secret_names))
    assert use_case.apply(request, approved).created_secrets == approved.missing_core_secrets


def test_compilation_keeps_portal_core_alias_refusal() -> None:
    settings = Settings()
    portal = OpenAIPortalConfig(name="remote", api_key_secret_name=settings.server.web.secret_key_secret)
    with pytest.raises(ValueError, match="cannot alias core"):
        _contributor_request(("extension-token",), settings=settings, portals=(portal,))


@pytest.mark.parametrize("alias_core", [False, True])
def test_compilation_keeps_soulstone_requirements_and_core_alias_refusal(*, alias_core: bool) -> None:
    from lychd.extensions.builtin.animator.soulstones.vllm import VllmSoulstoneConfig

    settings = Settings.model_validate({"extensions": {"builtins": ("animator/vllm",), "crypt": ()}})
    secret_name = settings.server.database.password_secret if alias_core else "runtime-token"
    stone = VllmSoulstoneConfig(
        name="model",
        model_path="/models/test",
        port=20000,
        secret_env_files={"TOKEN_FILE": secret_name},
    )
    if alias_core:
        with pytest.raises(ValueError, match="must be distinct from core"):
            compile_bind_request(
                settings=settings,
                extensions=assemble_extensions(settings),
                runes=RuneRegistry(()),
                soulstones=(stone,),
                portals=(),
                uncaged=False,
            )
    else:
        request = compile_bind_request(
            settings=settings,
            extensions=assemble_extensions(settings),
            runes=RuneRegistry(()),
            soulstones=(stone,),
            portals=(),
            uncaged=False,
        )
        assert request.required_secret_names == ("runtime-token",)


@dataclass(frozen=True)
class _RawPodmanContributor:
    arguments: tuple[str, ...]
    mutate_before_return: bool
    pod_ports: tuple[str, ...] = ()

    def contribute(self, ctx: TransmutationContext) -> QuadletContribution:
        del ctx
        container = QuadletContainer(
            description="Extension raw argument policy",
            image="example/extension:latest",
            container_name="extension-raw-args",
            podman_args=[] if self.mutate_before_return else list(self.arguments),
        )
        if self.mutate_before_return:
            container.podman_args.extend(self.arguments)
        return QuadletContribution(containers=(container,), pod_ports=self.pod_ports)


@pytest.mark.parametrize(
    "arguments",
    [("--secret=extension-token",), ("--secret", "extension-token"), ('"--secret=extension-token"',)],
)
@pytest.mark.parametrize("mutate_before_return", [False, True])
def test_contributor_raw_secret_refuses_before_binding_effects(
    arguments: tuple[str, ...],
    *,
    mutate_before_return: bool,
) -> None:
    context = ExtensionContext()
    context.registration_view("crypt:raw-secret-test").transmutation.add_contributor(
        _RawPodmanContributor(arguments=arguments, mutate_before_return=mutate_before_return),
    )
    use_case, secrets, scribe, systemd = _bind_ports(set())

    def bind_contribution() -> None:
        request = compile_bind_request(
            settings=Settings(),
            extensions=AssembledExtensions(context=context),
            runes=RuneRegistry(()),
            soulstones=(),
            portals=(),
            uncaged=False,
        )
        use_case.apply(request, use_case.plan(request))

    with pytest.raises(ValueError, match="Raw Podman arguments"):
        bind_contribution()

    secrets.exists.assert_not_called()
    secrets.ensure_present.assert_not_called()
    scribe.plan_reconcile_all.assert_not_called()
    scribe.reconcile_all.assert_not_called()
    systemd.daemon_reload.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ("--publish=0.0.0.0:19999:9999/udp",),
        ("--publish", "0.0.0.0:19999:9999/udp"),
        ('"--publish=0.0.0.0:19999:9999/udp"',),
        ("-p", "0.0.0.0:19999:9999/udp"),
        ("-p0.0.0.0:19999:9999/udp",),
        ("-dp0.0.0.0:19999:9999/udp",),
        ("--publish-all",),
        ("-P",),
        ("-dP",),
        ("--network=host",),
        ("--network", "host"),
        ("--net=host",),
        ("--volume=/etc:/etc:ro",),
        ("--tmpfs=/:rw",),
        ("--user=0",),
        ("--future-option=enabled",),
        ("--replace=true",),
        ("--replace --ipc=host",),
    ],
)
@pytest.mark.parametrize("mutate_before_return", [False, True])
def test_contributor_unsupported_raw_arguments_refuse_before_binding_effects(
    arguments: tuple[str, ...],
    *,
    mutate_before_return: bool,
) -> None:
    context = ExtensionContext()
    context.registration_view("crypt:raw-policy-test").transmutation.add_contributor(
        _RawPodmanContributor(arguments=arguments, mutate_before_return=mutate_before_return),
    )
    use_case, secrets, scribe, systemd = _bind_ports(set())

    def bind_contribution() -> None:
        request = compile_bind_request(
            settings=Settings(),
            extensions=AssembledExtensions(context=context),
            runes=RuneRegistry(()),
            soulstones=(),
            portals=(),
            uncaged=False,
        )
        use_case.apply(request, use_case.plan(request))

    with pytest.raises(ValueError, match="Raw Podman arguments"):
        bind_contribution()

    secrets.exists.assert_not_called()
    secrets.ensure_present.assert_not_called()
    scribe.plan_reconcile_all.assert_not_called()
    scribe.reconcile_all.assert_not_called()
    systemd.daemon_reload.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("--replace",),
        (
            "--ulimit=memlock=-1:-1",
            "--replace",
            "--tmpfs=/app/logs:rw,nosuid,nodev,noexec,mode=1777",
            "--ipc=host",
            "--replace",
        ),
    ],
)
@pytest.mark.parametrize("mutate_before_return", [False, True])
def test_supported_raw_contribution_keeps_typed_loopback_ports_through_binding(
    arguments: tuple[str, ...],
    *,
    mutate_before_return: bool,
) -> None:
    context = ExtensionContext()
    context.registration_view("crypt:raw-policy-test").transmutation.add_contributor(
        _RawPodmanContributor(
            arguments=arguments,
            mutate_before_return=mutate_before_return,
            pod_ports=("19999:9999",),
        ),
    )
    request = compile_bind_request(
        settings=Settings(),
        extensions=AssembledExtensions(context=context),
        runes=RuneRegistry(()),
        soulstones=(),
        portals=(),
        uncaged=False,
    )
    use_case, _, scribe, systemd = _bind_ports(set())

    result = use_case.apply(request, use_case.plan(request))

    container = next(
        manifest
        for manifest in request.manifests
        if isinstance(manifest, QuadletContainer) and manifest.container_name == "extension-raw-args"
    )
    pod = next(manifest for manifest in request.manifests if isinstance(manifest, QuadletPod))
    assert container.podman_args == list(arguments)
    assert pod.publish_ports[-1] == "127.0.0.1:19999:9999"
    assert result.binding_generation == "committed"
    scribe.reconcile_all.assert_called_once_with(
        request.manifests,
        plain_units=request.plain_unit_mapping(),
        expected_generation="before",
        expected_desired_generation="desired",
    )
    systemd.daemon_reload.assert_called_once_with()


def _native_profile_request(
    settings: Settings,
    *,
    native_only: bool = False,
    soulstones: Sequence[SoulstoneConfig] = (),
) -> BindRequest:
    return compile_bind_request(
        settings=settings,
        extensions=assemble_extensions(settings),
        runes=RuneRegistry(()),
        soulstones=soulstones,
        portals=(),
        uncaged=False,
        native_only=native_only,
    )


@pytest.mark.parametrize("actuator", ["host-reactor", "systemd"])
def test_native_only_fileset_preserves_storage_and_orders_native_start(actuator: str) -> None:
    """Only the native listener changes; every image-owned bootstrap contract survives."""
    from lychd.system import constants

    settings = Settings()
    settings.orchestration.switching = settings.orchestration.switching.model_validate({"actuator": actuator})
    settings.server.port = 9999
    settings.server.database.port = 15432
    baseline = _native_profile_request(settings)
    request = _native_profile_request(settings, native_only=True)
    containers = {m.container_name: m for m in request.manifests if isinstance(m, QuadletContainer)}
    baseline_containers = {m.container_name: m for m in baseline.manifests if isinstance(m, QuadletContainer)}
    assert set(containers) == {"lychd-phylactery", "lychd-migrate"}
    for name, container in containers.items():
        assert container == baseline_containers[name]
    pod = next(m for m in request.manifests if isinstance(m, QuadletPod))
    assert pod.publish_ports == ["127.0.0.1:15432:5432"]
    expected = {"lychd-uncaged-vessel.service"}
    if actuator == "host-reactor":
        expected |= {"lychd-reactor.path", "lychd-reactor.service"}
    assert set(request.plain_unit_mapping()) == expected
    native = request.plain_unit_mapping()["lychd-uncaged-vessel.service"]
    assert f'ExecStart="{Path(sys.prefix) / "bin" / "lychd"}" serve --host 127.0.0.1 --port 9999' in native
    dependencies = "lychd-migrate.service" + (" lychd-reactor.path" if actuator == "host-reactor" else "")
    assert f"Requires={dependencies}\nAfter={dependencies}\n" in native
    assignments = [line for line in native.splitlines() if line.startswith("Environment=")]
    assert assignments == sorted(
        [
            f'Environment="HOME={Path.home()}"',
            'Environment="LYCHD_MODE=uncaged"',
            'Environment="LYCHD_RUNTIME_SECRETS=podman-core@1"',
            'Environment="SERVER__DATABASE__HOST=127.0.0.1"',
            'Environment="SERVER__DATABASE__PORT=15432"',
            f'Environment="XDG_CACHE_HOME={constants.PATH_XDG_CACHE_HOME}"',
            f'Environment="XDG_CONFIG_HOME={constants.PATH_XDG_CONFIG_HOME}"',
            f'Environment="XDG_DATA_HOME={constants.PATH_XDG_DATA_HOME}"',
        ]
    )
    runtime_names = {
        settings.server.database.runtime_password_secret,
        settings.server.web.secret_key_secret,
        settings.server.web.access_password_secret,
    }
    assert set(request.required_secret_names) == {*baseline.required_secret_names, *runtime_names}
    assert {secret.name for secret in request.core_secrets} == {
        settings.server.database.password_secret,
        settings.server.database.phoenix_password_secret,
    }
    assert request.secret_names == baseline.secret_names


def test_native_only_refuses_dual_selection_before_compilation() -> None:
    settings = Settings()
    with pytest.raises(ValueError, match="mutually exclusive"):
        compile_bind_request(
            settings=settings,
            extensions=assemble_extensions(settings),
            runes=RuneRegistry(()),
            soulstones=(),
            portals=(),
            uncaged=True,
            native_only=True,
        )


def test_native_only_preserves_model_declarations_and_contributor_ports() -> None:
    from lychd.extensions.builtin.animator.soulstones.vllm import VllmSoulstoneConfig

    settings = Settings.model_validate({"extensions": {"builtins": ("animator/vllm",), "crypt": ()}})
    stone = VllmSoulstoneConfig(name="model", model_path="/models/test", port=20000)
    baseline = _native_profile_request(settings, soulstones=(stone,))
    native = _native_profile_request(settings, soulstones=(stone,), native_only=True)
    assert [m for m in native.manifests if not isinstance(m, QuadletPod)] == [
        m
        for m in baseline.manifests
        if not isinstance(m, QuadletPod)
        and not (isinstance(m, QuadletContainer) and m.container_name == "lychd-vessel")
    ]
    assert set(native.required_secret_names) == {
        *baseline.required_secret_names,
        settings.server.database.runtime_password_secret,
        settings.server.web.secret_key_secret,
        settings.server.web.access_password_secret,
    }


def test_native_only_cannot_reintroduce_application_port_through_contribution() -> None:
    settings = Settings()
    context = ExtensionContext()
    context.registration_view("crypt:port-test").transmutation.add_contributor(
        _RawPodmanContributor(arguments=(), mutate_before_return=False, pod_ports=(f"{settings.server.port}:9999",)),
    )
    with pytest.raises(ValueError, match="native Vessel listener port"):
        compile_bind_request(
            settings=settings,
            extensions=AssembledExtensions(context=context),
            runes=RuneRegistry(()),
            soulstones=(),
            portals=(),
            uncaged=False,
            native_only=True,
        )


def test_native_fileset_cannot_adopt_a_foreign_native_unit(tmp_path: Path) -> None:
    from lychd.system.constants import PATH_RUNE_TEMPLATES_DIR
    from lychd.system.services.scribe import ScribeConflictError, ScribeService

    quadlet, units = tmp_path / "quadlet", tmp_path / "units"
    quadlet.mkdir(mode=0o700)
    units.mkdir(mode=0o700)
    occupied = units / "lychd-uncaged-vessel.service"
    occupied.write_text("foreign-native-unit", encoding="utf-8")
    request = _native_profile_request(Settings(), native_only=True)
    scribe = ScribeService(templates_dir=PATH_RUNE_TEMPLATES_DIR, output_dir=quadlet, systemd_dir=units)
    with pytest.raises(ScribeConflictError, match="unowned"):
        scribe.plan_reconcile_all(request.manifests, plain_units=request.plain_unit_mapping())
    assert occupied.read_text(encoding="utf-8") == "foreign-native-unit"
    assert not scribe.ownership_path.exists()
    assert tuple(quadlet.iterdir()) == ()


def test_native_runtime_secret_disappearing_after_plan_refuses_before_effects() -> None:
    settings = Settings()
    request = _native_profile_request(settings, native_only=True)
    present = set(request.secret_names)
    use_case, secrets, scribe, systemd = _bind_ports(present)
    approved = use_case.plan(request)
    present.remove(settings.server.database.runtime_password_secret)
    with pytest.raises(BindingPlanDriftError, match="secret state changed after planning"):
        use_case.apply(request, approved)
    secrets.ensure_present.assert_not_called()
    scribe.reconcile_all.assert_not_called()
    systemd.daemon_reload.assert_not_called()


@pytest.mark.parametrize("missing_index", [0, 1, 2])
@pytest.mark.parametrize("dry_run", [False, True])
def test_native_runtime_secret_absent_before_initial_plan_refuses_cli_and_apply(
    missing_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dry_run: bool,
) -> None:
    from lychd.cli.binding import PreparedBinding
    from lychd.cli.commands import bind_quadlets

    names = ("native-runtime", "native-signing", "native-access")
    present: set[str] = set(names)
    use_case, secrets, scribe, systemd = _bind_ports(present)
    secrets.read.return_value = SecretStr("synthetic-native-capture-credential-1234567890")
    monkeypatch.setattr(
        os,
        "environ",
        {
            "LYCHD_RUNTIME_SECRETS": "podman-core@1",
            "SERVER__DATABASE__RUNTIME_PASSWORD_SECRET": names[0],
            "SERVER__WEB__SECRET_KEY_SECRET": names[1],
            "SERVER__WEB__ACCESS_PASSWORD_SECRET": names[2],
        },
    )
    monkeypatch.setattr("lychd.config.settings.root.PATH_LYCHD_TOML", tmp_path / "absent.toml")
    monkeypatch.setattr("lychd.system.host_tools.trusted_host_tool", MagicMock(return_value="/usr/bin/podman"))
    monkeypatch.setattr("lychd.system.services.secrets.PodmanSecretStore", MagicMock(return_value=secrets))
    settings = Settings()
    assert settings.runtime_secret_capture is not None
    assert settings.runtime_secret_capture.names == names
    assert [call.args[0] for call in secrets.read.call_args_list] == list(names)
    request = _native_profile_request(settings, native_only=True)
    missing_name = names[missing_index]
    present.remove(missing_name)
    approved = use_case.plan(request)
    assert approved.missing_required_secrets == (missing_name,)
    assert set(approved.missing_core_secrets) == {
        settings.server.database.password_secret,
        settings.server.database.phoenix_password_secret,
    }
    session = MagicMock()
    session.preflight = BindingPreflightReport(issues=(), foundation=approved.foundation)
    session.prepare.return_value = PreparedBinding(request=request, plan=approved, use_case=use_case)
    monkeypatch.setattr("lychd.cli.binding.BindingCommandSession.inspect", MagicMock(return_value=session))
    result = CliRunner().invoke(bind_quadlets, ["--native-only", *(["--dry-run"] if dry_run else [])])
    assert result.exit_code == 1, result.output
    assert missing_name in result.output
    assert "Binding plan is coherent" not in result.output
    with pytest.raises(BindingRequirementError, match=missing_name):
        use_case.apply(request, approved)
    assert secrets.read.call_count == 3
    secrets.ensure_present.assert_not_called()
    scribe.reconcile_all.assert_not_called()
    systemd.daemon_reload.assert_not_called()


@pytest.mark.parametrize("stem", ["lychd-vessel", "lychd-uncaged-vessel"])
@pytest.mark.parametrize("suffix", ["", ".container", ".service"])
def test_native_contribution_cannot_emit_a_reserved_vessel_source(stem: str, suffix: str) -> None:
    _assert_native_contribution_refuses(
        QuadletContainer(
            description="Reserved source",
            image="fixture/image",
            container_name=f"{stem}{suffix}",
        )
    )


@pytest.mark.parametrize("stem", ["lychd-vessel", "lychd-uncaged-vessel"])
@pytest.mark.parametrize(
    "field", ["pod", "pod_service", "wants", "requires", "after", "binds_to", "conflicts", "wanted_by"]
)
def test_native_contribution_cannot_reintroduce_a_vessel_through_unit_directives(stem: str, field: str) -> None:
    value = f"{stem}.container" if field == "pod" else f"{stem}.service"
    _assert_native_contribution_refuses(
        QuadletContainer.model_validate(
            {
                "description": "Reserved unit reference",
                "image": "fixture/image",
                "container_name": "lychd-sidecar",
                field: value if field in {"pod", "pod_service"} else [value],
            }
        )
    )


def _assert_native_contribution_refuses(container: QuadletContainer) -> None:
    context = ExtensionContext()
    context.registration_view("crypt:reserved-vessel-test").transmutation.add_contributor(
        _SecretContributor(containers=(container,)),
    )
    use_case, secrets, scribe, systemd = _bind_ports(set())

    def apply_contribution() -> None:
        request = compile_bind_request(
            settings=Settings(),
            extensions=AssembledExtensions(context=context),
            runes=RuneRegistry(()),
            soulstones=(),
            portals=(),
            uncaged=False,
            native_only=True,
        )
        use_case.apply(request, use_case.plan(request))

    with pytest.raises(ValueError, match="reserved Vessel identities"):
        apply_contribution()
    secrets.exists.assert_not_called()
    secrets.ensure_present.assert_not_called()
    scribe.plan_reconcile_all.assert_not_called()
    scribe.reconcile_all.assert_not_called()
    systemd.daemon_reload.assert_not_called()


def test_native_only_preserves_an_ordinary_contribution_under_its_own_identity() -> None:
    container = QuadletContainer(
        description="Ordinary contribution",
        image="fixture/image",
        container_name="lychd-sidecar",
        pod="lychd.pod",
    )
    context = ExtensionContext()
    context.registration_view("crypt:ordinary-sidecar").transmutation.add_contributor(
        _SecretContributor(containers=(container,)),
    )
    settings = Settings()
    for native_only in (False, True):
        request = compile_bind_request(
            settings=settings,
            extensions=AssembledExtensions(context=context),
            runes=RuneRegistry(()),
            soulstones=(),
            portals=(),
            uncaged=False,
            native_only=native_only,
        )
        assert container in request.manifests
        assert ("lychd-uncaged-vessel.service" in request.plain_unit_mapping()) is native_only
