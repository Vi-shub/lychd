import { afterEach, expect, it, vi } from "vitest";
import type { NoteProposal } from "./models";
afterEach(() => { vi.unstubAllGlobals(); document.cookie = "archive-csrf=; Max-Age=0"; });
function browserRequests() { const Native = globalThis.Request; vi.stubGlobal("Request", class extends Native { constructor(input: RequestInfo | URL, init?: RequestInit) { super(typeof input === "string" ? new URL(input, "http://localhost") : input, init); } }); }
it("uses exact finite metadata/default reads, cancellation, frozen JSON and protected explicit CAS writes", async () => {
  browserRequests(); const requests: Request[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => { if (request.url.endsWith("/altar/status")) return Response.json({ csrf: { cookie_name: "archive-csrf", header_name: "X-Archive-CSRF" } }); requests.push(request); return Response.json({}); }));
  vi.resetModules(); const api = await import("./client"); document.cookie = "archive-csrf=only-csrf";
  const id = "00000000-0000-4000-8000-000000000001", abort = new AbortController();
  const body: NoteProposal = { request_id: id, namespace_id: id, source_key: " key & é ", source_revision: "r1", source_digest: "a".repeat(64), text: " é\r\n ", producer: "p", observed_at: "2026-10-19T00:00:00Z", classification: "public" };
  await api.getArchiveProfile(abort.signal); await api.getArchiveBridgeDefault(abort.signal);
  await api.listArchiveNotes({ sourceKey: body.source_key, cursor: "cursor+/=", signal: abort.signal }); await api.getArchiveNote(id, abort.signal);
  await api.admitArchiveNote(body); await api.promoteArchiveNote(id, { expected_version: 7 }); await api.revokeArchiveNote(id, { expected_version: 8 });
  expect(requests).toHaveLength(7); expect(requests.map((r) => r.method)).toEqual(["GET", "GET", "GET", "GET", "POST", "POST", "POST"]);
  expect(requests[1]!.url).toBe("http://localhost/api/v1/archive/bridge-default");
  const query = new URL(requests[2]!.url).searchParams; expect([...query]).toEqual([["limit", "50"], ["source_key", body.source_key], ["cursor", "cursor+/="]]);
  expect(requests[3]!.url).toBe(`http://localhost/api/v1/archive/notes/${id}`);
  expect(await requests[4]!.text()).toBe(JSON.stringify(body)); expect(await requests[5]!.json()).toEqual({ expected_version: 7 });
  for (const request of requests.slice(4)) { expect(request.headers.get("X-Archive-CSRF")).toBe("only-csrf"); expect(request.headers.has("Authorization")).toBe(false); }
  abort.abort(); expect(requests.slice(0, 4).every((r) => r.signal.aborted)).toBe(true);
});
it("retains typed safe status/code for presentation to close arbitrary server detail", async () => {
  browserRequests(); vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "arbitrary sensitive detail", extra: { code: "stale_version" } }, { status: 409 })));
  vi.resetModules(); const api = await import("./client"); await expect(api.getArchiveNote("00000000-0000-4000-8000-000000000001")).rejects.toMatchObject({ status: 409, code: "stale_version" });
});
it("reads exact Run recall metadata by GET with cancellation and no CSRF discovery or mutation", async () => {
  browserRequests(); const requests: Request[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => { requests.push(request); return Response.json({}); }));
  vi.resetModules(); const api = await import("./client"); const controller = new AbortController();
  const id = "00000000-0000-4000-8000-000000000001";
  await api.getArchiveRunRecall(id, controller.signal);
  expect(requests).toHaveLength(1);
  expect(requests[0]!.url).toBe(`http://localhost/api/v1/archive/runs/${id}/recall`);
  expect(requests[0]!.method).toBe("GET"); expect(requests[0]!.body).toBeNull();
  expect([...requests[0]!.headers.keys()]).not.toContain("x-archive-csrf");
  controller.abort(); expect(requests[0]!.signal.aborted).toBe(true);
});

