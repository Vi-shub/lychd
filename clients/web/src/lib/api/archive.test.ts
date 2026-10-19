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
