import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OrbRunSnapshot } from "$lib/api/models";

vi.mock("$app/navigation", () => ({ goto: vi.fn() }));
vi.mock("$app/state", () => ({
  page: { url: new URL("http://localhost/orb/run-a") }
}));
vi.mock("$lib/api/client", () => ({
  getAtlasReferences: vi.fn().mockResolvedValue([]),
  getOrbRun: vi.fn()
}));

import { page } from "$app/state";
import { getOrbRun } from "$lib/api/client";
import OrbView from "./OrbView.svelte";
import { listenToOrb, type OrbLiveCallbacks } from "$lib/orb/live";
vi.mock("$lib/orb/live", () => ({ listenToOrb: vi.fn(() => vi.fn()) }));

const snapshot: OrbRunSnapshot = {
  schema_version: 1,
  workflow: null,
  snapshot_at: "2026-07-30T00:00:00Z",
  capture: "process_local",
  ledger_head_seq: 0,
  page_end_seq: 0,
  live_tail: "ledger_poll",
  next_after_seq: null,
  has_more: false,
  known_omissions: [],
  delegated_jobs: [],
  gaps: [],
  evidence: [
    {
      event_id: "event-a",
      seq: 0,
      kind: "node",
      occurred_at: "2026-07-30T00:00:00Z",
      summary: "Step completed",
      capture: "process_local",
      nexus_path: null,
      occurrence_id: "occurrence-a",
      phase: "settled",
      subject_key: "step-a",
      transition_request_id: null
    }
  ],
  pattern: {
    pattern_id: "bridge_chat",
    revision: "1",
    digest: "digest-a",
    exact: true,
    loom_path: "/loom/bridge_chat/1"
  },
  run: {
    run_id: "run-a",
    session_id: "session-a",
    workflow_name: "bridge_chat",
    status: "done",
    created_at: "2026-07-30T00:00:00Z",
    started_at: "2026-07-30T00:00:00Z",
    finished_at: "2026-07-30T00:00:00Z",
    error_present: false,
    bridge_path: "/bridge/session-a"
  }
};