it("discovers generation by GET and protects exact semantic recall, metadata inspection and receipt resolution through generated paths", async () => {
  browserRequests(); const requests: Request[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    if (request.url.endsWith("/altar/status")) return Response.json({ csrf: { cookie_name: "archive-csrf", header_name: "X-Archive-CSRF" } });
    requests.push(request); return Response.json(request.url.endsWith("/inspect") ? null : {});
  }));
  vi.resetModules(); const api = await import("./client"); document.cookie = "archive-csrf=only-csrf";
  const id = "00000000-0000-4000-8000-000000000001", abort = new AbortController();
  const body = { namespace_id: id, request_id: id, generation: { generation_id: id, manifest_digest: "a".repeat(64) },
    query: " exact é\r\n ", min_cosine: -0.25, max_results: 3, char_budget: 4000, run_id: null };
  await api.discoverArchiveGeneration(abort.signal); await api.recallArchiveSemanticNotes(body);
  expect(await api.inspectArchiveSemanticRecall(body, abort.signal)).toBeNull();
  await api.resolveArchiveSemanticRecall(id, { receipt_digest: "b".repeat(64) }, abort.signal);
  expect(requests.map((request) => [new URL(request.url).pathname, request.method])).toEqual([
    ["/api/v1/archive/derivations/generations/configured", "GET"], ["/api/v1/archive/semantic/recalls", "POST"],
    ["/api/v1/archive/semantic/recalls/inspect", "POST"], [`/api/v1/archive/semantic/recalls/${id}/resolve`, "POST"]
  ]);
  expect(requests[0]!.body).toBeNull();
  expect(await requests[1]!.text()).toBe(JSON.stringify(body)); expect(await requests[2]!.text()).toBe(JSON.stringify(body));
  expect(await requests[3]!.json()).toEqual({ receipt_digest: "b".repeat(64) });
  for (const request of requests.slice(1)) { expect(request.headers.get("X-Archive-CSRF")).toBe("only-csrf"); expect(request.headers.has("Authorization")).toBe(false); }
  abort.abort(); expect(requests[0]!.signal.aborted).toBe(true); expect(requests[2]!.signal.aborted).toBe(true); expect(requests[3]!.signal.aborted).toBe(true);
});

it("protects explicit generation registration, exact derivation and metadata status independently without fabricated occurrence fields", async () => {
  browserRequests(); const requests: Request[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    if (request.url.endsWith("/altar/status")) return Response.json({ csrf: { cookie_name: "archive-csrf", header_name: "X-Archive-CSRF" } });
    requests.push(request); return Response.json({});
  }));
  vi.resetModules(); const api = await import("./client"); document.cookie = "archive-csrf=only-csrf";
  const id = "00000000-0000-4000-8000-000000000001", abort = new AbortController();
  const generation = { generation_id: id, manifest_digest: "a".repeat(64) };
  const body = { generation, source: { record_id: id, revision_digest: "b".repeat(64), lifecycle_version: 7 } };
  await api.registerArchiveGeneration(generation); await api.deriveArchiveNote(body); await api.inspectArchiveDerivation(body, abort.signal);
  expect(requests.map((r) => [new URL(r.url).pathname, r.method])).toEqual([
    ["/api/v1/archive/derivations/generations/register", "POST"], ["/api/v1/archive/derivations/derive", "POST"], ["/api/v1/archive/derivations/status", "POST"]
  ]);
  expect(await requests[0]!.text()).toBe(JSON.stringify(generation));
  expect(await requests[1]!.text()).toBe(JSON.stringify(body)); expect(await requests[2]!.text()).toBe(JSON.stringify(body));
  for (const request of requests) { expect(request.headers.get("X-Archive-CSRF")).toBe("only-csrf"); expect(request.headers.has("Authorization")).toBe(false); }
  abort.abort(); expect(requests[2]!.signal.aborted).toBe(true);
});
