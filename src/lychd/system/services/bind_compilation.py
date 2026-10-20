"""Pure declaration-to-request compilation for one bind generation."""

from __future__ import annotations

import secrets
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from lychd.system.schemas import QuadletContainer, SystemdService, podman_secret_source, systemd_executable_token
from lychd.system.services.bind import BindRequest

if TYPE_CHECKING:
    from lychd.config.runes.registry import RuneRegistry
    from lychd.config.settings.root import Settings
    from lychd.domain.animation.schemas import PortalConfig, SoulstoneConfig
    from lychd.domain.animation.services.adapters.contracts import RuntimePlan
    from lychd.extensions.host import AssembledExtensions


def required_secret_names_from_soulstones(
    soulstones: Sequence[SoulstoneConfig],
    runtime_plans: Sequence[RuntimePlan] = (),
) -> list[str]:
    """Collect every Rune-, control-, and adapter-planned Soulstone secret."""
    if runtime_plans and len(runtime_plans) != len(soulstones):
        msg = "Runtime plan count must match the Soulstone count"
        raise ValueError(msg)
    names: set[str] = set()
    for stone in soulstones:
        names.update(name for name in stone.secret_env_files.values() if name)
        names.update(stone.control_plane_secret_names)
    for plan in runtime_plans:
        names.update(podman_secret_source(spec) for spec in plan.secrets)
    return sorted(names)


def uncaged_control_plane_secret_names(
    soulstones: Sequence[SoulstoneConfig],
) -> tuple[str, ...]:
    """Return secrets that only the caged Vessel can receive from Podman."""
    return tuple(sorted({name for stone in soulstones for name in stone.control_plane_secret_names}))


def compile_bind_request(
    *,
    settings: Settings,
    extensions: AssembledExtensions,
    runes: RuneRegistry,
    soulstones: Sequence[SoulstoneConfig],
    portals: Sequence[PortalConfig],
    uncaged: bool,
    native_only: bool = False,
) -> BindRequest:
    """Compile one caged, dual, or explicitly native-only complete binding generation."""
    if uncaged and native_only:
        msg = "--native-only and --uncaged are mutually exclusive."
        raise ValueError(msg)
    from lychd.domain.animation.capabilities import CapabilitySpec
    from lychd.domain.animation.conflicts import require_soulstone_capability_coverage
    from lychd.domain.animation.services.adapters.registry import (
        RuntimeAdapterRegistry,
    )
    from lychd.domain.animation.transmute import Transmuter

    runtime_planner = RuntimeAdapterRegistry(
        soulstone_registrations=extensions.soulstone_registrations,
    )
    runtime_plans = tuple(runtime_planner.plan(stone) for stone in soulstones)
    capability_specs: list[CapabilitySpec] = []
    for stone in soulstones:
        runtime = runtime_planner.build_runtime(stone)
        if runtime is not None:
            capability_specs.extend(runtime_planner.build_capability_specs(runtime))
    require_soulstone_capability_coverage(
        soulstones,
        capability_animator_names=(spec.animator_name for spec in capability_specs),
    )
    required_soulstone_secrets = required_secret_names_from_soulstones(
        soulstones,
        runtime_plans,
    )
    portal_secrets = {portal.api_key_secret_name for portal in portals if portal.api_key_secret_name is not None}
    manifests = Transmuter(
        settings=settings,
        runtime_planner=runtime_planner,
        contributors=extensions.quadlet_contributors,
    ).transmute_all(
        tuple(soulstones),
        portals=tuple(portals),
        runes=runes,
        runtime_plans=runtime_plans,
        native_only=native_only,
    )
    core_secret_factories = {
        settings.server.web.secret_key_secret: (lambda: secrets.token_hex(32)),
        settings.server.database.password_secret: (lambda: secrets.token_urlsafe(16)),
        settings.server.database.runtime_password_secret: (lambda: secrets.token_urlsafe(32)),
        settings.server.database.phoenix_password_secret: (lambda: secrets.token_urlsafe(32)),
        settings.server.web.access_password_secret: (lambda: secrets.token_urlsafe(32)),
    }
    native_runtime_secrets = (
        (
            settings.server.database.runtime_password_secret,
            settings.server.web.secret_key_secret,
            settings.server.web.access_password_secret,
        )
        if native_only
        else ()
    )
    for name in native_runtime_secrets:
        del core_secret_factories[name]
    manifest_secrets = {
        podman_secret_source(spec)
        for manifest in manifests
        if isinstance(manifest, QuadletContainer)
        for spec in manifest.secrets
    }
    # Only emitted references inherit generation authority; explicit operator
    # requirements must retain BindRequest's core/operator overlap refusal.
    manifest_requirements = manifest_secrets.difference(core_secret_factories)
    return BindRequest.compile(
        manifests=manifests,
        plain_units=_desired_plain_units(
            settings=settings,
            uncaged=uncaged,
            native_only=native_only,
        ),
        core_secret_factories=core_secret_factories,
        required_secret_names=sorted(
            {
                *required_soulstone_secrets,
                *portal_secrets,
                *manifest_requirements,
                *native_runtime_secrets,
            }
        ),
    )


