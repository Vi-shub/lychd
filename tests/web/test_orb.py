"""Selected-Run Orb projection with explicit capture and gaps."""

# Exercise the bounded stream generator directly to control poll and disconnect timing.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from typing import TYPE_CHECKING

import pytest
from litestar.testing import RequestFactory

from lychd.agents.router import Intent
from lychd.agents.workflows import DELEGATED_RITE, BuiltinWorkflowRegistry
from lychd.agents.workflows.bridge_chat import BRIDGE_CHAT
from lychd.domain.cortex.events import RunEvent, RunEventKind
from lychd.domain.cortex.runs import RunStatus
from lychd.domain.delegation.models import DelegatedAgentProfile, DelegatedAgentRequest
from lychd.domain.web.contracts import OrbEventEnvelope
from lychd.interface.web.bridge_stream import BoundedRunEventStream
from lychd.interface.web.orb import _orb_events

if TYPE_CHECKING:
    from types import SimpleNamespace

    from litestar import Litestar
    from litestar.testing import TestClient
    from litestar.types import HTTPDisconnectEvent, Message


def _seed(services: SimpleNamespace, run_id: str) -> None:
    async def seed() -> None:
        run = await services.ledger.create(
            Intent(session_id="session-1", run_id=run_id, prompt="secret prompt", source="bridge"),
            workflow_name=BRIDGE_CHAT.name,
            pattern_manifest=BRIDGE_CHAT.manifest.snapshot(),
            queue_name="runs",
            priority=70,
        )
        await services.ledger.set_status(run.run_id, RunStatus.RUNNING)
        await services.ledger.append_event(RunEvent(run_id=run.run_id, seq=0, kind=RunEventKind.STATUS, data="running"))
        await services.ledger.append_event(
            RunEvent(
                run_id=run.run_id,
                seq=2,
                kind=RunEventKind.NODE,
                data="converse",
                meta={
                    "phase": "entered",
                    "occurrence_id": "occurrence-1",
                    "pattern_id": BRIDGE_CHAT.manifest.key,
                    "pattern_revision": BRIDGE_CHAT.manifest.revision,
                },
            )
        )
        await services.ledger.append_event(
            RunEvent(
                run_id=run.run_id,
                seq=3,
                kind=RunEventKind.LOG,
                data="raw secret that must not be projected",
                meta={"level": "warning"},
            )
        )

    asyncio.run(seed())


def test_selected_run_projects_exact_pattern_and_honest_gap(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
) -> None:
    _seed(fake_services, "run-orb")

    response = altar_client.get("/api/v1/orb/runs/run-orb")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["run_id"] == "run-orb"
    assert body["pattern"]["exact"] is True
    assert body["pattern"]["loom_path"] == "/loom/bridge_chat/1?run=run-orb"
    assert body["workflow"]["pattern_id"] == "bridge_chat"
    assert body["workflow"]["revision"] == "1"
    assert body["workflow"]["digest"] == body["pattern"]["digest"]
    assert body["capture"] == "process_local"
    assert body["live_tail"] == "ledger_poll"
    assert body["ledger_head_seq"] == 3
    assert body["page_end_seq"] == 3
    assert body["has_more"] is False
    assert body["gaps"] == [{"start_seq": 1, "end_seq": 1, "classification": "unknown_or_omitted"}]
    assert body["evidence"][1]["occurrence_id"] == "occurrence-1"
    assert body["evidence"][1]["subject_key"] == "converse"
    assert "raw secret" not in response.text
    assert "Token deltas" in body["known_omissions"][0]


def test_selected_run_uses_boot_catalogue_for_exactness(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
) -> None:
    _seed(fake_services, "run-registry-mismatch")
    fake_services.workflows = BuiltinWorkflowRegistry(workflows=(DELEGATED_RITE,))

    body = altar_client.get("/api/v1/orb/runs/run-registry-mismatch").json()

    assert body["pattern"]["exact"] is True
    assert body["pattern"]["loom_path"] is None
    assert body["workflow"] is None


