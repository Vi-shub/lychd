import { render, screen } from "@testing-library/svelte";
import { beforeEach, expect, it, vi } from "vitest";
vi.mock("$app/state", async () => { const { SvelteURL } = await import("svelte/reactivity"); return { page: { state: {}, url: new SvelteURL("http://localhost/bridge") } }; });
vi.mock("$app/navigation", () => ({ goto: vi.fn(), afterNavigate: vi.fn(), beforeNavigate: vi.fn() }));
vi.mock("$lib/api/client", () => ({ ApiError: class extends Error {}, getArchiveProfile: vi.fn().mockRejectedValue(new Error()), getArchiveBridgeDefault: vi.fn(), listArchiveNotes: vi.fn(), getArchiveNote: vi.fn(), admitArchiveNote: vi.fn(), promoteArchiveNote: vi.fn(), revokeArchiveNote: vi.fn() }));
import { page } from "$app/state";
import { getArchiveProfile, admitArchiveNote } from "$lib/api/client";
import Page from "./+page.svelte";
beforeEach(() => { vi.clearAllMocks(); });
it.each(["notes=new&notes=manage", "notes=invalid", "notes=new&investigation=new", "notes=manage&investigation="])("rejects ambiguous focus %s without reads or admission", async (query) => {
  page.url.href = `http://localhost/bridge?${query}`; const view = render(Page);
  expect(screen.getByRole("heading", { name: "Public-note selection unavailable" })).toBeTruthy(); expect(getArchiveProfile).not.toHaveBeenCalled(); expect(admitArchiveNote).not.toHaveBeenCalled(); view.unmount();
});
it("dispatches closed notes focus to metadata independently from conversation", async () => {
  page.url.href = "http://localhost/bridge?notes=manage"; const view = render(Page); await screen.findByRole("heading", { name: "Public notes" }); expect(getArchiveProfile).toHaveBeenCalledTimes(1); expect(admitArchiveNote).not.toHaveBeenCalled(); view.unmount();
});
