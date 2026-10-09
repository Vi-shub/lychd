import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OrbEventEnvelope } from "$lib/api/models";
import { fixtureRun } from "./__fixtures__/snapshot";
import { getOrbCatalogue, listenToOrb, type OrbLiveCallbacks } from "./live";

class Source extends EventTarget {
  static CLOSED = 2;
  static instances: Source[] = [];
  readyState = 1;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn(() => { this.readyState = Source.CLOSED; });
  constructor(readonly url: string) { super(); Source.instances.push(this); }
  emit(kind: string, frame: unknown, id?: string) { this.dispatchEvent(new MessageEvent(kind, { data: JSON.stringify(frame), lastEventId: id ?? String((frame as OrbEventEnvelope).cursor) })); }
}
const callbacks = (): OrbLiveCallbacks => ({ onFrame: vi.fn(), onConnection: vi.fn(), onReset: vi.fn(), onFault: vi.fn() });
function frame(changes: Partial<OrbEventEnvelope> = {}): OrbEventEnvelope {
  const snapshot = fixtureRun();
  snapshot.evidence = [{ ...snapshot.evidence[0]!, seq: 1, event_id: "live-1" }];
  snapshot.ledger_head_seq = 1;
  return { schema_version: 1, run_id: "run-a", kind: "evidence", after_seq: 0, cursor: 1, snapshot, ...changes };
}
beforeEach(() => { Source.instances = []; vi.stubGlobal("EventSource", Source); });
afterEach(() => vi.unstubAllGlobals());

describe("Orb retained-ledger feed", () => {
  it("uses the selected Run and retained head; ignores replay, accepts metadata and closes only on settled", () => {
    const cb = callbacks(); listenToOrb("run-a", 0, cb);
    const source = Source.instances[0]!;
    expect(source.url).toBe("/api/v1/orb/runs/run-a/events?after_seq=0");
    source.onopen?.(); expect(cb.onConnection).toHaveBeenLastCalledWith("connected");
    const event = frame(); event.snapshot.run.status = "done";
    source.emit("evidence", event);
    expect(source.close).not.toHaveBeenCalled();
    source.emit("evidence", event);
    expect(cb.onFrame).toHaveBeenCalledOnce();
    source.emit("settled", frame({ kind: "settled", after_seq: 1, cursor: 1, snapshot: { ...event.snapshot, evidence: [] } }));
    expect(cb.onFrame).toHaveBeenCalledTimes(2);
    expect(source.close).toHaveBeenCalledOnce();
    expect(cb.onConnection).toHaveBeenLastCalledWith("settled");
  });
  it.each(["foreign", "cursor", "range", "schema", "oversize"])("closes on invalid %s without publishing data", (failure) => {
    const cb = callbacks(); listenToOrb("run-a", 0, cb);
    const value = frame();
    if (failure === "foreign") value.snapshot.run.run_id = "other";
    if (failure === "cursor") value.after_seq = 2;
    if (failure === "range") value.snapshot.evidence[0]!.seq = 5;
    if (failure === "schema") Object.assign(value, { schema_version: 2 });
    if (failure === "oversize") value.snapshot.evidence = Array.from({ length: 101 }, (_, i) => ({ ...value.snapshot.evidence[0]!, event_id: String(i), seq: i + 1 }));
    Source.instances[0]!.emit("evidence", value);
    expect(cb.onFrame).not.toHaveBeenCalled();
    expect(cb.onFault).toHaveBeenCalledOnce();
    expect(Source.instances[0]!.close).toHaveBeenCalledOnce();
  });
  it("treats a reset before monotonic-cursor validation and leaves replacement to its owner", () => {
    const cb = callbacks(); listenToOrb("run-a", 50, cb);
    Source.instances[0]!.emit("reset", frame({ kind: "reset", after_seq: 50, cursor: 0, snapshot: fixtureRun() }));
    expect(cb.onReset).toHaveBeenCalledOnce();
    expect(cb.onFault).not.toHaveBeenCalled();
    expect(cb.onFrame).not.toHaveBeenCalled();
    expect(Source.instances[0]!.close).toHaveBeenCalledOnce();
  });
  it("fences callbacks after teardown and distinguishes reconnecting from settlement", () => {
    const cb = callbacks(); const stop = listenToOrb("run-a", 0, cb);
    const source = Source.instances[0]!;
    source.onerror?.(); expect(cb.onConnection).toHaveBeenLastCalledWith("reconnecting");
    expect(cb.onFrame).not.toHaveBeenCalled();
    stop(); source.emit("evidence", frame()); source.onopen?.(); source.onerror?.();
    expect(cb.onFrame).not.toHaveBeenCalled();
    expect(cb.onConnection).toHaveBeenLastCalledWith("reconnecting");
  });
  it("stops native retries after a hard connection refusal", () => {
    const cb = callbacks(); listenToOrb("run-a", 0, cb);
    const source = Source.instances[0]!; source.readyState = Source.CLOSED; source.onerror?.();
    expect(cb.onConnection).toHaveBeenLastCalledWith("closed");
    expect(cb.onFault).toHaveBeenCalledOnce();
  });
});

it("keeps catalogue authorization refusal distinct from an empty catalogue", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 403 }));
  await expect(getOrbCatalogue()).rejects.toThrow("cannot read");
});