def test_animator_projection_retains_only_dispatch_selection_facts(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
) -> None:
    _seed(fake_services, "run-animator")

    async def seed_dispatch() -> None:
        meta = {
            "animator": "local-reasoner",
            "family": "chat",
            "model_id": "model-a",
            "grant_id": "grant-a",
            "phase": "warm",
            "prompt": "private input",
            "endpoint": "private endpoint",
        }
        for seq, kind in ((4, RunEventKind.DISPATCH), (5, RunEventKind.NODE)):
            await fake_services.ledger.append_event(
                RunEvent(run_id="run-animator", seq=seq, kind=kind, data="chat-a", meta=meta)
            )

    asyncio.run(seed_dispatch())
    response = altar_client.get("/api/v1/orb/runs/run-animator")
    dispatch, node = response.json()["evidence"][-2:]
    assert dispatch["animator_name"] == "local-reasoner"
    assert dispatch["capability_family"] == "chat"
    assert dispatch["model_id"] == "model-a"
    assert dispatch["grant_id"] == "grant-a"
    assert dispatch["phase"] == "warm"
    assert all(node[key] is None for key in ("animator_name", "capability_family", "model_id", "grant_id"))
    assert "private input" not in response.text
    assert "private endpoint" not in response.text


def test_selected_run_requires_full_pattern_equality_for_loom_link(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
) -> None:
    manifest = {**BRIDGE_CHAT.manifest.snapshot(), "unregistered_field": "drift"}

    async def seed() -> None:
        await fake_services.ledger.create(
            Intent(session_id="session-1", run_id="run-full-pattern-mismatch", prompt="raise", source="bridge"),
            workflow_name=BRIDGE_CHAT.name,
            pattern_manifest=manifest,
            queue_name="runs",
            priority=70,
        )

    asyncio.run(seed())

    body = altar_client.get("/api/v1/orb/runs/run-full-pattern-mismatch").json()

    assert body["pattern"]["exact"] is True
    assert body["pattern"]["loom_path"] is None
    assert body["workflow"] is None


def test_selected_run_is_bounded_and_unknown_is_404(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
) -> None:
    _seed(fake_services, "run-page")

    first = altar_client.get("/api/v1/orb/runs/run-page?limit=1")

    assert first.status_code == 200
    assert len(first.json()["evidence"]) == 1
    assert first.json()["has_more"] is True
    assert first.json()["page_end_seq"] == 0
    assert first.json()["next_after_seq"] == 0
    assert altar_client.get("/api/v1/orb/runs/missing").status_code == 404


