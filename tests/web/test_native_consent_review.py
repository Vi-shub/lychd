"""Reviewer-aware native disclosure and refusal across the real Bridge HTTP handlers."""

# Typed native rows are deliberately seeded without inventing a production creation route.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from litestar import Litestar
from litestar.connection import ASGIConnection
from litestar.datastructures import State
from litestar.middleware import DefineMiddleware
from litestar.middleware.authentication import AuthenticationResult

from lychd.agents.router import Intent
from lychd.domain.codex.ledger import _ConsentRow
from lychd.domain.codex.middleware import SigilAuthMiddleware
from lychd.domain.codex.schemas import CENSORED_VALUE, ConsentView, NativeConsentBinding
from lychd.domain.codex.sigil import Sigil
from lychd.domain.cortex.events import RunEvent, RunEventKind
from lychd.domain.cortex.runs import RunStatus
from lychd.domain.web.consent_reviews import (
    ConsentReviewForbiddenError,
    ConsentReviewUnavailableError,
    NativeReviewDocument,
    NativeReviewResolution,
    ReviewFact,
)
from lychd.domain.web.projection import EventProjector
from lychd.interface.web.bridge import BridgeController
from lychd.interface.web.deps import web_dependencies
from tests.codex.test_native_consent import binding
from tests.web.conftest import TEST_ACCESS_PASSWORD, TEST_AUTHORIZATION, AsgiClient
from tests.web.test_sse import _sse_events

REVIEWER = Sigil(name="eligible", scopes=frozenset({"altar:read", "runs:approve", "runs:cancel"}))
DOCUMENT = NativeReviewDocument(
    title="Prepared placement",
    facts=(
        ReviewFact(key="target", label="Target", value="(1, 2, 3)"),
        ReviewFact(key="state", label="Full block state", value="minecraft:oak_log[axis=y]"),
    ),
)


