import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { mockApi, renderRoute } from "../test-utils";

describe("LAN login", () => {
  it("shows login when required, then the app after a valid token", async () => {
    let authenticated = false;
    const calls = mockApi({
      "GET /auth/status": () => ({ required: true, authenticated }),
      "POST /auth/login": (body) => {
        if ((body as { token: string }).token !== "good") return { status: 401, body: { detail: "Invalid token" } };
        authenticated = true;
        return { authenticated: true };
      },
    });
    renderRoute("/");

    const input = await screen.findByLabelText("Access token");
    await userEvent.type(input, "bad");
    await userEvent.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByText("Invalid token")).toBeInTheDocument();

    await userEvent.clear(input);
    await userEvent.type(input, "good");
    await userEvent.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("heading", { name: "Workspaces" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
    expect(calls.filter((c) => c.path === "/auth/login")).toHaveLength(2);
  });

  it("hides login and logout when not in LAN mode", async () => {
    mockApi({});
    renderRoute("/");
    expect(await screen.findByRole("heading", { name: "Workspaces" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Log out" })).not.toBeInTheDocument();
  });
});
