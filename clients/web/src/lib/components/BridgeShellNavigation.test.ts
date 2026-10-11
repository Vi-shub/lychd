import { act, fireEvent, render, screen, within } from "@testing-library/svelte";
import { createRawSnippet } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BridgeSnapshot, OrbRunSnapshot } from "$lib/api/models";

vi.mock("$app/state", async () => {
  const { SvelteURL } = await import("svelte/reactivity");
  return { page: { state: {}, url: new SvelteURL("http://localhost/bridge") } };
});
vi.mock("$app/navigation", () => ({ goto: vi.fn(), replaceState: vi.fn() }));
vi.mock("$lib/api/client", () => ({
  ApiError: class ApiError extends Error {},
  cancelBridgeRun: vi.fn(),
  createBridgeSession: vi.fn(),
  decideConsent: vi.fn(),
  getAltarStatus: vi.fn(),
  getAtlasProject: vi.fn(),
  getAtlasReferences: vi.fn().mockResolvedValue([]),
  getBridgeSnapshot: vi.fn(),
  getBridgeSessions: vi.fn(),
  getOrbRun: vi.fn(),
  getRunSnapshot: vi.fn(),
  listenToRun: vi.fn(),
  sendBridgeMessage: vi.fn()
}));

import { page } from "$app/state";
import { createBridgeSession, getAltarStatus, getBridgeSnapshot, getOrbRun, sendBridgeMessage } from "$lib/api/client";
import { bridgeWorkspaceKey, createBridgeWorkspace } from "$lib/bridge/workspace.svelte";
import AltarShell from "./AltarShell.svelte";
import BridgeView from "./BridgeView.svelte";
import OrbView from "./OrbView.svelte";

const children = createRawSnippet(() => ({ render: () => "<div></div>" }));
const sessions = [
  { id: "session-a", title: "Newest A", created_at: "2026-10-10T12:00:00Z", pending_count: 0 },
  { id: "session-c", title: "Later selected C", created_at: "2026-10-09T12:00:00Z", pending_count: 0 },
  { id: "session-b", title: "Older B", created_at: "2026-10-08T12:00:00Z", pending_count: 0 }
];

function snapshot(sessionId = "session-a"): BridgeSnapshot {
  const session = sessions.find(({ id }) => id === sessionId);
  if (!session) throw new Error(`Unexpected session read: ${sessionId}`);
  return {
    sessions,
    session: { ...session, turns: [{
      role: "agent", content: `Retained answer for ${sessionId}`, run_id: `run-${sessionId.at(-1)}`,
      state: "done", fragments: [], created_at: session.created_at
    }] },
    active_runs: [], pending_consents: [], pending_count: 0
  };
}

const orb: OrbRunSnapshot = {
  schema_version: 1, snapshot_at: "2026-10-10T12:00:00Z", capture: "process_local",
  ledger_head_seq: 0, page_end_seq: 0, live_tail: "ledger_poll", next_after_seq: null,
  has_more: false, known_omissions: [], delegated_jobs: [], gaps: [],
  evidence: [{
    event_id: "event-b", seq: 0, kind: "node", occurred_at: "2026-10-08T12:00:00Z",
    summary: "B completed", capture: "process_local", nexus_path: null,
    occurrence_id: "occurrence-b", phase: "settled", subject_key: "step-b", transition_request_id: null
  }],
  pattern: { pattern_id: "bridge_chat", revision: "1", digest: "digest-b", exact: true, loom_path: "/loom/bridge_chat/1" },
  run: {
    run_id: "run-b", session_id: "session-b", workflow_name: "bridge_chat", status: "done",
    created_at: "2026-10-08T12:00:00Z", started_at: "2026-10-08T12:00:00Z",
    finished_at: "2026-10-08T12:00:00Z", error_present: false, bridge_path: "/bridge/session-b"
  }
};

async function visit(href: string) {
  await act(() => { page.url.href = new URL(href, "http://localhost").href; });
}

function shellHref(name: "Bridge" | "Orb") {
  return within(screen.getByRole("navigation", { name: "Instruments" }))
    .getByRole("link", { name }).getAttribute("href")!;
}

