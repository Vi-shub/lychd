"""Read-only host and secret-boundary checks shared by bind preview and apply."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from lychd.system.constants import PATH_LYCHD_TOML, PATH_SYSTEMD_USER_UNITS_DIR
from lychd.system.path_safety import path_has_symlink_component
from lychd.system.readiness import (
    BindingFoundation,
    HostFoundationError,
    HostFoundationInspection,
    HostReadinessService,
    ReadinessState,
)

_REACTOR_DIRECTORY_MODE = 0o700

if TYPE_CHECKING:
    from lychd.config.settings.root import Settings

type BindingPreflightIssueCode = Literal[
    "codex-shape",
    "codex-mode",
    "codex-owner",
    "host-foundation",
    "legacy-vessel-unit",
    "caged-actuator",
    "reactor-shape",
    "reactor-mode",
    "reactor-owner",
    "uncaged-control-secret",
    "uncaged-web-secret",
    "uncaged-access-secret",
    "uncaged-database-secret",
    "uncaged-runtime-database-secret",
    "native-secret-capture",
    "native-admin-secret",
    "native-existing-caged",
    "native-portal-secret",
]


class HostReadinessPort(Protocol):
    """Read-only source of the one authoritative host-foundation report."""

    def inspect(self) -> HostFoundationInspection:
        """Return current capability, site, and trusted-tool evidence."""
        ...


@dataclass(frozen=True, slots=True)
class BindingPreflightIssue:
    """One stable, operator-renderable reason binding cannot proceed."""

    code: BindingPreflightIssueCode
    target: str
    detail: str


@dataclass(frozen=True, slots=True)
class BindingPreflightReport:
    """The complete read-only prerequisite result for one bind plan."""

    issues: tuple[BindingPreflightIssue, ...]
    foundation: BindingFoundation | None

    @property
    def systemctl_bin(self) -> str | None:
        """Retain the concise renderer projection of trusted systemctl."""
        return self.foundation.systemctl_bin if self.foundation is not None else None

    @property
    def ready(self) -> bool:
        """Return whether the same plan may be previewed or applied as coherent."""
        return not self.issues

    def require_ready(self) -> BindingFoundation:
        """Return every attested authority tool or fail with the full report."""
        if self.issues:
            raise BindingPreflightError(self)
        if self.foundation is None:  # pragma: no cover - enforced by the foundation issue
            msg = "A ready binding preflight must contain trusted host-foundation evidence."
            raise AssertionError(msg)
        return self.foundation


class BindingPreflightError(RuntimeError):
    """Raised after rendering when a binding preflight is not executable."""

    def __init__(self, report: BindingPreflightReport) -> None:
        """Retain the report while exposing a concise command-boundary message."""
        self.report = report
        detail = "; ".join(f"{issue.target}: {issue.detail}" for issue in report.issues)
        super().__init__(f"Binding preflight failed: {detail}")


class BindingPreflightService:
    """Inspect bind prerequisites without loading configuration or mutating the host."""

    def __init__(
        self,
        *,
        codex_path: Path = PATH_LYCHD_TOML,
        legacy_vessel_unit_path: Path = PATH_SYSTEMD_USER_UNITS_DIR / "lychd-vessel.service",
        current_uid: int | None = None,
        host_readiness: HostReadinessPort | None = None,
    ) -> None:
        """Bind inspection to explicit, replaceable host dependencies."""
        self._codex_path = codex_path
        self._legacy_vessel_unit_path = legacy_vessel_unit_path
        self._current_uid = os.getuid() if current_uid is None else current_uid
        self._host_readiness = host_readiness or HostReadinessService(
            current_uid=self._current_uid,
        )

    def inspect(
        self,
        settings: Settings,
        *,
        uncaged: bool,
        uncaged_control_plane_secrets: Sequence[str] = (),
        native_only: bool = False,
        native_portal_secrets: Sequence[str] = (),
    ) -> BindingPreflightReport:
        """Return every bind blocker observed from one already-loaded Settings object."""
        if uncaged and native_only:
            msg = "--native-only and --uncaged are mutually exclusive."
            raise ValueError(msg)
        issues = [*self._inspect_codex(), *self._inspect_legacy_vessel_unit()]
        host = self._host_readiness.inspect()
        report = host.report
        foundation: BindingFoundation | None = None
        issues.extend(
            BindingPreflightIssue(
                code="host-foundation",
                target=item.label,
                detail=item.detail,
            )
            for item in report.items
            if item.required_for_bind and item.state is not ReadinessState.VERIFIED
        )
        if not any(issue.code == "host-foundation" for issue in issues):
            try:
                foundation = host.require_ready_for_bind()
            except HostFoundationError as exc:
                issues.append(
                    BindingPreflightIssue(
                        code="host-foundation",
                        target="host foundation",
                        detail=str(exc),
                    )
                )

        if uncaged or native_only:
            issues.extend(
                self._inspect_uncaged(
                    settings,
                    control_plane_secrets=uncaged_control_plane_secrets,
                    native_only=native_only,
                )
            )
        else:
            issues.extend(self._inspect_caged(settings))

        if native_only:
            if native_portal_secrets:
                issues.append(
                    BindingPreflightIssue(
                        code="native-portal-secret",
                        target="Portal credentials",
                        detail="Native-only core capture does not transport Portal secrets.",
                    )
                )
            if foundation is not None:
                issues.extend(self._inspect_native_bindings(foundation))
            switching = settings.orchestration.switching
            if switching.actuator == "host-reactor":
                issues.extend(self._inspect_reactor_directory(switching.host_reactor_dir, label="inbox"))
                issues.extend(self._inspect_reactor_directory(switching.host_reactor_journal_dir, label="journal"))

        return BindingPreflightReport(
            issues=tuple(issues),
            foundation=foundation,
        )

    @staticmethod
    def _inspect_native_bindings(foundation: BindingFoundation) -> list[BindingPreflightIssue]:
        """Refuse caged or mixed source generations without conversion or actuation."""
        from lychd.system.services.scribe import ScribeService

        owned = ScribeService(expected_sites=foundation.sites).inspect_owned_bindings()
        caged_source = foundation.sites.quadlet.path / "lychd-vessel.container"
        if "lychd-vessel.service" not in owned.runtime_units and not os.path.lexists(caged_source):
            return []
        return [
            BindingPreflightIssue(
                code="native-existing-caged",
                target="existing Vessel binding",
                detail="Native-only bind refuses an existing caged or mixed generation; no conversion is performed.",
            )
        ]

    def _inspect_legacy_vessel_unit(self) -> list[BindingPreflightIssue]:
        """Reject the obsolete static unit before preview and locked apply."""
        if not os.path.lexists(self._legacy_vessel_unit_path):
            return []
        return [
            BindingPreflightIssue(
                code="legacy-vessel-unit",
                target=str(self._legacy_vessel_unit_path),
                detail=(
                    "legacy static unit shadows the caged Quadlet service; disable and remove it "
                    "before binding (uncaged mode uses lychd-uncaged-vessel.service)"
                ),
            )
        ]

    def _inspect_codex(self) -> list[BindingPreflightIssue]:
        issues: list[BindingPreflightIssue] = []
        if symlink := path_has_symlink_component(self._codex_path):
            return [
                BindingPreflightIssue(
                    code="codex-shape",
                    target=str(self._codex_path),
                    detail=f"Codex traverses an untrusted symlink component: {symlink}",
                )
            ]
        try:
            metadata = self._codex_path.lstat()
        except OSError as exc:
            issues.append(
                BindingPreflightIssue(
                    code="codex-shape",
                    target=str(self._codex_path),
                    detail=f"Codex must exist as a real regular file: {exc}",
                )
            )
            return issues

        if self._codex_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            issues.append(
                BindingPreflightIssue(
                    code="codex-shape",
                    target=str(self._codex_path),
                    detail="Codex must exist as a real regular file",
                )
            )
            return issues

        if metadata.st_uid != self._current_uid:
            issues.append(
                BindingPreflightIssue(
                    code="codex-owner",
                    target=str(self._codex_path),
                    detail=f"Codex is owned by uid {metadata.st_uid}; expected {self._current_uid}",
                )
            )
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o077:
            issues.append(
                BindingPreflightIssue(
                    code="codex-mode",
                    target=str(self._codex_path),
                    detail=f"Codex mode is {oct(mode)}; group and other access must be closed",
                )
            )
        return issues

    def _inspect_caged(self, settings: Settings) -> list[BindingPreflightIssue]:
        switching = settings.orchestration.switching
        issues: list[BindingPreflightIssue] = []
        if switching.actuator != "host-reactor":
            issues.append(
                BindingPreflightIssue(
                    code="caged-actuator",
                    target="orchestration.switching.actuator",
                    detail="caged binding requires 'host-reactor'",
                )
            )
        issues.extend(self._inspect_reactor_directory(switching.host_reactor_dir, label="inbox"))
        issues.extend(
            self._inspect_reactor_directory(
                switching.host_reactor_journal_dir,
                label="journal",
            )
        )
        return issues

    def _inspect_reactor_directory(
        self,
        path: Path,
        *,
        label: str,
    ) -> list[BindingPreflightIssue]:
        issues: list[BindingPreflightIssue] = []
        if symlink := path_has_symlink_component(path):
            return [
                BindingPreflightIssue(
                    code="reactor-shape",
                    target=str(path),
                    detail=(f"Host Reactor {label} traverses an untrusted symlink component: {symlink}"),
                )
            ]
        try:
            metadata = path.lstat()
        except OSError as exc:
            issues.append(
                BindingPreflightIssue(
                    code="reactor-shape",
                    target=str(path),
                    detail=f"Host Reactor {label} must exist as a real directory: {exc}",
                )
            )
            return issues

        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            issues.append(
                BindingPreflightIssue(
                    code="reactor-shape",
                    target=str(path),
                    detail=f"Host Reactor {label} must exist as a real directory",
                )
            )
            return issues

        if metadata.st_uid != self._current_uid:
            issues.append(
                BindingPreflightIssue(
                    code="reactor-owner",
                    target=str(path),
                    detail=(f"Host Reactor {label} is owned by uid {metadata.st_uid}; expected {self._current_uid}"),
                )
            )
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != _REACTOR_DIRECTORY_MODE:
            issues.append(
                BindingPreflightIssue(
                    code="reactor-mode",
                    target=str(path),
                    detail=f"Host Reactor {label} mode is {oct(mode)}; expected 0o700",
                )
            )
        return issues

    def _inspect_uncaged(
        self,
        settings: Settings,
        *,
        control_plane_secrets: Sequence[str],
        native_only: bool = False,
    ) -> list[BindingPreflightIssue]:
        """Validate captured runtime credentials and each profile's bootstrap boundary."""
        issues: list[BindingPreflightIssue] = []
        if native_only:
            capture = settings.runtime_secret_capture
            names = (
                settings.server.database.runtime_password_secret,
                settings.server.web.secret_key_secret,
                settings.server.web.access_password_secret,
            )
            if capture is None or capture.profile != "podman-core@1" or capture.names != names:
                issues.append(
                    BindingPreflightIssue(
                        code="native-secret-capture",
                        target="native runtime credentials",
                        detail="Native-only bind requires the matching explicit podman-core@1 Settings capture.",
                    )
                )
            if settings.server.database.password is not None or settings.server.database.phoenix_password is not None:
                issues.append(
                    BindingPreflightIssue(
                        code="native-admin-secret",
                        target="image-owned database credentials",
                        detail="Native-only Settings must not contain bootstrap or Phoenix database credentials.",
                    )
                )
        if control_plane_secrets:
            issues.append(
                BindingPreflightIssue(
                    code="uncaged-control-secret",
                    target="Soulstone control plane",
                    detail=(
                        "uncaged Vessel cannot receive Podman-mounted secrets: "
                        f"{', '.join(sorted(control_plane_secrets))}"
                    ),
                )
            )

        if settings.server.web.secret_key is None:
            issues.append(
                BindingPreflightIssue(
                    code="uncaged-web-secret",
                    target="application signing key",
                    detail="Required application signing key is unavailable in Settings.",
                )
            )

        if settings.server.web.access_password is None:
            issues.append(
                BindingPreflightIssue(
                    code="uncaged-access-secret",
                    target="local access password",
                    detail="Required local access password is unavailable in Settings.",
                )
            )

        if not native_only and settings.server.database.password is None:
            issues.append(
                BindingPreflightIssue(
                    code="uncaged-database-secret",
                    target="bootstrap database password",
                    detail="Required bootstrap database password is unavailable in Settings.",
                )
            )

        if settings.server.database.runtime_password is None:
            issues.append(
                BindingPreflightIssue(
                    code="uncaged-runtime-database-secret",
                    target="runtime database password",
                    detail="Required runtime database password is unavailable in Settings.",
                )
            )
        return issues