def test_selected_run_projects_prompt_free_delegated_job_evidence(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
) -> None:
    _seed(fake_services, "run-delegated")

    async def seed_job() -> None:
        ref = await fake_services.delegates.submit(
            DelegatedAgentRequest(
                request_id="request-secret",
                run_id="run-delegated",
                step_id="dispatch_delegate",
                runtime="reference",
                profile=DelegatedAgentProfile.READ,
                prompt="private delegated prompt",
            )
        )
        await fake_services.delegates.refresh(ref.job_id)

    asyncio.run(seed_job())

    response = altar_client.get("/api/v1/orb/runs/run-delegated")

    assert response.status_code == 200
    jobs = response.json()["delegated_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["runtime"] == "reference"
    assert jobs[0]["profile"] == "read"
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["output_present"] is True
    assert [event["status"] for event in jobs[0]["events"]] == [
        "queued",
        "admitted",
        "preparing",
        "running",
        "succeeded",
    ]
    assert "private delegated prompt" not in response.text
    assert "Reference delegate completed" not in response.text


def test_selected_run_bounds_delegated_job_cardinality(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
) -> None:
    _seed(fake_services, "run-many-delegates")

    async def seed_jobs() -> None:
        for index in range(33):
            await fake_services.delegates.submit(
                DelegatedAgentRequest(
                    request_id=f"request-many-{index:02d}",
                    run_id="run-many-delegates",
                    step_id=f"delegate-{index:02d}",
                    runtime="reference",
                    profile=DelegatedAgentProfile.READ,
                    prompt="bounded private prompt",
                )
            )

    asyncio.run(seed_jobs())

    body = altar_client.get("/api/v1/orb/runs/run-many-delegates").json()

    assert len(body["delegated_jobs"]) == 32
    assert body["delegated_jobs"][0]["request_id"] == "request-many-01"
    assert body["delegated_jobs"][-1]["request_id"] == "request-many-32"
    assert any("Older delegated job summaries" in omission for omission in body["known_omissions"])


def _envelopes(body: str) -> list[OrbEventEnvelope]:
    return [OrbEventEnvelope.model_validate_json(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("status", [RunStatus.DONE, RunStatus.FAILED, RunStatus.CANCELLED])
def test_orb_stream_drains_bounded_retained_pages_before_terminal_settlement(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
    status: RunStatus,
) -> None:
    _seed(fake_services, "run-orb-stream")

    async def finish() -> None:
        for seq in range(4, 208):
            await fake_services.ledger.append_event(
                RunEvent(run_id="run-orb-stream", seq=seq, kind=RunEventKind.NODE, data="converse")
            )
        if status is RunStatus.CANCELLED:
            elected = await fake_services.ledger.begin_cancel("run-orb-stream")
            await fake_services.ledger.finish_cancel("run-orb-stream", enqueue_seq=elected.enqueue_seq)
        else:
            await fake_services.ledger.set_status("run-orb-stream", status)

    asyncio.run(finish())
    response = altar_client.get("/api/v1/orb/runs/run-orb-stream/events")
    envelopes = _envelopes(response.text)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [event.kind for event in envelopes] == ["evidence", "evidence", "settled"]
    assert [len(event.snapshot.evidence) for event in envelopes] == [100, 100, 7]
    assert [event.after_seq for event in envelopes] == [-1, 100, 200]
    assert [event.cursor for event in envelopes] == [100, 200, 207]
    assert all(event.run_id == "run-orb-stream" for event in envelopes)
    assert envelopes[-1].snapshot.run.status == status.value
    assert envelopes[0].snapshot.gaps[0].start_seq == 1
    assert "raw secret" not in response.text
    assert "secret prompt" not in response.text
    assert "id: 207" in response.text


@pytest.mark.parametrize("future_cursor", [999, (1 << 31) - 1])
def test_orb_reconnect_uses_last_event_id_and_future_cursor_resets(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
    future_cursor: int,
) -> None:
    _seed(fake_services, "run-orb-reconnect")
    asyncio.run(fake_services.ledger.set_status("run-orb-reconnect", RunStatus.DONE))

    response = altar_client.get(
        "/api/v1/orb/runs/run-orb-reconnect/events?after_seq=0",
        headers={"Last-Event-ID": "2"},
    )
    [event] = _envelopes(response.text)
    assert event.after_seq == 2
    assert event.cursor == 3
    assert [item.seq for item in event.snapshot.evidence] == [3]

    for response in (
        altar_client.get(f"/api/v1/orb/runs/run-orb-reconnect/events?after_seq={future_cursor}"),
        altar_client.get("/api/v1/orb/runs/run-orb-reconnect/events", headers={"Last-Event-ID": str(future_cursor)}),
    ):
        [reset] = _envelopes(response.text)
        assert reset.kind == "reset"
        assert reset.after_seq == future_cursor
        assert reset.cursor == 3
        assert reset.snapshot.evidence == []


@pytest.mark.parametrize("raw_cursor", ["not-a-sequence", "-2", str(1 << 31), str(10**100)])
def test_orb_stream_rejects_invalid_cursor(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
    raw_cursor: str,
) -> None:
    _seed(fake_services, "run-orb-invalid")
    response = altar_client.get("/api/v1/orb/runs/run-orb-invalid/events", headers={"Last-Event-ID": raw_cursor})
    assert response.status_code == 400


@pytest.mark.parametrize("suffix", ["", "/events"])
@pytest.mark.parametrize("cursor", [1 << 31, 10**100])
def test_orb_snapshot_and_stream_reject_cursor_overflow(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
    suffix: str,
    cursor: int,
) -> None:
    _seed(fake_services, "run-orb-overflow")

    response = altar_client.get(f"/api/v1/orb/runs/run-orb-overflow{suffix}", params={"after_seq": cursor})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")


def test_orb_stream_requires_authorization_and_an_existing_selected_run(
    altar_client: TestClient[Litestar],
) -> None:
    assert altar_client.get("/api/v1/orb/runs/missing/events").status_code == 404
    assert altar_client.get("/api/v1/orb/runs/missing/events", headers={"authorization": ""}).status_code == 401
    assert altar_client.get("/api/v1/orb/runs/missing/events?after_seq=-2").status_code == 400


def test_orb_live_feed_reads_retained_tee_and_metadata_without_tokens(
    fake_services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(fake_services, "run-orb-live")
    _seed(fake_services, "another-run")
    monkeypatch.setattr("lychd.interface.web.orb._POLL_INTERVAL_S", 0)

    async def observe() -> None:
        stream = _orb_events(
            fake_services.ledger,
            "run-orb-live",
            fake_services.workflows,
            after_seq=3,
            delegates=fake_services.delegates,
        )
        initial = OrbEventEnvelope.model_validate_json(str((await anext(stream)).data))
        assert initial.cursor == 3
        assert initial.snapshot.evidence == []
        fake_services.bus.open("run-orb-live", from_seq=4)
        emitter = fake_services.bus.emitter("run-orb-live")
        emitter.token("private live token")
        emitted = emitter.node("converse", phase="exited", occurrence_id="occurrence-live")
        await fake_services.bus.wait_persisted("run-orb-live")

        update = OrbEventEnvelope.model_validate_json(str((await anext(stream)).data))
        assert update.after_seq == 3
        assert update.cursor == 5
        assert [item.event_id for item in update.snapshot.evidence] == [emitted.event_id]
        assert update.snapshot.evidence[0].occurrence_id == "occurrence-live"
        assert [(gap.start_seq, gap.end_seq) for gap in update.snapshot.gaps] == [(4, 4)]
        assert "private live token" not in update.model_dump_json()

        await fake_services.delegates.submit(
            DelegatedAgentRequest(
                request_id="live-request",
                run_id="run-orb-live",
                step_id="dispatch_delegate",
                runtime="reference",
                profile=DelegatedAgentProfile.READ,
                prompt="private live job prompt",
            )
        )
        metadata = OrbEventEnvelope.model_validate_json(str((await anext(stream)).data))
        assert metadata.after_seq == metadata.cursor == 5
        assert metadata.snapshot.evidence == []
        assert len(metadata.snapshot.delegated_jobs) == 1
        assert "private live job prompt" not in metadata.model_dump_json()

        await fake_services.ledger.set_status("run-orb-live", RunStatus.DONE)
        settled = OrbEventEnvelope.model_validate_json(str((await anext(stream)).data))
        assert settled.kind == "settled"
        assert settled.cursor == 5
        assert settled.snapshot.run.status == "done"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(observe())


def test_orb_stream_shares_http_budget_and_releases_after_settlement(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
) -> None:
    _seed(fake_services, "run-orb-budget")
    asyncio.run(fake_services.ledger.set_status("run-orb-budget", RunStatus.DONE))
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(fake_services.bus.reserve_http_stream("run-orb-budget"))
        response = altar_client.get("/api/v1/orb/runs/run-orb-budget/events")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "5"
        assert response.headers["content-type"].startswith("application/json")
    assert altar_client.get("/api/v1/orb/runs/run-orb-budget/events").status_code == 200
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(fake_services.bus.reserve_http_stream("run-orb-budget"))


def test_orb_disconnect_cancels_polling_and_releases_capacity(fake_services: SimpleNamespace) -> None:
    _seed(fake_services, "run-orb-disconnect")

    async def disconnect() -> None:
        stream = _orb_events(fake_services.ledger, "run-orb-disconnect", fake_services.workflows, after_seq=3)
        request = RequestFactory().get("/api/v1/orb/runs/run-orb-disconnect/events")
        response = BoundedRunEventStream(stream, bus=fake_services.bus, run_id="run-orb-disconnect").to_asgi_response(
            None, request
        )
        sent = asyncio.Event()

        async def send(message: Message) -> None:
            if message["type"] == "http.response.body":
                sent.set()

        async def receive() -> HTTPDisconnectEvent:
            await sent.wait()
            return {"type": "http.disconnect"}

        await asyncio.wait_for(response(request.scope, receive, send), timeout=1)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        with ExitStack() as stack:
            for _ in range(4):
                stack.enter_context(fake_services.bus.reserve_http_stream("run-orb-disconnect"))

    asyncio.run(disconnect())