class Producer:
    """Controlled owner port; no production native producer is enabled."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.authentications: list[object | None] = []
        self.failure: str | None = None
        self.decision_allowed = True
        self.revoke_on_decide = False

    async def resolve(
        self,
        *,
        consent: ConsentView,
        reviewer: Sigil,
        purpose: Literal["view", "decide"],
        authentication: object | None = None,
    ) -> NativeReviewResolution:
        self.authentications.append(authentication)
        self.calls.append((consent.id, reviewer.name, purpose))
        if reviewer.name != "eligible" or self.failure == "forbidden":
            raise ConsentReviewForbiddenError
        if self.failure == "failure":
            message = "private-owner-details"
            raise RuntimeError(message)
        if self.failure == "incomplete":
            raise ConsentReviewUnavailableError
        assert consent.native_binding is not None
        value = consent.native_binding
        document = DOCUMENT
        if self.failure == "binding":
            value = replace(value, review_producer_revision="different")
        if self.failure == "digest":
            document = replace(document, title="Changed after preparation")
        if purpose == "decide" and self.revoke_on_decide:
            self.failure = "forbidden"
        return NativeReviewResolution(binding=value, document=document, decision_allowed=self.decision_allowed)


def _client(services: SimpleNamespace, reviewers: list[Sigil]) -> AsgiClient:
    class ReviewerMiddleware(SigilAuthMiddleware):
        async def authenticate_request(self, connection: ASGIConnection[Any, Any, Any, Any]) -> AuthenticationResult:
            result = await super().authenticate_request(connection)
            # Trusted test injection precedes route guards; clients cannot choose it.
            return AuthenticationResult(user=reviewers[0], auth=result.auth)

    app = Litestar(
        route_handlers=[BridgeController],
        dependencies=web_dependencies,
        middleware=[DefineMiddleware(ReviewerMiddleware, access_password=TEST_ACCESS_PASSWORD)],
        state=State({"services": services}),
    )
    return AsgiClient(app, headers={"authorization": TEST_AUTHORIZATION})


def _seed(services: SimpleNamespace, producer: Producer | None) -> tuple[str, str, NativeConsentBinding]:
    async def seed() -> tuple[str, str, NativeConsentBinding]:
        session = await services.bridge_sessions.create_session()
        run_id = "native-run"
        await services.ledger.create(
            Intent(session_id=session.id, run_id=run_id, prompt="p", source="bridge"),
            workflow_name="bridge_chat",
            queue_name="runs",
            priority=70,
        )
        await services.ledger.set_status(run_id, RunStatus.RUNNING)
        value = binding(review_digest=DOCUMENT.digest)
        services.consents._rows["native"] = _ConsentRow(
            id="native",
            run_id=run_id,
            tool_name="place",
            args={"secret": "never-disclose-secret"},
            call_ids=(),
            native_review_required=True,
            native_binding=value,
        )
        services.projector = EventProjector(
            fragments=services.fragments,
            sessions=services.bridge_sessions,
            consents=services.consents,
            consent_reviews=producer,
        )
        return session.id, run_id, value

    return asyncio.run(seed())


def test_review_document_uses_exact_bytes_and_rejects_partial_or_unbounded_values() -> None:
    canonical = b'{"facts":[{"key":"target","label":"Target","value":"(1, 2, 3)"},{"key":"state","label":"Full block state","value":"minecraft:oak_log[axis=y]"}],"schema_version":1,"title":"Prepared placement"}'
    assert DOCUMENT.canonical_bytes() == canonical
    assert DOCUMENT.digest == hashlib.sha256(b"lychd.consent-review.v1\n" + canonical).hexdigest()
    for kwargs in (
        {"title": ""},
        {"facts": ()},
        {"facts": (DOCUMENT.facts[0], DOCUMENT.facts[0])},
        {"facts": tuple(ReviewFact(key=str(i), label="l", value="é" * 8192) for i in range(5))},
    ):
        with pytest.raises(ValueError, match=r"[Nn]ative review"):
            replace(DOCUMENT, **kwargs)
    with pytest.raises(ValueError, match=r"[Nn]ative review"):
        ReviewFact(key="large", label="l", value="x" * 8193)


@pytest.mark.parametrize("failure", [None, "forbidden", "failure", "incomplete", "binding", "digest"])
def test_all_native_snapshot_failures_are_unavailable_and_censored(
    fake_services: SimpleNamespace, failure: str | None
) -> None:
    producer = Producer() if failure is not None else None
    if producer:
        producer.failure = failure
    session_id, _, _ = _seed(fake_services, producer)
    client = _client(fake_services, [REVIEWER])
    for route in ("/api/v1/bridge", f"/api/v1/bridge/sessions/{session_id}"):
        response = client.get(route)
        assert response.status_code == 200
        card = response.json()["pending_consents"][0]
        assert card["native_review_status"] == "unavailable"
        assert card["native_review"] is None
        assert card["review_binding_digest"] is None
        assert card["can_decide"] is False
        assert card["args"] == {"secret": CENSORED_VALUE}
        assert "private-owner-details" not in response.text
        assert "never-disclose-secret" not in response.text


def test_snapshots_stream_replay_and_direct_verdict_share_exact_review_and_reviewer(
    fake_services: SimpleNamespace,
) -> None:
    producer = Producer()
    session_id, run_id, value = _seed(fake_services, producer)
    reviewers = [REVIEWER]
    client = _client(fake_services, reviewers)
    cards = [
        client.get(route).json()["pending_consents"][0]
        for route in ("/api/v1/bridge", f"/api/v1/bridge/sessions/{session_id}")
    ]
    emitter = fake_services.bus.emitter(run_id)
    emitter.emit(RunEventKind.STATUS, "waiting")
    emitter.emit(RunEventKind.CONSENT, "native")
    emitter.emit(RunEventKind.DONE, "done")
    for headers in ({}, {"Last-Event-ID": "0"}):
        response = client.get(f"/api/v1/bridge/runs/{run_id}/events", headers=headers)
        assert response.status_code == 200
        cards.append(
            next(
                item["data"]["payload"]["consent"] for item in _sse_events(response.text) if item["event"] == "consent"
            )
        )
    reviewers[0] = Sigil(name="ineligible", scopes=REVIEWER.scopes)
    refused = client.get(f"/api/v1/bridge/runs/{run_id}/events")
    hidden = next(
        item["data"]["payload"]["consent"] for item in _sse_events(refused.text) if item["event"] == "consent"
    )
    assert hidden["native_review"] is None
    reviewers[0] = REVIEWER
    response = client.post(
        "/api/v1/bridge/consents/native/decision", json={"verdict": "approve", "review_binding_digest": value.digest}
    )
    assert response.status_code == 200
    cards.append(response.json()["consent"])
    for card in cards:
        assert card["native_review_status"] == "available"
        assert card["native_review"] == json.loads(json.dumps(asdict(DOCUMENT)))
        assert card["review_binding_digest"] == value.digest
        assert card["args"] == {"secret": CENSORED_VALUE}
    assert cards[0]["can_decide"] is True
    assert cards[-1]["can_decide"] is False
    assert fake_services.run_engine.consent_resumptions == [("native", True)]
    assert ("native", "eligible", "decide") in producer.calls
    assert ("native", "ineligible", "view") in producer.calls


@pytest.mark.parametrize("verdict", ["approve", "deny"])
@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        ("missing-echo", 409),
        ("stale-echo", 409),
        ("absent-producer", 409),
        ("forbidden", 403),
        ("cannot-decide", 403),
        ("failure", 503),
        ("incomplete", 409),
        ("binding", 409),
        ("digest", 409),
        ("malformed-binding", 409),
    ],
)
def test_native_refusal_does_not_mutate_verdict_run_or_resume(
    fake_services: SimpleNamespace, verdict: str, failure: str, expected_status: int
) -> None:
    producer = Producer()
    _, run_id, value = _seed(fake_services, None if failure == "absent-producer" else producer)
    producer.failure = failure
    producer.decision_allowed = failure != "cannot-decide"
    if failure == "malformed-binding":
        fake_services.consents._rows["native"].native_binding = None
    before_consent = asyncio.run(fake_services.consents.get("native"))
    before_run = asyncio.run(fake_services.ledger.get(run_id))
    body = {"verdict": verdict}
    if failure != "missing-echo":
        body["review_binding_digest"] = "0" * 64 if failure == "stale-echo" else value.digest
    response = _client(fake_services, [REVIEWER]).post("/api/v1/bridge/consents/native/decision", json=body)
    assert response.status_code == expected_status
    assert "private-owner-details" not in response.text
    assert asyncio.run(fake_services.consents.get("native")) == before_consent
    assert asyncio.run(fake_services.ledger.get(run_id)) == before_run
    assert fake_services.run_engine.consent_resumptions == []


def test_native_first_verdict_retry_rechecks_owner_and_retains_first_attribution(
    fake_services: SimpleNamespace,
) -> None:
    producer = Producer()
    _, _, value = _seed(fake_services, producer)
    client = _client(fake_services, [REVIEWER])

    def decide(verdict: str) -> Any:
        return client.post(
            "/api/v1/bridge/consents/native/decision", json={"verdict": verdict, "review_binding_digest": value.digest}
        )

    assert decide("deny").status_code == 200
    first = asyncio.run(fake_services.consents.get("native"))
    producer.failure = "forbidden"
    assert decide("approve").status_code == 403
    assert fake_services.run_engine.consent_resumptions == [("native", False)]
    producer.failure = None
    assert decide("approve").json()["consent"]["state"] == "refused"
    assert asyncio.run(fake_services.consents.get("native")) == first
    assert fake_services.run_engine.consent_resumptions == [("native", False), ("native", False)]


def test_post_verdict_projection_rechecks_disclosure_and_read_scope_cannot_judge(
    fake_services: SimpleNamespace,
) -> None:
    producer = Producer()
    _, _, value = _seed(fake_services, producer)
    reader = replace(REVIEWER, scopes=frozenset({"altar:read"}))
    client = _client(fake_services, [reader])
    card = client.get("/api/v1/bridge").json()["pending_consents"][0]
    assert card["native_review_status"] == "available"
    assert card["can_decide"] is False
    assert (
        client.post(
            "/api/v1/bridge/consents/native/decision",
            json={"verdict": "approve", "review_binding_digest": value.digest},
        ).status_code
        == 403
    )
    producer.revoke_on_decide = True
    response = _client(fake_services, [REVIEWER]).post(
        "/api/v1/bridge/consents/native/decision", json={"verdict": "approve", "review_binding_digest": value.digest}
    )
    assert response.status_code == 200
    assert response.json()["consent"]["native_review"] is None
    assert fake_services.run_engine.consent_resumptions == [("native", True)]


def test_foreign_run_event_never_calls_producer_or_discloses_card(fake_services: SimpleNamespace) -> None:
    producer = Producer()
    _seed(fake_services, producer)
    for identifier in ("native", "missing"):
        event = RunEvent(run_id="foreign", seq=0, kind=RunEventKind.CONSENT, data=identifier)
        envelope = asyncio.run(fake_services.projector.project(event, reviewer=REVIEWER))
        assert envelope.payload == {}
    assert producer.calls == []


def test_legacy_echo_is_rejected_and_old_body_remains_compatible(fake_services: SimpleNamespace) -> None:
    _seed(fake_services, None)
    row = fake_services.consents._rows["native"]
    row.native_review_required = False
    row.native_binding = None
    client = _client(fake_services, [REVIEWER])
    response = client.post(
        "/api/v1/bridge/consents/native/decision", json={"verdict": "approve", "review_binding_digest": "a" * 64}
    )
    assert response.status_code == 409
    assert fake_services.run_engine.consent_resumptions == []
    response = client.post("/api/v1/bridge/consents/native/decision", json={"verdict": "approve"})
    assert response.status_code == 200
    assert response.json()["consent"]["native_review_status"] == "not_required"
    assert fake_services.run_engine.consent_resumptions == [("native", True)]


def test_binding_change_during_owner_resolution_refuses_before_verdict(
    fake_services: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer = Producer()
    _, _, value = _seed(fake_services, producer)
    resolve = producer.resolve

    async def changed(
        *,
        consent: ConsentView,
        reviewer: Sigil,
        purpose: Literal["view", "decide"],
        authentication: object | None = None,
    ) -> NativeReviewResolution:
        result = await resolve(consent=consent, reviewer=reviewer, purpose=purpose, authentication=authentication)
        fake_services.consents._rows["native"].native_binding = replace(value, preparation_digest="d" * 64)
        return result

    monkeypatch.setattr(producer, "resolve", changed)
    response = _client(fake_services, [REVIEWER]).post(
        "/api/v1/bridge/consents/native/decision", json={"verdict": "approve", "review_binding_digest": value.digest}
    )
    assert response.status_code == 409
    assert fake_services.consents._rows["native"].status == "pending"
    assert fake_services.consents._rows["native"].decided_by is None
    assert fake_services.run_engine.consent_resumptions == []


def test_run_snapshot_cancel_and_terminal_stream_pass_authenticated_reviewer(
    fake_services: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer = Producer()
    _, run_id, _ = _seed(fake_services, producer)
    reviewers = [REVIEWER]
    client = _client(fake_services, reviewers)
    seen: list[tuple[str, str]] = []
    project = fake_services.projector.project

    async def recording(event: RunEvent, *, reviewer: Sigil, authentication: object | None = None) -> Any:
        assert authentication is not None
        seen.append((str(event.kind), reviewer.name))
        return await project(event, reviewer=reviewer, authentication=authentication)

    monkeypatch.setattr(fake_services.projector, "project", recording)
    fake_services.bus.emitter(run_id).emit(RunEventKind.FRAGMENT, "{}")
    assert client.get(f"/api/v1/bridge/runs/{run_id}").status_code == 200
    assert client.post(f"/api/v1/bridge/runs/{run_id}/cancel").status_code == 200
    assert client.get(f"/api/v1/bridge/runs/{run_id}/events").status_code == 200
    assert seen == [("fragment", "eligible"), ("fragment", "eligible"), ("resync", "eligible")]