function deferred<T>() {
  let reject!: (reason?: unknown) => void;
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept, decline) => {
    reject = decline;
    resolve = accept;
  });
  return { promise, reject, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("EventSource", vi.fn());
  page.url.href = "http://localhost/orb/run-a";
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
  vi.mocked(getOrbRun).mockResolvedValue(snapshot);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Orb evidence focus", () => {
  it("discloses a linked job without hiding the Run coverage and ordered evidence", async () => {
    page.url.href = "http://localhost/orb/run-a?job=job-linked";
    const linked: OrbRunSnapshot = {
      ...snapshot,
      known_omissions: ["Older jobs are outside this bounded snapshot."],
      delegated_jobs: [{
        job_id: "job-linked", request_id: "request-linked", runtime: "reference", profile: "read", step_id: "step-a",
        status: "completed", output_present: false, artifact_count: 0, error_present: false,
        events: [], events_truncated: false
      }]
    };
    vi.mocked(getOrbRun).mockResolvedValue(linked);
    const view = render(OrbView, { runId: "run-a" });
    const coverage = await screen.findByRole("region", { name: "Evidence coverage" });
    const limits = screen.getByRole("region", { name: "Evidence limits" });
    const evidence = screen.getByRole("region", { name: "Ordered evidence" });
    const disclosure = view.container.querySelector<HTMLDetailsElement>("details.delegated-jobs")!;
    const job = view.container.querySelector("#delegated-job-job-linked");

    expect(coverage.compareDocumentPosition(limits) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(limits.compareDocumentPosition(evidence) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(evidence.compareDocumentPosition(disclosure) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(disclosure.open).toBe(true);
    await waitFor(() => expect(document.activeElement).toBe(job));
    expect(screen.getByText(/Job lifecycle sequences belong to each AgentJob/)).toBeTruthy();
    const reader = screen.getByRole("button", { name: /Step completed/ });
    reader.focus();
    const callbacks = vi.mocked(listenToOrb).mock.calls[0]![2];
    await act(() => callbacks.onFrame({ schema_version: 1, run_id: "run-a", kind: "evidence", after_seq: 0, cursor: 0,
      snapshot: { ...linked, evidence: [] } }));
    expect(document.activeElement).toBe(reader);
    await fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(getOrbRun).toHaveBeenCalledTimes(2));
    expect(document.activeElement).toBe(reader);
    vi.mocked(getOrbRun).mockResolvedValue({ ...linked, run: { ...linked.run, run_id: "run-b" } });
    await view.rerender({ runId: "run-b" });
    await waitFor(() => expect(document.activeElement).toBe(view.container.querySelector("#delegated-job-job-linked")));
    view.unmount();
  });

  it("keeps unselected job detail closed so its volume cannot precede the evidence", async () => {
    vi.mocked(getOrbRun).mockResolvedValue({
      ...snapshot,
      delegated_jobs: [{
        job_id: "job-visible", request_id: "request-visible", runtime: "reference", profile: "read", step_id: "step-a",
        status: "completed", output_present: false, artifact_count: 0, error_present: false,
        events: [], events_truncated: false
      }]
    });
    const view = render(OrbView, { runId: "run-a" });
    await screen.findByRole("region", { name: "Ordered evidence" });
    expect(view.container.querySelector<HTMLDetailsElement>("details.delegated-jobs")?.open).toBe(false);
    view.unmount();
  });

  it("reports a linked delegated job absent from the selected Run snapshot", async () => {
    page.url.href = "http://localhost/orb/run-a?job=job-unavailable";
    vi.mocked(getOrbRun).mockResolvedValue({
      ...snapshot,
      delegated_jobs: [{
        job_id: "job-visible", request_id: "request-visible", runtime: "reference", profile: "read", step_id: "step-a",
        status: "completed", output_present: false, artifact_count: 0, error_present: false,
        events: [], events_truncated: false
      }]
    });
    const view = render(OrbView, { runId: "run-a" });
    expect(await screen.findByText(/The linked delegated job is not present in this Run's bounded snapshot/)).toBeTruthy();
    expect(view.container.querySelector(".delegated-job[aria-current='true']")).toBeNull();

    vi.mocked(getOrbRun).mockResolvedValue({
      ...snapshot,
      delegated_jobs: [{
        job_id: "job-unavailable", request_id: "request-selected", runtime: "reference", profile: "read", step_id: "step-a",
        status: "completed", output_present: false, artifact_count: 0, error_present: false,
        events: [], events_truncated: false
      }]
    });
    await fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(view.container.querySelector(".delegated-job[aria-current='true']")?.id)
      .toBe("delegated-job-job-unavailable"));
    expect(screen.queryByText(/The linked delegated job is not present/)).toBeNull();
    view.unmount();
  });

  it("aborts a pending Orb read when its view is destroyed", async () => {
    const pending = deferred<OrbRunSnapshot>();
    let signal: AbortSignal | undefined;
    vi.mocked(getOrbRun).mockImplementationOnce((_runId, options) => {
      signal = options?.signal;
      return pending.promise;
    });
    const view = render(OrbView, { runId: "run-a" });
    await act(async () => {
      await Promise.resolve();
    });

    expect(signal?.aborted).toBe(false);
    view.unmount();

    expect(signal?.aborted).toBe(true);
    await act(() => pending.reject(new DOMException("Aborted", "AbortError")));
  });

  it("aborts a pending Orb read when the selected Run is cleared", async () => {
    const pending = deferred<OrbRunSnapshot>();
    let signal: AbortSignal | undefined;
    vi.mocked(getOrbRun).mockImplementationOnce((_runId, options) => {
      signal = options?.signal;
      return pending.promise;
    });
    const view = render(OrbView, { runId: "run-a" });
    await act(async () => {
      await Promise.resolve();
    });

    await view.rerender({ runId: undefined });

    expect(signal?.aborted).toBe(true);
    expect(screen.getByRole("heading", { name: "Orb" })).toBeTruthy();
    await act(() => pending.reject(new DOMException("Aborted", "AbortError")));
    view.unmount();
  });

  it("returns focus to the evidence row after its inspector closes", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false })));
    const view = render(OrbView, { runId: "run-a" });
    const opener = await screen.findByRole("button", { name: /Step completed/ });

    await fireEvent.click(opener);
    const close = await screen.findByRole("button", { name: "Close" });
    expect(document.activeElement).toBe(screen.getByRole("region", { name: "Selected event" }));
    await fireEvent.click(close);

    expect(document.activeElement).toBe(opener);
    view.unmount();
  });

  it("clears selected evidence when navigation changes Run identity", async () => {
    const nextRun = deferred<OrbRunSnapshot>();
    vi.mocked(getOrbRun)
      .mockResolvedValueOnce(snapshot)
      .mockReturnValueOnce(nextRun.promise);
    const view = render(OrbView, { runId: "run-a" });
    const opener = await screen.findByRole("button", { name: /Step completed/ });

    await fireEvent.click(opener);
    expect(screen.getByText("Selected event")).toBeTruthy();

    await view.rerender({ runId: "run-b" });
    expect(screen.queryByText("Selected event")).toBeNull();
    expect(screen.queryByRole("button", { name: /Step completed/ })).toBeNull();

    await act(() => nextRun.reject(new Error("Run B unavailable")));
    expect(screen.getByRole("alert").textContent).toContain("Run B unavailable");
    expect(screen.queryByText("Selected event")).toBeNull();
    view.unmount();
  });

  it("retains same-Run evidence when refresh fails", async () => {
    vi.mocked(getOrbRun)
      .mockResolvedValueOnce(snapshot)
      .mockRejectedValueOnce(new Error("Refresh unavailable"));
    const view = render(OrbView, { runId: "run-a" });

    expect(await screen.findByRole("button", { name: /Step completed/ })).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect((await screen.findByText("Refresh unavailable")).getAttribute("role")).toBe("alert");
    expect(screen.getByRole("button", { name: /Step completed/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeTruthy();
    view.unmount();
  });
});

describe("Orb evidence pagination", () => {
  it("keeps a linked event outside the loaded page distinct from a gap and selects it after loading", async () => {
    page.url.href = "http://localhost/orb/run-a?event=event-b";
    vi.mocked(getOrbRun)
      .mockResolvedValueOnce({
        ...snapshot,
        ledger_head_seq: 4,
        page_end_seq: 2,
        next_after_seq: 2,
        has_more: true,
        gaps: [{ start_seq: 1, end_seq: 2, classification: "unknown_or_omitted" }]
      })
      .mockResolvedValueOnce({
        ...snapshot,
        ledger_head_seq: 4,
        page_end_seq: 4,
        evidence: [{ ...snapshot.evidence[0]!, event_id: "event-b", seq: 4, summary: "Linked event" }]
      });
    const view = render(OrbView, { runId: "run-a" });
    expect(await screen.findByText(/outside this loaded page/)).toBeTruthy();
    expect(screen.getByText("Unknown or omitted interval")).toBeTruthy();
    expect(screen.getByText(/Sequence is order, not elapsed time or causality/)).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: "Load more retained evidence" }));

    const linkedRow = await screen.findByRole("button", { name: /Linked event/ });
    expect(linkedRow.getAttribute("aria-current")).toBe("true");
    expect(screen.getByRole("region", { name: "Selected event" })).toBeTruthy();
    expect(screen.queryByText(/outside this loaded page/)).toBeNull();
    expect(screen.getByText("Unknown or omitted interval")).toBeTruthy();
    view.unmount();
  });

  it("releases pagination ownership when navigation changes Run identity", async () => {
    const firstPage: OrbRunSnapshot = {
      ...snapshot,
      ledger_head_seq: 1,
      next_after_seq: 0,
      has_more: true
    };
    const stalePage = deferred<OrbRunSnapshot>();
    const runB: OrbRunSnapshot = {
      ...firstPage,
      run: { ...firstPage.run, run_id: "run-b" }
    };
    vi.mocked(getOrbRun)
      .mockResolvedValueOnce(firstPage)
      .mockReturnValueOnce(stalePage.promise)
      .mockResolvedValueOnce(runB);
    const view = render(OrbView, { runId: "run-a" });

    await fireEvent.click(
      await screen.findByRole("button", { name: "Load more retained evidence" })
    );
    expect(screen.getByRole("button", { name: "Reading…" })).toBeTruthy();

    await view.rerender({ runId: "run-b" });

    const runBLoadMore = await screen.findByRole("button", {
      name: "Load more retained evidence"
    });
    expect((runBLoadMore as HTMLButtonElement).disabled).toBe(false);
    await act(() => stalePage.resolve(snapshot));
    expect((runBLoadMore as HTMLButtonElement).disabled).toBe(false);
    view.unmount();
  });

  it("retains loaded evidence and offers a local retry when load more fails", async () => {
    const firstPage: OrbRunSnapshot = {
      ...snapshot,
      ledger_head_seq: 1,
      next_after_seq: 0,
      has_more: true
    };
    const nextPage: OrbRunSnapshot = {
      ...snapshot,
      ledger_head_seq: 1,
      page_end_seq: 1,
      evidence: [
        {
          ...snapshot.evidence[0]!,
          event_id: "event-b",
          seq: 1,
          summary: "Second step completed",
          subject_key: "step-b"
        }
      ]
    };
    vi.mocked(getOrbRun)
      .mockResolvedValueOnce(firstPage)
      .mockRejectedValueOnce(new Error("Next page unavailable"))
      .mockResolvedValueOnce(nextPage);
    const view = render(OrbView, { runId: "run-a" });

    expect(await screen.findByRole("button", { name: /Step completed/ })).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Load more retained evidence" }));

    expect((await screen.findByText("Next page unavailable")).getAttribute("role")).toBe("alert");
    expect(screen.getByRole("button", { name: /Step completed/ })).toBeTruthy();

    await fireEvent.click(
      screen.getByRole("button", { name: "Retry loading retained evidence" })
    );

    expect(await screen.findByRole("button", { name: /Second step completed/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Step completed/ })).toBeTruthy();
    expect(screen.queryByText("Next page unavailable")).toBeNull();
    expect(getOrbRun).toHaveBeenNthCalledWith(
      2,
      "run-a",
      expect.objectContaining({ afterSeq: 0, limit: 100 })
    );
    expect(getOrbRun).toHaveBeenNthCalledWith(
      3,
      "run-a",
      expect.objectContaining({ afterSeq: 0, limit: 100 })
    );
    view.unmount();
  });

  it("refuses to merge a page returned for another Run", async () => {
    const firstPage: OrbRunSnapshot = {
      ...snapshot,
      ledger_head_seq: 1,
      next_after_seq: 0,
      has_more: true
    };
    const wrongPage: OrbRunSnapshot = {
      ...snapshot,
      run: { ...snapshot.run, run_id: "run-b" },
      evidence: [
        {
          ...snapshot.evidence[0]!,
          event_id: "event-b",
          seq: 1,
          summary: "Foreign step",
          subject_key: "step-b"
        }
      ]
    };
    vi.mocked(getOrbRun).mockResolvedValueOnce(firstPage).mockResolvedValueOnce(wrongPage);
    const view = render(OrbView, { runId: "run-a" });

    expect(await screen.findByRole("button", { name: /Step completed/ })).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Load more retained evidence" }));

    expect((await screen.findByText("The Orb returned evidence for another Run.")).getAttribute("role")).toBe("alert");
    expect(screen.queryByRole("button", { name: /Foreign step/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Step completed/ })).toBeTruthy();
    view.unmount();
  });
});