function mountBridge(work: ReturnType<typeof createBridgeWorkspace>) {
  // Model the two existing Bridge page wrappers; the href comes from the rendered shell.
  const sessionId = page.url.pathname === "/bridge" ? undefined : decodeURIComponent(page.url.pathname.slice("/bridge/".length));
  return render(BridgeView, {
    target: screen.getByRole("main"), props: { sessionId },
    context: new Map([[bridgeWorkspaceKey, work]])
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  page.url.href = "http://localhost/bridge";
  vi.mocked(getAltarStatus).mockResolvedValue({
    csrf: { cookie_name: "csrf", header_name: "X-CSRF-Token" }, pending_consents: 1
  });
  vi.mocked(getBridgeSnapshot).mockImplementation(async (sessionId) => snapshot(sessionId));
  vi.mocked(getOrbRun).mockResolvedValue(orb);
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
});

afterEach(() => { vi.unstubAllGlobals(); });

describe("Bridge selection across instrument navigation", () => {
  it("returns through the shell to older B and restores its draft instead of reading newest A", async () => {
    await visit("/bridge/session-b");
    const shell = render(AltarShell, { children });
    const work = createBridgeWorkspace();
    const first = mountBridge(work);
    const composer = await screen.findByLabelText("Message");
    await fireEvent.input(composer, { target: { value: "Unfinished B offering" } });
    const orbHref = screen.getByRole("link", { name: "Look into the Orb" }).getAttribute("href")!;
    first.unmount();
    await visit(orbHref);
    const evidence = render(OrbView, { target: screen.getByRole("main"), props: { runId: "run-b" } });
    await screen.findByText("B completed");
    const returnHref = shellHref("Bridge");
    evidence.unmount();
    await visit(returnHref);
    const returned = mountBridge(work);
    const returnedComposer = await screen.findByLabelText("Message");

    expect.soft(vi.mocked(getBridgeSnapshot).mock.calls.map(([id]) => id)).toEqual(["session-b", "session-b"]);
    expect.soft((returnedComposer as HTMLTextAreaElement).value).toBe("Unfinished B offering");
    expect.soft(screen.queryByText("Retained answer for session-a")).toBeNull();
    expect.soft(screen.queryByText("Retained answer for session-b")).toBeTruthy();
    expect(work.drafts.get("session-b")).toBe("Unfinished B offering");
    expect(sendBridgeMessage).not.toHaveBeenCalled();
    expect(createBridgeSession).not.toHaveBeenCalled();
    returned.unmount();
    shell.unmount();
  });

  it("keeps a fresh shell at the root landing without inventing a selected session", async () => {
    const shell = render(AltarShell, { children });
    await act(() => Promise.resolve());
    expect(shellHref("Bridge")).toBe("/bridge");
    const work = createBridgeWorkspace();
    const landing = mountBridge(work);
    await screen.findByText("Retained answer for session-a");
    expect(getBridgeSnapshot).toHaveBeenCalledWith(undefined);
    expect(shellHref("Bridge")).toBe("/bridge");
    landing.unmount();
    shell.unmount();
  });

  it("replaces B with a later explicit C selection and forgets it with the shell lifetime", async () => {
    await visit("/bridge/session-b");
    const shell = render(AltarShell, { children });
    await visit("/orb/run-b");
    await visit("/bridge/session%2Dc");
    await visit("/nexus");
    expect.soft(shellHref("Bridge")).toBe("/bridge/session-c");
    shell.unmount();
    const fresh = render(AltarShell, { children });
    await act(() => Promise.resolve());
    expect(shellHref("Bridge")).toBe("/bridge");
    fresh.unmount();
  });

  it("does not let unrelated, bare, or malformed paths overwrite the explicit Bridge selection", async () => {
    await visit("/bridge/session-b");
    const shell = render(AltarShell, { children });
    for (const path of [
      "/atlas/session-c", "/bridges/session-c", "/bridge", "/bridge/", "/bridge?attention=pending",
      "/bridge/session-c/extra", "/bridge/%2Fsession-c", "/bridge/%E0%A4%A", "/bridge/%20session-c",
      `/bridge/${"x".repeat(129)}`
    ]) {
      await visit(path);
      expect.soft(shellHref("Bridge"), path).toBe("/bridge/session-b");
    }
    shell.unmount();
  });

  it("keeps the explicit attention link as a chooser after B has been selected", async () => {
    await visit("/bridge/session-b");
    const shell = render(AltarShell, { children });
    const attention = await screen.findByRole("link", { name: /1 awaiting/ });
    expect(attention.getAttribute("href")).toBe("/bridge?attention=pending");
    await visit(attention.getAttribute("href")!);
    const chooser = mountBridge(createBridgeWorkspace());
    await screen.findByRole("heading", { name: "Conversations awaiting consent" });
    expect(screen.queryByLabelText("Message")).toBeNull();
    expect(sendBridgeMessage).not.toHaveBeenCalled();
    expect(createBridgeSession).not.toHaveBeenCalled();
    chooser.unmount();
    shell.unmount();
  });

  it("preserves the contextual Orb return Run while the global tab opens the parallel field", async () => {
    await visit("/orb/run-b?event=event-b");
    const shell = render(AltarShell, { children });
    const evidence = render(OrbView, { target: screen.getByRole("main"), props: { runId: "run-b" } });
    const contextual = await within(screen.getByRole("main")).findByRole("link", { name: "Bridge" });
    expect(contextual.getAttribute("href")).toBe("/bridge/session-b?run=run-b");
    await visit("/orb/run-b?event=event-later&job=job-b");
    evidence.unmount();
    await visit(contextual.getAttribute("href")!);
    expect(shellHref("Orb")).toBe("/orb");
    shell.unmount();
  });
});
