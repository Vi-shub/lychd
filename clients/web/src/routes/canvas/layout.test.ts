import { render, screen } from "@testing-library/svelte";
import { createRawSnippet } from "svelte";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("$app/state", () => ({ page: { url: new URL("http://localhost/canvas") } }));
vi.mock("$lib/api/client", async (original) => ({
  ...await original<typeof import("$lib/api/client")>(),
  getAltarStatus: vi.fn()
}));

import { getAltarStatus } from "$lib/api/client";
import Layout from "../+layout.svelte";

afterEach(() => vi.restoreAllMocks());

it("opens the offline development Canvas without a protected status request or sign-in challenge", () => {
  const children = createRawSnippet(() => ({ render: () => "<p>Offline Canvas draft</p>" }));
  const view = render(Layout, { children });
  expect(screen.getByText("Offline Canvas draft")).toBeTruthy();
  expect(getAltarStatus).not.toHaveBeenCalled();
  expect(screen.queryByRole("navigation", { name: "Instruments" })).toBeNull();
  view.unmount();
});
