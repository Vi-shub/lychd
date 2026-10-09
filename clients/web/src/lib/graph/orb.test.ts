import { describe, expect, it } from "vitest";
import { fixtureRun, pattern } from "$lib/orb/__fixtures__/snapshot";
import { matchesOrbPattern, mergeOrbEvidence, orbEvidenceNodeId, projectOrb, projectOrbField } from "./orb";

describe("Orb semantic graph", () => {
  it("keeps declared permissions distinct from supplied records, including a return edge", () => {
    const snapshot = fixtureRun();
    const graph = projectOrb(snapshot, pattern);
    expect(graph.edges).toHaveLength(2);
    expect(graph.edges.every((edge) => edge.relation === "permission")).toBe(true);
    expect(graph.nodes.find((node) => node.id === "station:inspect")?.count).toBe(1);
    expect(graph.nodes.find((node) => node.id === "station:revise")?.detail).toContain("no recorded phase");
    expect(graph.nodes.find((node) => node.id === "station:revise")?.state).toBe("unknown");
  });
  it("does not relabel a dispatch subject as a station and keeps unknowns and gaps explicit", () => {
    const snapshot = fixtureRun();
    snapshot.evidence = [{ ...snapshot.evidence[0]!, kind: "dispatch" }, { ...snapshot.evidence[0]!, event_id: "unknown", seq: 2, subject_key: null }];
    snapshot.gaps = [{ start_seq: 1, end_seq: 1, classification: "unknown_or_omitted" }];
    const graph = projectOrb(snapshot, pattern);
    expect(graph.nodes.find((node) => node.id === "station:inspect")?.count).toBe(0);
    expect(graph.nodes.some((node) => node.label === "Unknown subject")).toBe(true);
    expect(graph.nodes.some((node) => node.kind === "gap")).toBe(true);
    expect(orbEvidenceNodeId(snapshot.evidence[0]!, pattern)).toMatch(/^subject:dispatch:/);
  });
  it("requires exact pinned manifest identity before adding workflow edges", () => {
    const snapshot = fixtureRun();
    expect(matchesOrbPattern(snapshot, pattern)).toBe(true);
    expect(matchesOrbPattern(snapshot, { ...pattern, digest: "different" })).toBe(false);
    expect(projectOrb(snapshot, null).edges).toEqual([]);
  });
  it("unfolds exact Run/subject/occurrence/record membership without a causal edge", () => {
    const a = fixtureRun(), b = fixtureRun("run-b");
    const lenses = new Map([["run-a", { snapshot: a, pattern }], ["run-b", { snapshot: b, pattern }]]);
    const collapsed = projectOrbField([a.run, b.run], lenses, "runs");
    const unfolded = projectOrbField([a.run, b.run], lenses, "records");
    expect(collapsed.projection.nodes).toHaveLength(2);
    expect(collapsed.projection.nodes[0]!.members).toContain("run:run-a|record%3Arun-a-event-0");
    expect(unfolded.projection.identity).toBe(collapsed.projection.identity);
    expect(unfolded.projection.nodes.filter((node) => node.kind === "occurrence")).toHaveLength(2);
    expect(unfolded.projection.nodes.filter((node) => node.kind === "event")).toHaveLength(2);
    expect(new Set(unfolded.projection.nodes.map((node) => node.id)).size).toBe(unfolded.projection.nodes.length);
    expect(unfolded.projection.edges.every((edge) => ["permission", "contains"].includes(edge.relation))).toBe(true);
    expect([...unfolded.selections.values()].some((item) => item.runId === "run-b" && item.eventId === "run-b-event-0")).toBe(true);
  });
  it("deduplicates replayed immutable identities and keeps supplied sequence order", () => {
    const event = fixtureRun().evidence[0]!;
    expect(mergeOrbEvidence([event], [{ ...event, event_id: "second", seq: 1 }, event]).map((item) => item.event_id)).toEqual([event.event_id, "second"]);
  });
});
