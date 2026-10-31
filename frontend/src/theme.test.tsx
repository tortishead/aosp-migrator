import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderRoute } from "./test-utils";
import { THEME_KEY } from "./theme";

function stubSystemDark(dark: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: dark && query.includes("dark"),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

describe("day/night mode", () => {
  beforeEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.theme;
  });

  it("follows the system preference until toggled, then remembers the choice", async () => {
    stubSystemDark(true);
    mockApi({});
    renderRoute("/");

    const toggle = await screen.findByRole("button", { name: "Switch to day mode" });
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem(THEME_KEY)).toBeNull();

    await userEvent.click(toggle);
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    expect(screen.getByRole("button", { name: "Switch to night mode" })).toBeInTheDocument();
  });

  it("uses the saved choice over the system preference", async () => {
    stubSystemDark(false);
    localStorage.setItem(THEME_KEY, "dark");
    mockApi({});
    renderRoute("/");

    expect(await screen.findByRole("button", { name: "Switch to day mode" })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
