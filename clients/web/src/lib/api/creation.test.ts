import { afterEach, expect, it, vi } from "vitest";
import type { components } from "./openapi";

afterEach(() => { vi.unstubAllGlobals(); document.cookie = "creation-csrf=; Max-Age=0"; });
it("uses the existing protected same-origin/CSRF transport with literal unchanged body and exact read UUID", async () => {
  const NativeRequest = globalThis.Request;
  class BrowserRequest extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) { super(typeof input === "string" ? new URL(input, "http://localhost") : input, init); }
  }
  const sent: Request[] = [];
  vi.stubGlobal("Request", BrowserRequest);
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    if (request.url.endsWith("/altar/status")) return Response.json({ csrf: { cookie_name: "creation-csrf", header_name: "X-Creation-Csrf" } });
    sent.push(request); return Response.json({ case_id: "00000000-0000-4000-8000-000000000001" });
  }));
  document.cookie = "creation-csrf=only-csrf";
  vi.resetModules(); const { submitCreationInvestigation, readCreationInvestigation } = await import("./client");
  const body: components["schemas"]["InvestigationRequest"] = { request_id: "00000000-0000-4000-8000-000000000001", public_material: true,
    repository: "public/example", base_revision: "main", tree_or_patch_digest: "a".repeat(40), question: "  Literal é? ",
    sources: [{ name: "a.py", text: "  é\n" }], criteria: [{ id: "answer", predicate: "Cite it." }], non_goals: [], omissions: [] };
  await submitCreationInvestigation(body); await readCreationInvestigation(body.request_id);
  expect(sent).toHaveLength(2); expect(sent[0]!.method).toBe("POST");
  expect(sent[0]!.headers.get("X-Creation-Csrf")).toBe("only-csrf");
  expect(sent[0]!.headers.has("Authorization")).toBe(false); // Browser HTTP authentication remains outside client code.
  expect(await sent[0]!.text()).toBe(JSON.stringify(body));
  expect(sent[1]!.method).toBe("GET"); expect(sent[1]!.url).toBe(`http://localhost/api/v1/creation/investigations/${body.request_id}`);
});


function browserRequests(): void {
  const NativeRequest = globalThis.Request;
  class BrowserRequest extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === "string" ? new URL(input, "http://localhost") : input, init);
    }
  }
  vi.stubGlobal("Request", BrowserRequest);
}

it("reads historical feedback with the exact protected GET and propagates cancellation without a CSRF read", async () => {
  browserRequests();
  const caseId = "00000000-0000-4000-8000-000000000001";
  const feedback: components["schemas"]["FeedbackView"] = {
    case_id: caseId, assessments: [], decisions: [], current_assessment_id: null,
    current_decision_id: null, native_status: "complete", native_verdict: null,
    status: "unassessed", target: null
  };
  const sent: Request[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    sent.push(request); return Response.json(feedback);
  }));
  vi.resetModules(); const { readCreationFeedback } = await import("./client");
  const controller = new AbortController();
  expect(await readCreationFeedback(caseId, controller.signal)).toEqual(feedback);
  expect(sent).toHaveLength(1);
  expect(sent[0]!.method).toBe("GET");
  expect(sent[0]!.url).toBe(`http://localhost/api/v1/creation/investigations/${caseId}/feedback`);
  expect(sent[0]!.headers.has("Authorization")).toBe(false);
  expect(sent[0]!.signal.aborted).toBe(false);
  controller.abort(); expect(sent[0]!.signal.aborted).toBe(true);
});

it("preserves a protected feedback refusal as a typed status for the disclosure to clear", async () => {
  browserRequests();
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "Not permitted" }, { status: 403 })));
  vi.resetModules(); const { readCreationFeedback, ApiError } = await import("./client");
  const failure: unknown = await readCreationFeedback("00000000-0000-4000-8000-000000000001").catch((error: unknown) => error);
  expect(failure).toBeInstanceOf(ApiError);
  expect(failure).toMatchObject({ status: 403 });
});
