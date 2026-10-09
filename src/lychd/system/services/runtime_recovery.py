"""Verified host-only retirement of one uncertain soft activation record."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from lychd.domain.orchestration.actuator import capability_config_generation
from lychd.system.operator.process import ProcessRunner
from lychd.system.services.scribe.naming import runtime_unit_for_source
from lychd.system.services.systemctl_process import validate_systemctl_timeout
from lychd.system.unit_names import animator_service_unit, animator_target_unit

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from lychd.domain.animation.protocols import CapabilityRegistry
    from lychd.domain.orchestration.containment import SoftActivationRecord
    from lychd.system.services.runtime_guard import FileSoftActivationGuard
    from lychd.system.services.runtime_topology import RuntimeTopologyAttestor
    from lychd.system.services.scribe import OwnedBindings

_JOB_COLUMN_COUNT = 4
_VESSEL_UNITS = ("lychd-vessel.service", "lychd-uncaged-vessel.service")
_STATE_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "NeedDaemonReload",
    "DropInPaths",
    "FragmentPath",
    "SourcePath",
    "UnitFileState",
)
_SERVICE_PROPERTIES = (*_STATE_PROPERTIES, "MainPID", "ControlPID")


class SoftActivationRecovery:
    """Clear only after proving the effect owner and affected runtime are stopped.

    The pending guard fences a restarted Vessel before queue publication. Holding
    the host lifecycle lock excludes operator/Host Reactor mutations; proving the
    previous Vessel stopped excludes the only in-process activation owner. A new
    Vessel cannot arm an activation before this exact pending record is cleared.
    """

    def __init__(
        self,
        *,
        guard: FileSoftActivationGuard,
        registry: CapabilityRegistry,
        topology: RuntimeTopologyAttestor,
        owned_bindings: Callable[[], OwnedBindings],
        runner: ProcessRunner,
        systemctl_bin: str,
        systemctl_timeout_s: float = 120.0,
        lock_factory: Callable[[], AbstractContextManager[object]],
    ) -> None:
        """Bind exact guard custody, declaration truth, and trusted host observation."""
        if not Path(systemctl_bin).is_absolute():
            message = "Soft activation recovery requires an attested absolute systemctl executable."
            raise ValueError(message)
        self._guard = guard
        self._registry = registry
        self._topology = topology
        self._owned_bindings = owned_bindings
        self._runner = runner
        self._systemctl = systemctl_bin
        self._timeout = validate_systemctl_timeout(systemctl_timeout_s)
        self._lock_factory = lock_factory

    async def recover(self, activation_id: str) -> SoftActivationRecord:
        """Verify one operator-named record and retire it without runtime mutation."""
        with self._lock_factory():
            record = self._guard.pending()
            if record is None or record.activation_id != activation_id:
                message = "No exact matching pending soft activation; containment was not changed."
                raise RuntimeError(message)
            self._require_current_declaration(record)
            owned = self._owned_bindings()
            sources = self._require_owned_sources(owned)
            await self._topology.attest_current_topology()
            relevant = frozenset(
                {
                    *_VESSEL_UNITS,
                    "lychd-pod.service",
                    animator_service_unit(record.target_animator),
                    animator_target_unit(record.target_animator),
                }
            )
            # Bracket the final cold observations with pending-job checks. An
            # inactive snapshot never authorizes release while work can land.
            self._require_no_jobs(relevant)
            self._require_cold_world(record, sources)
            self._require_no_jobs(relevant)
            if self._owned_bindings() != owned:
                message = "Binding ownership changed during recovery; containment was not changed."
                raise RuntimeError(message)
            self._require_current_declaration(record)
            self._guard.clear(record)
            return record

    def _require_current_declaration(self, record: SoftActivationRecord) -> None:
        target = self._registry.get_capability(record.target_capability_key)
        if (
            target is None
            or target.animator_name != record.target_animator
            or not target.concurrency.dedicated
            or self._registry.get_soulstone_rune(record.target_animator) is None
            or capability_config_generation(self._registry) != record.config_generation
        ):
            message = "Soft activation declaration changed or is unavailable; containment was not changed."
            raise RuntimeError(message)

    @staticmethod
    def _require_owned_sources(owned: OwnedBindings) -> dict[str, Path]:
        if not owned.receipt_present or owned.generation is None:
            message = "Recovery requires validated Scribe binding ownership."
            raise RuntimeError(message)
        sources: dict[str, Path] = {}
        for source in (*owned.quadlet_sources, *owned.systemd_sources):
            unit = runtime_unit_for_source(source.name)
            if unit in sources:
                message = f"Recovery found ambiguous ownership for {unit}."
                raise RuntimeError(message)
            sources[unit] = source
        if len(set(_VESSEL_UNITS).intersection(sources)) != 1:
            message = "Recovery cannot prove one exact owned Vessel unit."
            raise RuntimeError(message)
        return sources

    def _require_cold_world(self, record: SoftActivationRecord, sources: dict[str, Path]) -> None:
        units = (
            *_VESSEL_UNITS,
            animator_service_unit(record.target_animator),
            animator_target_unit(record.target_animator),
        )
        for unit in units:
            source = sources.get(unit)
            if source is None and unit not in _VESSEL_UNITS:
                message = f"Recovery cannot prove ownership of {unit}."
                raise RuntimeError(message)
            self._require_cold_unit(unit, source=source)

    def _require_cold_unit(self, unit: str, *, source: Path | None) -> None:
        properties = _SERVICE_PROPERTIES if unit.endswith(".service") else _STATE_PROPERTIES
        payload = self._observe("show", *(f"--property={name}" for name in properties), unit)
        fields: dict[str, str] = {}
        for line in payload.splitlines():
            name, separator, value = line.partition("=")
            if not separator or name in fields:
                message = f"Recovery received malformed state for {unit}."
                raise RuntimeError(message)
            fields[name] = value
        if fields.get("Id") != unit:
            message = f"Recovery cannot prove exact unit identity for {unit}."
            raise RuntimeError(message)
        if source is None:
            if (
                fields.get("LoadState") == "not-found"
                and fields.get("ActiveState") == "inactive"
                and fields.get("SubState") == "dead"
                and fields.get("MainPID", "0") == "0"
                and fields.get("ControlPID", "0") == "0"
            ):
                return
            message = f"Unowned Vessel unit {unit} remains loaded or uncertain."
            raise RuntimeError(message)
        if not set(properties) <= fields.keys():
            message = f"Recovery observation omitted state for {unit}."
            raise RuntimeError(message)
        if (
            fields["LoadState"] != "loaded"
            or (fields["ActiveState"], fields["SubState"]) not in {("inactive", "dead"), ("failed", "failed")}
            or fields["NeedDaemonReload"] != "no"
            or fields["DropInPaths"]
            or (unit.endswith(".service") and (fields["MainPID"] != "0" or fields["ControlPID"] != "0"))
        ):
            message = f"Unit {unit} is not proved stopped without a main/control process; containment was not changed."
            raise RuntimeError(message)
        if source.suffix in {".container", ".pod"}:
            exact_source = (
                fields["SourcePath"] == str(source)
                and Path(fields["FragmentPath"]).name == unit
                and fields["UnitFileState"] == "generated"
            )
        else:
            exact_source = fields["FragmentPath"] == str(source)
        if not exact_source:
            message = f"Recovery loaded source does not match Scribe ownership for {unit}."
            raise RuntimeError(message)

    def _require_no_jobs(self, relevant: frozenset[str]) -> None:
        payload = self._observe("list-jobs", "--no-legend", "--plain", "--no-pager")
        for line in payload.splitlines():
            columns = line.split()
            if len(columns) != _JOB_COLUMN_COUNT or not columns[0].isdigit():
                message = "Recovery could not classify pending systemd jobs; containment was not changed."
                raise RuntimeError(message)
            if columns[1] in relevant:
                message = f"Pending systemd work on {columns[1]}; containment was not changed."
                raise RuntimeError(message)

    def _observe(self, *arguments: str) -> str:
        result = self._runner.run((self._systemctl, "--user", *arguments), timeout_s=self._timeout)
        if result.returncode != 0:
            message = f"Recovery systemctl {arguments[0]} observation failed; containment was not changed."
            raise RuntimeError(message)
        return result.stdout