def _host_reactor_units(*, settings: Settings) -> dict[str, str]:
    """Render the host-only trigger/consumer into the desired plain-unit set."""
    from lychd.system.constants import (
        PATH_XDG_CACHE_HOME,
        PATH_XDG_CONFIG_HOME,
        PATH_XDG_DATA_HOME,
    )
    from lychd.system.services.reactor import (
        render_reactor_path_unit,
        render_reactor_service_unit,
    )

    executable = Path(sys.prefix) / "bin" / "lychd"
    environment = {
        "HOME": str(Path.home()),
        "XDG_CACHE_HOME": str(PATH_XDG_CACHE_HOME),
        "XDG_CONFIG_HOME": str(PATH_XDG_CONFIG_HOME),
        "XDG_DATA_HOME": str(PATH_XDG_DATA_HOME),
    }
    return {
        "lychd-reactor.service": render_reactor_service_unit(
            executable=executable,
            environment=environment,
        ),
        "lychd-reactor.path": render_reactor_path_unit(
            inbox_dir=settings.orchestration.switching.host_reactor_dir,
            journal_dir=settings.orchestration.switching.host_reactor_journal_dir,
        ),
    }


def _compile_uncaged_vessel_unit(*, settings: Settings, native_only: bool = False) -> SystemdService:
    """Compile the host-native Vessel with one literal installed executable token."""
    executable = Path(sys.prefix) / "bin" / "lychd"
    environment = {"LYCHD_MODE": "uncaged"}
    dependencies: list[str] = []
    if native_only:
        from lychd.system.constants import PATH_XDG_CACHE_HOME, PATH_XDG_CONFIG_HOME, PATH_XDG_DATA_HOME

        environment.update(
            {
                "HOME": str(Path.home()),
                "XDG_CACHE_HOME": str(PATH_XDG_CACHE_HOME),
                "XDG_CONFIG_HOME": str(PATH_XDG_CONFIG_HOME),
                "XDG_DATA_HOME": str(PATH_XDG_DATA_HOME),
                "LYCHD_RUNTIME_SECRETS": "podman-core@1",
                "SERVER__DATABASE__HOST": "127.0.0.1",
                "SERVER__DATABASE__PORT": str(settings.server.database.port),
            }
        )
        dependencies = ["lychd-migrate.service"]
        if settings.orchestration.switching.actuator == "host-reactor":
            dependencies.append("lychd-reactor.path")
    return SystemdService(
        name="lychd-uncaged-vessel",
        description="LychD Vessel (uncaged)",
        exec_start=f"{systemd_executable_token(executable)} serve --host 127.0.0.1 --port {settings.server.port}",
        environment=environment,
        requires=dependencies,
        after=dependencies,
    )


def _desired_plain_units(
    *,
    settings: Settings,
    uncaged: bool,
    native_only: bool = False,
) -> dict[str, str]:
    """Compile the complete non-Quadlet unit set for one binding plan."""
    plain_units = (
        _host_reactor_units(settings=settings) if settings.orchestration.switching.actuator == "host-reactor" else {}
    )
    if uncaged or native_only:
        service = _compile_uncaged_vessel_unit(settings=settings, native_only=native_only)
        plain_units[service.filename] = service.render()
    return plain_units


__all__ = [
    "compile_bind_request",
    "required_secret_names_from_soulstones",
    "uncaged_control_plane_secret_names",
]
