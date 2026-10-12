"""Single-snapshot contracts for pure bind request compilation."""

import sys
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lychd.config import QuadletConfig
from lychd.config.runes.registry import RuneRegistry
from lychd.config.settings.root import Settings, get_settings
from lychd.config.settings.server import ServerSettings
from lychd.domain.animation.conflicts import ConflictTopologyError
from lychd.domain.animation.schemas import GenericSoulstoneConfig, OpenAIPortalConfig
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
