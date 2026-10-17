"""Bounded readiness observations selecting one exact Bridge@2 admission key.

This owner never prepares a grant or enters a model. Execution keeps the existing
exact-key Dispatcher law; its observations reserve no future readiness.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, Self, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from lychd.domain.animation.capabilities import CapabilityPhase, CapabilitySpec, CapabilityState, SourceKind
from lychd.domain.animation.schemas.capability_family import CapabilityFamily
from lychd.domain.cortex.leases import AnimatorAdmission

if TYPE_CHECKING:
    from lychd.domain.animation.protocols import CapabilityRegistry
    from lychd.domain.cortex.leases import LeaseLedger

_PROBE_TIMEOUT_S = 5.0
_ROUTE_COUNT = 2
_ASCII_SPACE = 32
_ASCII_DELETE = 127
type ExclusionCode = Literal["disabled", "not_warm", "admission_closed", "readiness_changed"]


class BridgePreferenceCode(StrEnum):
    """Closed public refusal codes without operational exception text."""

    POLICY = "preference_policy"
    BINDING = "preference_binding"
    DECLARATION = "preference_declaration"
    DECLARATION_CHANGED = "preference_declaration_changed"
    PROBE_INVALID = "preference_probe_invalid"
    PROBE_FAILED = "preference_probe_failed"
    TIMEOUT = "preference_timeout"
    NO_ELIGIBLE_ROUTE = "preference_no_eligible_route"
    UNAVAILABLE = "preference_unavailable"


class BridgePreferenceError(ValueError):
    """Expose only a content-free code across the admission boundary."""

    def __init__(self, code: object) -> None:
        """Retain a closed code for public admission refusal."""
        self.code = code if isinstance(code, BridgePreferenceCode) else BridgePreferenceCode.UNAVAILABLE
        super().__init__(self.code.value)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class BridgePreferenceRoute(_Record):
    """One literal registry identity with a unitless ordinal preference."""

    capability_key: str = Field(strict=True, min_length=1, max_length=512)
    weight: int = Field(strict=True, ge=0, le=1_000_000)

    @field_validator("capability_key")
    @classmethod
    def literal_key(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < _ASCII_SPACE or ord(character) == _ASCII_DELETE for character in value
        ):
            msg = "Preference keys must be unchanged literal registry identities."
            raise ValueError(msg)
        return value


class BridgePreference(_Record):
    """The complete version-one admission policy; no execution fallback."""

    schema_version: Literal[1]
    routes: tuple[BridgePreferenceRoute, BridgePreferenceRoute]

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            msg = "Preference schema_version must be integer 1."
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def distinct_positive_policy(self) -> Self:
        if (
            len({route.capability_key for route in self.routes}) != _ROUTE_COUNT
            or sum(route.weight for route in self.routes) == 0
        ):
            msg = "Preference requires distinct routes and a positive total weight."
            raise ValueError(msg)
        return self

    def canonical_bytes(self) -> bytes:
        """Order semantically unordered routes by literal key before hashing."""
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "routes": [route.model_dump() for route in sorted(self.routes, key=lambda route: route.capability_key)],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()


class BridgeRouteObservation(_Record):
    """A server-timed observation; no provider reasons, bodies or handles."""

    capability_key: str = Field(min_length=1, max_length=512)
    phase: CapabilityPhase | None
    observed_at: AwareDatetime | None
    admission_open: bool
    exclusion_code: ExclusionCode | None

    def validate_positive(self, started: datetime, finished: datetime) -> None:
        """Check positive-route evidence without trusting unchecked model copies."""
        if self.phase is None or self.observed_at is None or not (started <= self.observed_at <= finished):
            msg = "Positive preference routes require this batch's observations."
            raise ValueError(msg)
        if self.exclusion_code is None and (self.phase is not CapabilityPhase.WARM or not self.admission_open):
            msg = "Preference selected an ineligible observation."
            raise ValueError(msg)
        if self.exclusion_code == "disabled":
            msg = "Positive preference routes cannot be disabled."
            raise ValueError(msg)
        if self.exclusion_code == "not_warm" and self.phase is CapabilityPhase.WARM:
            msg = "Warm observations cannot claim a non-warm exclusion."
            raise ValueError(msg)
        if self.exclusion_code == "admission_closed" and self.admission_open:
            msg = "Open admission cannot claim a closed-admission exclusion."
            raise ValueError(msg)


class BridgePreferenceDecision(_Record):
    """Immutable selection retained with the admitted exact execution binding."""

    policy: BridgePreference
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_started_at: AwareDatetime
    batch_finished_at: AwareDatetime
    observations: tuple[BridgeRouteObservation, BridgeRouteObservation]
    selected_capability_key: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def coherent_selection(self) -> Self:
        if self.policy_digest != self.policy.digest or self.batch_finished_at < self.batch_started_at:
            msg = "Preference decision has invalid policy or observation custody."
            raise ValueError(msg)
        observations = {observation.capability_key: observation for observation in self.observations}
        if len(observations) != _ROUTE_COUNT or set(observations) != {
            route.capability_key for route in self.policy.routes
        }:
            msg = "Preference observations must match the complete policy."
            raise ValueError(msg)
        eligible: list[BridgePreferenceRoute] = []
        for route in self.policy.routes:
            observation = observations[route.capability_key]
            if route.weight == 0:
                if (observation.phase, observation.observed_at, observation.exclusion_code) != (None, None, "disabled"):
                    msg = "Disabled preference routes cannot carry probe observations."
                    raise ValueError(msg)
                continue
            observation.validate_positive(self.batch_started_at, self.batch_finished_at)
            if observation.exclusion_code is None:
                eligible.append(route)
        if not eligible or min(eligible, key=lambda route: (-route.weight, route.capability_key)).capability_key != (
            self.selected_capability_key
        ):
            msg = "Preference selection must follow its retained eligible ordering."
            raise ValueError(msg)
        return self


def validate_retained_preference(decision: object, admitted_capability_key: object) -> BridgePreferenceDecision | None:
    """Revalidate actual custody boundaries, including unchecked model_copy inputs."""
    if decision is None:
        return None
    try:
        payload = decision.model_dump(mode="python") if isinstance(decision, BridgePreferenceDecision) else decision
        validated = BridgePreferenceDecision.model_validate(payload)
    except (ValueError, TypeError):
        raise BridgePreferenceError(BridgePreferenceCode.BINDING) from None
    if validated.selected_capability_key != admitted_capability_key:
        raise BridgePreferenceError(BridgePreferenceCode.BINDING)
    return validated


class BridgePreferenceSelector:
    """Observe the assembled registry without activation, grant or provider entry."""

    def __init__(self, registry: CapabilityRegistry, leases: LeaseLedger) -> None:
        """Bind the assembled registry and current admission owner."""
        self.registry, self.leases = registry, leases

    def _declarations(self, policy: BridgePreference) -> dict[str, CapabilitySpec]:
        declarations: dict[str, CapabilitySpec] = {}
        for route in policy.routes:
            spec = self.registry.get_capability(route.capability_key)
            if (
                spec is None
                or spec.key != f"{spec.animator_name}:{spec.family.value}:{spec.model_id}"
                or spec.key != route.capability_key
                or spec.source_kind is not SourceKind.SOULSTONE
                or spec.family is not CapabilityFamily.CHAT
                or spec.supports_tools is not True
            ):
                raise BridgePreferenceError(BridgePreferenceCode.DECLARATION)
            declarations[route.capability_key] = spec
        return declarations

    async def select(self, policy: BridgePreference) -> BridgePreferenceDecision:
        """Run one cooperative five-second probe batch, then rank current eligibility."""
        try:
            return await self._select(policy)
        except BridgePreferenceError:
            raise
        except Exception as exc:
            raise BridgePreferenceError(BridgePreferenceCode.PROBE_FAILED) from exc

    async def _select(self, policy: BridgePreference) -> BridgePreferenceDecision:
        try:
            policy = BridgePreference.model_validate(policy.model_dump(mode="python"))
        except (ValueError, TypeError):
            raise BridgePreferenceError(BridgePreferenceCode.POLICY) from None
        declarations = self._declarations(policy)
        positive = {declarations[route.capability_key].animator_name for route in policy.routes if route.weight > 0}
        catalogue = self.registry.list_capabilities()
        if not set(declarations).issubset({spec.key for spec in catalogue}):
            raise BridgePreferenceError(BridgePreferenceCode.DECLARATION)
        states: dict[str, CapabilityState] = {}
        times: dict[str, datetime] = {}
        generations: dict[str, int] = {}
        started = datetime.now(UTC)
        try:
            async with asyncio.timeout(_PROBE_TIMEOUT_S):
                for animator in sorted(positive):
                    generations[animator] = self.leases.admission_generation(animator)
                    returned = self._validate_states(
                        await self.registry.refresh_capability_states_for_animator(animator),
                        {spec.key for spec in catalogue if spec.animator_name == animator},
                    )
                    states.update({state.capability_key: state for state in returned})
                    times[animator] = datetime.now(UTC)
        except TimeoutError:
            raise BridgePreferenceError(BridgePreferenceCode.TIMEOUT) from None
        if self._declarations(policy) != declarations:
            raise BridgePreferenceError(BridgePreferenceCode.DECLARATION_CHANGED)
        observations = [
            self._observation(route, declarations[route.capability_key], states, times, generations)
            for route in sorted(policy.routes, key=lambda route: route.capability_key)
        ]
        eligible = {observation.capability_key for observation in observations if observation.exclusion_code is None}
        if not eligible:
            raise BridgePreferenceError(BridgePreferenceCode.NO_ELIGIBLE_ROUTE)
        selected = min(
            (route for route in policy.routes if route.capability_key in eligible),
            key=lambda route: (-route.weight, route.capability_key),
        )
        return BridgePreferenceDecision(
            policy=policy,
            policy_digest=policy.digest,
            batch_started_at=started,
            batch_finished_at=datetime.now(UTC),
            observations=(observations[0], observations[1]),
            selected_capability_key=selected.capability_key,
        )

    @staticmethod
    def _validate_states(returned: object, expected: set[str]) -> list[CapabilityState]:
        if not isinstance(returned, list):
            raise BridgePreferenceError(BridgePreferenceCode.PROBE_INVALID)
        states = [BridgePreferenceSelector._validate_state(state) for state in cast("list[object]", returned)]
        if (
            len({state.capability_key for state in states}) != len(states)
            or {state.capability_key for state in states} != expected
        ):
            raise BridgePreferenceError(BridgePreferenceCode.PROBE_INVALID)
        return states

    @staticmethod
    def _validate_state(state: object) -> CapabilityState:
        if not isinstance(state, CapabilityState):
            raise BridgePreferenceError(BridgePreferenceCode.PROBE_INVALID)
        try:
            return CapabilityState.model_validate(state.model_dump(mode="python"), strict=True)
        except (ValueError, TypeError):
            raise BridgePreferenceError(BridgePreferenceCode.PROBE_INVALID) from None

    def _observation(
        self,
        route: BridgePreferenceRoute,
        spec: CapabilitySpec,
        states: dict[str, CapabilityState],
        times: dict[str, datetime],
        generations: dict[str, int],
    ) -> BridgeRouteObservation:
        opened = self.leases.admission(spec.animator_name) is AnimatorAdmission.OPEN
        state = states.get(route.capability_key) if route.weight else None
        code: ExclusionCode | None = None
        if route.weight == 0:
            code = "disabled"
        elif self.leases.admission_generation(spec.animator_name) != generations[spec.animator_name]:
            code = "readiness_changed"
        elif not opened:
            code = "admission_closed"
        elif state is None or state.phase is not CapabilityPhase.WARM:
            code = "not_warm"
        return BridgeRouteObservation(
            capability_key=route.capability_key,
            phase=state.phase if state is not None else None,
            observed_at=times.get(spec.animator_name) if route.weight else None,
            admission_open=opened,
            exclusion_code=code,
        )
