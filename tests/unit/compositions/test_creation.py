"""Boundaries and actual agent assembly with inert Pydantic AI responses."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from lychd.agents.context_limits import limit_model_requests
from lychd.agents.the_first_one import default_forge
from lychd.agents.workflows import builtin_workflow_registry
from lychd.compositions.creation.schemas import (
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    CaseView,
    CreationRefused,
    InvestigationRequest,
    canonical,
    decode,
    digest,
    validate_output,
)
from lychd.compositions.creation.services import (
    CONSTRUCTOR,
    VERIFIER,
    InvestigationService,
    LocalCreationPolicy,
    ObservedModel,
    Role,
    check_packets,
    register_agents,
    require_profile,
)
from lychd.compositions.creation.workflow import investigation_workflow
from lychd.config.settings.weaver import CreationCastingSettings, WeaverSettings
from lychd.domain.animation.capabilities import CapabilitySpec
from lychd.domain.codex.local_access import LocalOperatorAccess, LocalOperatorAuthentication
from lychd.domain.codex.sigil import Sigil, default_local_sigil
from lychd.domain.cortex.leases import LeaseLedger
from lychd.interface.web.altar_services import _build_creation  # pyright: ignore[reportPrivateUsage]
from tests.support.creation import (
    CreationDispatcher,
    CreationRegistry,
    candidate_document,
    capability,
    request_document,
    verdict_document,
)

ACCESS_VALUE = "inert-test-password"


def test_exact_profile_two_budget_manifest_and_selector() -> None:
    service, _common = stage_fixture(StageStore())
    profile = service.profile()
    assert (profile["composition"], profile["revision"], profile["profile"]) == (
        "creation.investigation",
        "2",
        "public-source-qwen@2",
    )
    assert profile["pattern"]["revision"] == "2"
    assert profile["pattern"]["implementation_revision"] == "py.2"
    assert profile["pattern"]["checkpoint_schema"] == "creation-investigation-state-v1"
    assert profile["context_tokens"] == INPUT_TOKENS + OUTPUT_TOKENS == 4096
    assert INPUT_TOKENS == 3584
    assert profile["output_tokens"] == CONSTRUCTOR.max_tokens == VERIFIER.max_tokens == OUTPUT_TOKENS == 512
    assert profile["role_seconds"] == profile["recovery_seconds"] == 30
    assert profile["lifetime_seconds"] == 180
    assert profile["agent_retries"] == profile["sdk_retries"] == CONSTRUCTOR.retries == VERIFIER.retries == 0
    assert profile["precount_available"] is False
    assert WeaverSettings().creation is None
    assert CreationCastingSettings(revision="2", capability_key=capability().key).revision == "2"
    with pytest.raises(ValidationError):
        CreationCastingSettings.model_validate({"revision": "1", "capability_key": capability().key})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_tokens", 256),
        ("max_tokens", 511),
        ("max_tokens", 513),
        ("max_context", 4095),
        ("max_context", 4097),
        ("temperature", 0.1),
        ("top_p", 0.9),
        ("thinking", None),
    ],
)
def test_profile_two_refuses_old_or_nearby_generation_bindings(field: str, value: Any) -> None:
    data = capability().model_dump(mode="json")
    data["generation_profile"][field] = value
    with pytest.raises(CreationRefused, match="unsupported_capability"):
        require_profile(CapabilitySpec.model_validate(data))


def test_selected_assembly_registers_only_profile_two_and_omission_stays_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise the exact private opt-in assembly without opening a database.
    monkeypatch.setattr("lychd.db.engine.get_session_factory", object)
    selection = WeaverSettings(creation=CreationCastingSettings(revision="2", capability_key=capability().key))
    common = {
        "persistence": "postgres",
        "access": LocalOperatorAccess(access_password=ACCESS_VALUE),
        "registry": CreationRegistry(),
        "leases": LeaseLedger(),
        "workflows": builtin_workflow_registry(),
    }
    service, registered, _forge = _build_creation(
        settings=cast("Any", SimpleNamespace(weaver=selection)), **cast("Any", common)
    )
    assert service is not None
    assert registered.get("creation.investigate") is registered.get_revision("creation.investigate", "2")
    assert registered.is_active("creation.investigate", "2")
    assert registered.get_revision("creation.investigate", "1") is None
    disabled, original, _forge = _build_creation(
        settings=cast("Any", SimpleNamespace(weaver=WeaverSettings())), **cast("Any", common)
    )
    assert disabled is None
    assert original is common["workflows"]
    assert original.get("creation.investigate") is None


@pytest.mark.parametrize("change", ["path", "duplicate", "bytes", "public", "question"])
def test_input_boundary_refuses_ambiguous_or_unsupported_public_source(change: str) -> None:
    data = request_document().model_dump(mode="json")
    if change == "path":
        data["sources"][0]["name"] = "../private.py"
    elif change == "duplicate":
        data["sources"] *= 2
    elif change == "bytes":
        data["sources"][0]["text"] = "é" * 2049
    elif change == "public":
        data["public_material"] = 1
    else:
        data["question"] = "é" * 257
    with pytest.raises((ValidationError, CreationRefused)):
        InvestigationRequest.model_validate(data)


def test_duplicate_keys_and_citation_byte_spans_are_deterministic_not_a_semantic_oracle() -> None:
    with pytest.raises(CreationRefused):
        decode(b'{"question":"one","question":"two"}')
    request = request_document()
    # This intentionally wrong counterexample passes the citation gate; the separate verdict refutes its predicate.
    receipt = decode(validate_output(request, candidate_document()))
    span = receipt["citations"][0]
    assert span["start"] == 0
    assert span["stop"] == len(request.sources[0].text.encode())
    assert receipt["semantic_correctness"] == "not_established"
    bad = candidate_document().model_dump(mode="json")
    bad["findings"][0]["citations"][0]["last_line"] = 3
    with pytest.raises(CreationRefused, match="citation_span"):
        validate_output(request, type(candidate_document()).model_validate(bad))
    assert verdict_document().disposition == "rejected"


def test_role_policy_requires_actual_current_marker_not_sigil_or_reconstructed_fields() -> None:
    access = LocalOperatorAccess(access_password=ACCESS_VALUE)
    actual = access.authenticate(username=b"magus", password=b"inert-test-password")
    assert actual is not None
    policy = LocalCreationPolicy(access)
    fake = LocalOperatorAuthentication(
        subject=actual.subject, authentication_generation=actual.authentication_generation
    )
    for marker, principal in [(fake, default_local_sigil()), (actual, Sigil(name="scope-only", scopes=frozenset()))]:
        with pytest.raises(CreationRefused, match="local_authority"):
            policy.http(marker, principal, "read")
    assert policy.http(actual, default_local_sigil(), "read") is actual
    access.revoke()
    with pytest.raises(CreationRefused, match="local_authority"):
        policy.http(actual, default_local_sigil(), "read")


class StageStore:
    """Only records boundary ordering; PostgreSQL tests own transaction/replay evidence."""

    def __init__(self) -> None:
        self.view: CaseView | None = None
        self.events: list[str] = []
        self.responses: list[bytes] = []
        self.usages: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self.cancelled = False

    async def ready(self, _admission_id: UUID, _run_id: str) -> CaseView:
        assert self.view is not None
        return self.view

    async def claim(self, _admission_id: UUID, _run_id: str, role: str, _packet: bytes) -> UUID:
        self.events.append(f"claim:{role}")
        return uuid4()

    async def complete(self, _attempt_id: UUID, response: bytes, usage: dict[str, Any]) -> None:
        self.events.append("complete")
        self.responses.append(response)
        self.usages.append(usage)

    async def overflow(self, _attempt_id: UUID, metadata: dict[str, Any]) -> None:
        self.events.append("overflow")
        assert metadata["response_length"] > 65536

    async def adopt(self, _attempt_id: UUID, _run_id: str, output: bytes, _receipt: bytes) -> None:
        assert self.responses
        assert self.events[-1] == "complete"
        if self.cancelled:
            code = "run_fence"
            raise CreationRefused(code)
        self.events.append("adopt")
        assert self.view is not None
        document = decode(output)
        field = "verdict" if "disposition" in document else "candidate"
        self.view = self.view.model_copy(update={field: document})

    async def fail(self, _admission_id: UUID, reason: str, _attempt_id: UUID | None) -> None:
        self.events.append("fail")
        self.failures.append(reason)


def stage_fixture(store: StageStore) -> tuple[InvestigationService, Any]:
    access = LocalOperatorAccess(access_password=ACCESS_VALUE)
    policy = LocalCreationPolicy(access)
    leases = LeaseLedger()
    service = InvestigationService(
        store=store, policy=policy, registry=CreationRegistry(), leases=leases, capability_key=capability().key
    )
    investigation_workflow(service)
    request = request_document()
    store.view = CaseView(
        case_id=request.request_id,
        request_digest=digest(canonical(request)),
        profile_digest=digest(canonical(service.profile())),
        status="reserved",
        deadline=(datetime.now(UTC) + timedelta(seconds=180)).isoformat(),
        slot_encumbered=True,
        request=request,
        profile=service.profile(),
        admissions=(),
        attempts=(),
        candidate=None,
        candidate_digest=None,
        verdict=None,
        reason=None,
    )
    forge = default_forge()
    register_agents(forge)
    common = SimpleNamespace(
        forge=forge, dispatcher=None, orchestrator=None, context=None, sigil_provider=default_local_sigil
    )
    return service, common


@pytest.mark.asyncio
async def test_two_real_agent_assemblies_have_fresh_messages_and_custody_before_adoption() -> None:
    store = StageStore()
    service, common = stage_fixture(store)
    messages: list[bytes] = []

    async def respond(history: Any, info: Any) -> ModelResponse:
        assert info.model_settings["max_tokens"] == 512
        assert info.model_settings["temperature"] == 0
        assert info.model_settings["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
        messages.append(ModelMessagesTypeAdapter.dump_json(history))
        output = candidate_document() if len(messages) == 1 else verdict_document()
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result",
                    output.model_dump(mode="json"),
                    tool_call_id="private-constructor-call" if len(messages) == 1 else "review-call",
                )
            ],
            usage=RequestUsage(input_tokens=3584, output_tokens=512),
        )

    common.dispatcher = CreationDispatcher(FunctionModel(respond), service.leases)
    check_packets(cast("CaseView", store.view).request)
    await service.stage(uuid4(), str(uuid4()), "constructor", common)
    await service.stage(uuid4(), str(uuid4()), "verifier", common)
    assert store.events == ["claim:constructor", "complete", "adopt", "claim:verifier", "complete", "adopt"]
    assert CONSTRUCTOR.instructions.encode() in messages[0]
    assert VERIFIER.instructions.encode() in messages[1]
    assert CONSTRUCTOR.instructions.encode() not in messages[1]
    assert b"private-constructor-call" not in messages[1]
    assert candidate_document().summary.encode() in messages[1]
    assert len(store.responses) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["constructor", "verifier"])
@pytest.mark.parametrize("binding", ["changed", "missing"])
async def test_profile_drift_after_complete_model_response_refuses_adoption_without_another_call(
    role: Role, binding: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StageStore()
    service, common = stage_fixture(store)
    assert store.view is not None
    original_candidate = candidate_document().model_dump(mode="json") if role == "verifier" else None
    store.view = store.view.model_copy(
        update={"candidate": original_candidate, "status": "candidate" if role == "verifier" else "reserved"}
    )
    original_profile = canonical(store.view.profile)
    replacement = capability().model_copy(update={"registrant_id": "changed-registration"})
    assert require_profile(replacement) is replacement
    registry = SimpleNamespace(get_capability=Mock(return_value=replacement if binding == "changed" else None))
    complete = store.complete

    async def complete_then_change(attempt_id: UUID, response: bytes, usage: dict[str, Any]) -> None:
        await complete(attempt_id, response, usage)
        service.registry = registry

    monkeypatch.setattr(store, "complete", complete_then_change)
    output = candidate_document() if role == "constructor" else verdict_document()
    calls: list[str] = []

    async def respond(_messages: Any, _info: Any) -> ModelResponse:
        calls.append(role)
        return ModelResponse(
            parts=[ToolCallPart("final_result", output.model_dump(mode="json"), tool_call_id="complete-call")],
            usage=RequestUsage(input_tokens=100, output_tokens=80),
            finish_reason="stop",
            provider_response_id="complete-before-profile-drift",
        )

    common.dispatcher = CreationDispatcher(FunctionModel(respond), service.leases)
    await service.stage(uuid4(), str(uuid4()), role, common)
    assert calls == [role]
    assert store.events == [f"claim:{role}", "complete", "fail"]
    assert store.failures == ["profile_changed" if binding == "changed" else "unsupported_capability"]
    assert store.view.candidate == original_candidate
    assert store.view.verdict is None
    assert canonical(store.view.profile) == original_profile
    assert len(common.dispatcher.calls) == 1
    assert not service.leases.active()
    registry.get_capability.assert_called_once_with(capability().key)
    assert len(store.responses) == 1
    retained = ModelMessagesTypeAdapter.validate_json(store.responses[0])[0]
    assert isinstance(retained, ModelResponse)
    assert retained.provider_response_id == "complete-before-profile-drift"
    assert retained.finish_reason == "stop"
    assert retained.usage.input_tokens == 100
    assert retained.usage.output_tokens == 80
    assert store.usages == [{"input_tokens": 100, "output_tokens": 80, "precount_available": False}]
    part = retained.parts[0]
    assert isinstance(part, ToolCallPart)
    assert part.tool_call_id == "complete-call"
    assert part.args == output.model_dump(mode="json")


@pytest.mark.asyncio
async def test_cancel_wins_adoption_but_keeps_complete_late_response() -> None:
    store = StageStore()
    service, common = stage_fixture(store)

    async def respond(_history: Any, _info: Any) -> ModelResponse:
        store.cancelled = True
        return ModelResponse(
            parts=[ToolCallPart("final_result", candidate_document().model_dump(mode="json"))],
            usage=RequestUsage(input_tokens=100, output_tokens=80),
        )

    common.dispatcher = CreationDispatcher(FunctionModel(respond), service.leases)
    await service.stage(uuid4(), str(uuid4()), "constructor", common)
    assert store.events == ["claim:constructor", "complete", "fail"]
    assert store.responses
    assert cast("CaseView", store.view).candidate is None


@pytest.mark.asyncio
async def test_complete_response_custody_survives_context_guard_failure() -> None:
    store = StageStore()

    async def before() -> None:
        store.events.append("before")

    async def respond(_messages: Any, _info: Any) -> ModelResponse:
        return ModelResponse(parts=[TextPart("complete")], usage=RequestUsage(input_tokens=4000, output_tokens=3))

    model = FunctionModel(respond)
    observed = ObservedModel(model, store=store, attempt_id=uuid4(), before=before)
    wrapped, _limits = limit_model_requests(
        observed, context_window=4096, output_reserve=OUTPUT_TOKENS, counter_model=model
    )
    assert wrapped is not None
    with pytest.raises(UsageLimitExceeded, match="input_tokens_limit"):
        await wrapped.request([], None, ModelRequestParameters())
    assert store.events == ["before", "complete"]
    assert b"complete" in store.responses[0]


@pytest.mark.asyncio
async def test_invalid_complete_output_gets_no_validation_repair_call() -> None:
    store = StageStore()
    service, common = stage_fixture(store)
    calls: list[str] = []

    async def respond(_messages: Any, _info: Any) -> ModelResponse:
        calls.append("called")
        return ModelResponse(
            parts=[ToolCallPart("final_result", {"wrong": "schema"})],
            usage=RequestUsage(input_tokens=100, output_tokens=20),
        )

    common.dispatcher = CreationDispatcher(FunctionModel(respond), service.leases)
    await service.stage(uuid4(), str(uuid4()), "constructor", common)
    assert calls == ["called"]
    assert store.events == ["claim:constructor", "complete", "fail"]
    assert len(store.responses) == 1
    assert cast("CaseView", store.view).candidate is None


@pytest.mark.asyncio
async def test_oversized_complete_response_keeps_only_bounded_failure_metadata() -> None:
    store = StageStore()

    async def before() -> None:
        store.events.append("before")

    async def respond(_messages: Any, _info: Any) -> ModelResponse:
        return ModelResponse(parts=[TextPart("x" * 65537)], usage=RequestUsage(input_tokens=100, output_tokens=256))

    model = ObservedModel(FunctionModel(respond), store=store, attempt_id=uuid4(), before=before)
    with pytest.raises(CreationRefused, match="raw_response_bytes"):
        await model.request([], None, ModelRequestParameters())
    assert store.events == ["before", "overflow"]
    assert store.responses == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["constructor", "verifier"])
@pytest.mark.parametrize("case", ["length", "duplicate_top", "duplicate_nested"])
async def test_raw_final_result_refusal_keeps_custody_without_adoption_or_retry(
    role: Role, case: Literal["length", "duplicate_top", "duplicate_nested"]
) -> None:
    store = StageStore()
    service, common = stage_fixture(store)
    assert store.view is not None
    original_candidate = candidate_document().model_dump(mode="json") if role == "verifier" else None
    store.view = store.view.model_copy(
        update={"candidate": original_candidate, "status": "candidate" if role == "verifier" else "reserved"}
    )
    output = candidate_document() if role == "constructor" else verdict_document()
    arguments = canonical(output).decode("utf-8")
    if case == "duplicate_top":
        if role == "constructor":
            arguments = arguments.replace('"summary":', '"summary":"discarded","summary":', 1)
        else:
            arguments = arguments.replace(
                '"disposition":"rejected"', '"disposition":"rejected","disposition":"accepted"', 1
            )
    elif case == "duplicate_nested":
        line_field = f'"first_line":{output.findings[0].citations[0].first_line}'
        arguments = arguments.replace(line_field, f'"first_line":99,{line_field}', 1)
    calls: list[str] = []

    async def respond(_messages: Any, _info: Any) -> ModelResponse:
        calls.append("called")
        return ModelResponse(
            parts=[ToolCallPart("final_result", arguments, tool_call_id="retained-call")],
            usage=RequestUsage(input_tokens=100, output_tokens=256),
            finish_reason="length" if case == "length" else None,
            provider_response_id="retained-response",
            provider_details={"evidence": "retain this complete metadata"},
        )

    common.dispatcher = CreationDispatcher(FunctionModel(respond), service.leases)
    await service.stage(uuid4(), str(uuid4()), role, common)
    assert calls == ["called"]
    assert store.events == [f"claim:{role}", "complete", "fail"]
    assert store.failures == ["output_length" if case == "length" else "invalid_json"]
    assert store.usages == [{"input_tokens": 100, "output_tokens": 256, "precount_available": False}]
    assert store.view.candidate == original_candidate
    assert store.view.verdict is None
    assert len(store.responses) == 1
    retained = ModelMessagesTypeAdapter.validate_json(store.responses[0])[0]
    assert isinstance(retained, ModelResponse)
    assert retained.provider_response_id == "retained-response"
    assert retained.provider_details == {"evidence": "retain this complete metadata"}
    assert retained.finish_reason == ("length" if case == "length" else None)
    assert retained.usage.input_tokens == 100
    assert retained.usage.output_tokens == 256
    part = retained.parts[0]
    assert isinstance(part, ToolCallPart)
    assert part.tool_call_id == "retained-call"
    assert part.args == arguments