it("hands exact Run and event context to Bridge and the pinned Pattern", async () => {
  page.url.href = "http://localhost/orb/run-a?event=event-a&job=job-a";
  const view = render(OrbView, { runId: "run-a" });
  expect((await screen.findByRole("link", { name: "Bridge" })).getAttribute("href"))
    .toBe("/bridge/session-a?run=run-a");
  expect(screen.getByRole("link", { name: "Exact Pattern →" }).getAttribute("href"))
    .toBe("/loom/bridge_chat/1?run=run-a&event=event-a&job=job-a");
  view.unmount();
});


describe("Orb selected-Run live ownership", () => {
  it("freezes historical Animator evidence across live-window eviction and resumes on Follow latest", async () => {
    const view = render(OrbView, { runId: "run-a" });
    await waitFor(() => expect(listenToOrb).toHaveBeenCalledOnce());
    const callbacks = vi.mocked(listenToOrb).mock.calls[0]![2];
    const grant = { ...snapshot.evidence[0]!, event_id: "grant-live", seq: 1, kind: "dispatch", summary: "Grant selected", animator_name: "historical-reasoner", model_id: "model-at-one" };
    await act(() => callbacks.onFrame({ schema_version: 1, run_id: "run-a", kind: "evidence", after_seq: 0, cursor: 1,
      snapshot: { ...snapshot, ledger_head_seq: 1, evidence: [grant] } }));
    const rail = screen.getByRole("complementary", { name: "Recorded Animators" });
    await fireEvent.click(within(rail).getByRole("button", { name: /historical-reasoner/ }));
    const slider = screen.getByRole("slider", { name: "Record cursor" });
    expect(slider.getAttribute("aria-valuetext")).toBe("Record #1");
    expect(screen.getByRole("button", { name: "LIVE" }).getAttribute("aria-pressed")).toBe("false");
    await act(() => {
      for (let seq = 2; seq <= 103; seq++) callbacks.onFrame({ schema_version: 1, run_id: "run-a", kind: "evidence", after_seq: seq - 1, cursor: seq,
        snapshot: { ...snapshot, ledger_head_seq: seq, evidence: [{ ...grant, event_id: `future-${seq}`, seq, animator_name: "future-animator" }] } });
    });
    expect(slider.getAttribute("aria-valuetext")).toBe("Record #1");
    expect(within(rail).getByText("historical-reasoner")).toBeTruthy();
    expect(within(rail).queryByText("future-animator")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Follow latest" }));
    expect(screen.getByRole("button", { name: "LIVE" }).getAttribute("aria-pressed")).toBe("true");
    expect(slider.getAttribute("aria-valuetext")).toBe("Record #103");
    expect(within(rail).queryByText("historical-reasoner")).toBeNull();
    expect(within(rail).getByText("future-animator")).toBeTruthy();
    view.unmount();
  });

  it("closes the prior feed and fences callbacks after a Run change and unmount", async () => {
    const closeA = vi.fn(), closeB = vi.fn();
    vi.mocked(listenToOrb).mockReturnValueOnce(closeA).mockReturnValueOnce(closeB);
    const view = render(OrbView, { runId: "run-a" });
    await waitFor(() => expect(listenToOrb).toHaveBeenCalledOnce());
    const callbacks = vi.mocked(listenToOrb).mock.calls[0]![2] as OrbLiveCallbacks;
    vi.mocked(getOrbRun).mockResolvedValue({ ...snapshot, run: { ...snapshot.run, run_id: "run-b" } });
    await view.rerender({ runId: "run-b" });
    await waitFor(() => expect(listenToOrb).toHaveBeenCalledTimes(2));
    expect(closeA).toHaveBeenCalledOnce();
    await act(() => callbacks.onFrame({ schema_version: 1, run_id: "run-a", kind: "evidence", after_seq: 0, cursor: 1,
      snapshot: { ...snapshot, ledger_head_seq: 1, evidence: [{ ...snapshot.evidence[0]!, event_id: "late-a", seq: 1, summary: "Late foreign callback" }] } }));
    expect(screen.queryByText("Late foreign callback")).toBeNull();
    expect(screen.getByText("run-b")).toBeTruthy();
    view.unmount();
    expect(closeB).toHaveBeenCalledOnce();
  });

  it("appends real records while preserving selected evidence and paused follow", async () => {
    page.url.href = "http://localhost/orb/run-a?event=event-a";
    const view = render(OrbView, { runId: "run-a" });
    const opener = await screen.findByRole("button", { name: /Step completed/ });
    await fireEvent.click(opener);
    const callbacks = vi.mocked(listenToOrb).mock.calls[0]![2];
    await act(() => callbacks.onFrame({ schema_version: 1, run_id: "run-a", kind: "evidence", after_seq: 0, cursor: 1,
      snapshot: { ...snapshot, ledger_head_seq: 1, evidence: [{ ...snapshot.evidence[0]!, event_id: "event-live", seq: 1, summary: "Fresh ledger record" }] } }));
    expect(screen.getByRole("button", { name: /Fresh ledger record/ })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Selected event" }).textContent).toContain("event-a");
    expect(screen.getByRole("button", { name: "Follow latest" }).getAttribute("aria-pressed")).toBe("false");
    view.unmount();
  });
});
