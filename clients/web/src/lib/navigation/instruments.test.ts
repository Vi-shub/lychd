import { describe, expect, it } from "vitest";
import { bridgeRunHref, loomOriginHref, orbReturnHref, selectedBridgeHref, selectedOrbHref, scavengerRunHref } from "./instruments";

describe("instrument navigation hints", () => {
  it("normalizes an explicit Bridge session beneath the configured base", () => {
    expect(selectedBridgeHref("/altar/bridge/session%2Db", "/altar/bridge")).toBe("/bridge/session-b");
    expect(selectedBridgeHref("/bridge/session-b", "/altar/bridge")).toBeNull();
    expect(selectedBridgeHref("/elsewhere/altar/bridge/session-b", "/altar/bridge")).toBeNull();
    expect(selectedBridgeHref("/altar/bridge/session-b/extra", "/altar/bridge")).toBeNull();
  });

  it("preserves bounded selection without admitting foreign routes or query authority", () => {
    expect(selectedOrbHref("/altar/orb/run-a", "?event=event-a&job=job-a&redirect=https://elsewhere.test", "/altar/orb"))
      .toBe("/orb/run-a?event=event-a&job=job-a");
    for (const path of ["/orb", "//elsewhere/orb/run-a", "/orb/../bridge", "/orb/run-a/extra", "/orb/%2Fbridge", "/orb/%E0%A4", `/orb/${"a".repeat(129)}`]) {
      expect(selectedOrbHref(path, "?event=event-a")).toBeNull();
    }
    expect(selectedOrbHref("/orb/run-a", `?event=${"a".repeat(129)}`)).toBe("/orb/run-a");
  });

  it("carries one Run and event through an exact Pattern and back", () => {
    const target = loomOriginHref("/loom/bridge_chat/1?run=wrong", "run-a", "?event=event-a&job=job-a");
    expect(target).toBe("/loom/bridge_chat/1?run=run-a&event=event-a&job=job-a");
    expect(orbReturnHref("run-a", new URL(target!, "http://localhost").search))
      .toBe("/orb/run-a?event=event-a&job=job-a");
    expect(loomOriginHref("https://elsewhere/loom/p/1", "run-a")).toBeNull();
    expect(bridgeRunHref("/bridge/session-a", "run-a")).toBe("/bridge/session-a?run=run-a");
    expect(bridgeRunHref("//elsewhere/bridge/session-a", "run-a")).toBeNull();
  });
});

it("accepts only the retained Scavenger UUID navigation hint and permits no invented Bridge context", () => {
  const path = "/scavenger?observation=00000000-0000-4000-8000-000000000001";
  expect(scavengerRunHref(path)).toBe(path); expect(bridgeRunHref(null, "run-a")).toBeNull();
  for (const invalid of [null, undefined, "javascript:alert(1)", `https://foreign.test${path}`, `/${path}`,
    `${path}&next=https://foreign.test`, "/scavenger?observation=../bridge"]) expect(scavengerRunHref(invalid)).toBeNull();
});
