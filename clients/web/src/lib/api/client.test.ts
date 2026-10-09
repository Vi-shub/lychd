import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunEventEnvelope, TransitionEventEnvelope } from "./models";
import { listenToRun, listenToSwap } from "./client";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static CONNECTING = 0;
  static CLOSED = 2;

  onerror: ((event: Event) => void) | null = null;
  readyState = FakeEventSource.CONNECTING;
  closeCalls = 0;
  private readonly listeners = new Map<string, EventListenerOrEventListenerObject[]>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject | null
  ) {
    if (!listener) return;
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close() {
    this.readyState = FakeEventSource.CLOSED;
    this.closeCalls++;
  }

  emit(type: string, data: string) {
    const event = new MessageEvent<string>(type, { data });
    for (const listener of this.listeners.get(type) ?? []) {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    }
  }

  disconnect() {
    this.onerror?.(new Event("error"));
  }
}

function runEvent(seq: number): RunEventEnvelope {
  return {
    schema_version: 1,
    run_id: "run-a",
    event_id: "00000000-0000-4000-8000-000000000001",
    seq,
    kind: "token",
    occurred_at: "2026-07-28T00:00:00Z",
    payload: { text: "ash" }
  };
}

function transitionEvent(ticketId: string): TransitionEventEnvelope {
  return {
    schema_version: 1,
    seq: 0,
    ticket: {
      id: ticketId,
      request_id: "request-a",
      target: "chat:local",
      state: "warming",
      phase: "warming",
      action_type: "SWAP",
      total_metabolic_cost: 1,
      physical_transition_id: null,
      compensation_transition_id: null
    }
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => { resolve = accept; });
  return { promise, resolve };
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("run stream lifecycle", () => {
  it("reduces queued events against the replacement snapshot cursor in order", async () => {
    const recovery = deferred<{ cursor: number; terminal: boolean }>();
    const onRefetch = vi.fn(() => recovery.promise);
    const onEvent = vi.fn();
    const onHardClose = vi.fn();
    const stop = listenToRun("run-a", onEvent, vi.fn(), onRefetch, { onHardClose });
    const source = FakeEventSource.instances[0]!;
    source.emit("token", JSON.stringify(runEvent(2)));
    await vi.waitFor(() => expect(onRefetch).toHaveBeenCalledOnce());
    source.emit("token", JSON.stringify(runEvent(3)));
    source.emit("token", JSON.stringify(runEvent(4)));
    source.emit("token", JSON.stringify(runEvent(5)));
    expect(onEvent).not.toHaveBeenCalled();

    recovery.resolve({ cursor: 3, terminal: false });
    await vi.waitFor(() => expect(onEvent).toHaveBeenCalledTimes(2));
    expect(onEvent.mock.calls.map(([event]) => event.seq)).toEqual([4, 5]);
    expect(onRefetch).toHaveBeenCalledOnce();
    expect(onHardClose).not.toHaveBeenCalled();
    stop();
  });

  it("bounds events behind a stalled refetch and hands overflow to recovery once", async () => {
    const recovery = deferred<{ cursor: number; terminal: boolean }>();
    const onRefetch = vi.fn(() => recovery.promise);
    const onEvent = vi.fn();
    const onFault = vi.fn();
    const onHardClose = vi.fn();
    listenToRun("run-a", onEvent, onFault, onRefetch, { onHardClose });
    const source = FakeEventSource.instances[0]!;
    source.emit("token", JSON.stringify(runEvent(2)));
    await vi.waitFor(() => expect(onRefetch).toHaveBeenCalledOnce());

    for (let seq = 3; seq < 259; seq++) source.emit("token", JSON.stringify(runEvent(seq)));
    expect(onHardClose).not.toHaveBeenCalled();
    source.emit("token", JSON.stringify(runEvent(259)));
    expect(onHardClose).toHaveBeenCalledOnce();
    expect(onFault).toHaveBeenCalledWith("The run stream exceeded its pending event limit; its projection may be stale.");
    expect(source.closeCalls).toBe(1);

    source.emit("token", JSON.stringify(runEvent(260)));
    source.emit("token", "{}");
    recovery.resolve({ cursor: 258, terminal: false });
    await recovery.promise;
    await Promise.resolve();
    expect(onEvent).not.toHaveBeenCalled();
    expect(onRefetch).toHaveBeenCalledOnce();
    expect(onHardClose).toHaveBeenCalledOnce();
    expect(source.closeCalls).toBe(1);
  });

  it("discards pending events when torn down during an authoritative refetch", async () => {
    const recovery = deferred<{ cursor: number; terminal: boolean }>();
    const onRefetch = vi.fn(() => recovery.promise);
    const onEvent = vi.fn();
    const onFault = vi.fn();
    const stop = listenToRun("run-a", onEvent, onFault, onRefetch);
    const source = FakeEventSource.instances[0]!;
    source.emit("token", JSON.stringify(runEvent(2)));
    await vi.waitFor(() => expect(onRefetch).toHaveBeenCalledOnce());
    source.emit("token", JSON.stringify(runEvent(3)));
    stop();

    recovery.resolve({ cursor: 2, terminal: false });
    await recovery.promise;
    await Promise.resolve();
    source.emit("token", JSON.stringify(runEvent(4)));
    expect(onEvent).not.toHaveBeenCalled();
    expect(onFault).not.toHaveBeenCalled();
    expect(source.closeCalls).toBe(1);
  });

  it("reports a hard close for invalid data but leaves transient reconnects alive", () => {
    const onFault = vi.fn();
    const onHardClose = vi.fn();
    listenToRun(
      "run-a",
      vi.fn(),
      onFault,
      async () => ({ cursor: 0, terminal: false }),
      { onHardClose }
    );
    const source = FakeEventSource.instances[0];
    if (!source) throw new Error("The EventSource was not opened.");

    source.disconnect();
    expect(onFault).toHaveBeenCalledWith("The run stream went quiet; reconnecting.");
    expect(onHardClose).not.toHaveBeenCalled();
    expect(source.closeCalls).toBe(0);

    source.emit("token", "{}");
    expect(onHardClose).toHaveBeenCalledOnce();
    expect(source.closeCalls).toBe(1);
    expect(onFault).toHaveBeenLastCalledWith("The Vessel emitted an invalid run event.");
  });

  it("hard-closes exactly once when an authoritative refetch fails", async () => {
    const onFault = vi.fn();
    const onHardClose = vi.fn();
    const onRefetch = vi.fn().mockRejectedValue(new Error("snapshot unavailable"));
    listenToRun("run-a", vi.fn(), onFault, onRefetch, { onHardClose });
    const source = FakeEventSource.instances[0];
    if (!source) throw new Error("The EventSource was not opened.");

    source.emit("token", JSON.stringify(runEvent(2)));

    await vi.waitFor(() => expect(onHardClose).toHaveBeenCalledOnce());
    expect(source.closeCalls).toBe(1);
    expect(onFault).toHaveBeenCalledWith(
      "The authoritative run snapshot could not be refreshed."
    );

    source.emit("token", "{}");
    expect(onHardClose).toHaveBeenCalledOnce();
  });

  it("hands a permanent HTTP stream refusal to recovery once without retrying itself", () => {
    const onFault = vi.fn();
    const onHardClose = vi.fn();
    listenToRun("run-a", vi.fn(), onFault, vi.fn(), { onHardClose });
    const source = FakeEventSource.instances[0];
    if (!source) throw new Error("The EventSource was not opened.");

    // Non-200 SSE responses, including the stream budget's 429, are fatal to EventSource.
    source.readyState = FakeEventSource.CLOSED;
    source.disconnect();
    source.disconnect();

    expect(onHardClose).toHaveBeenCalledOnce();
    expect(onFault).toHaveBeenCalledWith("The run stream closed; its projection may be stale.");
    expect(source.closeCalls).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("hard-closes a structurally valid event from another run", () => {
    const onEvent = vi.fn();
    const onFault = vi.fn();
    const onHardClose = vi.fn();
    listenToRun("run-a", onEvent, onFault, vi.fn(), { onHardClose });
    const source = FakeEventSource.instances[0];
    if (!source) throw new Error("The EventSource was not opened.");

    source.emit("token", JSON.stringify({ ...runEvent(0), run_id: "run-b" }));

    expect(onEvent).not.toHaveBeenCalled();
    expect(onHardClose).toHaveBeenCalledWith(
      "The Vessel emitted a run event for another Run."
    );
    expect(onFault).toHaveBeenCalledWith(
      "The Vessel emitted a run event for another Run."
    );
    expect(source.closeCalls).toBe(1);
  });
});

describe("transition stream lifecycle", () => {
  it("keeps native reconnects alive but hard-closes a permanent HTTP refusal once", () => {
    const onFault = vi.fn();
    const onHardClose = vi.fn();
    const onEvent = vi.fn();
    listenToSwap("ticket-a", onEvent, onFault, { onHardClose });
    const source = FakeEventSource.instances[0]!;
    source.disconnect();
    expect(onHardClose).not.toHaveBeenCalled();
    expect(source.closeCalls).toBe(0);

    source.readyState = FakeEventSource.CLOSED;
    source.disconnect();
    source.disconnect();
    source.emit("transition", JSON.stringify(transitionEvent("ticket-a")));
    expect(onHardClose).toHaveBeenCalledOnce();
    expect(onFault).toHaveBeenLastCalledWith("The transition stream closed; its projection may be stale.");
    expect(source.closeCalls).toBe(1);
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("hard-closes a structurally valid event from another ticket", () => {
    const onEvent = vi.fn();
    const onFault = vi.fn();
    const onHardClose = vi.fn();
    listenToSwap("ticket-a", onEvent, onFault, { onHardClose });
    const source = FakeEventSource.instances[0];
    if (!source) throw new Error("The EventSource was not opened.");

    source.emit("transition", JSON.stringify(transitionEvent("ticket-b")));

    expect(onEvent).not.toHaveBeenCalled();
    expect(onHardClose).toHaveBeenCalledWith(
      "The Vessel emitted a transition event for another ticket."
    );
    expect(onFault).toHaveBeenCalledWith(
      "The Vessel emitted a transition event for another ticket."
    );
    expect(source.closeCalls).toBe(1);
  });

  it("ignores queued transition callbacks after the stream reaches terminal truth", () => {
    const onEvent = vi.fn();
    listenToSwap("ticket-a", onEvent, vi.fn());
    const source = FakeEventSource.instances[0];
    if (!source) throw new Error("The EventSource was not opened.");
    const settled = transitionEvent("ticket-a");
    settled.ticket.state = "settled";

    source.emit("transition", JSON.stringify(settled));
    source.emit("transition", JSON.stringify(transitionEvent("ticket-a")));

    expect(onEvent).toHaveBeenCalledOnce();
    expect(onEvent).toHaveBeenCalledWith(settled);
    expect(source.closeCalls).toBe(1);
  });
});
