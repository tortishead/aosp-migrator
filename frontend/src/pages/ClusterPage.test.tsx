import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ClusterDetail, ClusterMember } from "../api";
import { mockApi, renderRoute } from "../test-utils";

function member(key: string, overrides: Partial<ClusterMember> = {}): ClusterMember {
  return {
    item_id: Number(key.split("-")[1]),
    key,
    summary: `Summary of ${key}`,
    status: "Open",
    local_state: "new",
    dropped: false,
    diff_count: 1,
    ...overrides,
  };
}

function cluster(overrides: Partial<ClusterDetail> = {}): ClusterDetail {
  return {
    id: 5,
    name: "Camera",
    description: "HAL fixes",
    created_at: "2026-10-25T19:00:00Z",
    updated_at: "2026-10-25T19:00:00Z",
    members: [member("PROJ-103"), member("PROJ-101", { local_state: "not_relevant" }), member("PROJ-104", { dropped: true })],
    files: [
      { path: "a/Shared.java", touched_by: ["PROJ-101", "PROJ-103"] },
      { path: "b/Only.java", touched_by: ["PROJ-104"] },
    ],
    notes: [],
    history: [{ id: 1, type: "created", payload: { keys: ["PROJ-103", "PROJ-101", "PROJ-104"] }, created_at: "2026-10-25T19:00:00Z" }],
    ...overrides,
  };
}

const URL = "/workspaces/1/clusters/5";

function clusterApi(handlers: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({ "GET /workspaces/1/clusters/5": () => cluster(), ...handlers });
}

function row(key: string) {
  return screen.getByRole("link", { name: key }).closest("tr")!;
}

describe("ClusterPage", () => {
  it("shows ordered members with warnings, overlapping files and history", async () => {
    clusterApi();
    renderRoute(URL);
    expect(await screen.findByRole("heading", { name: "Camera" })).toBeInTheDocument();
    const members = screen.getByRole("region", { name: "Members" });
    expect(within(members).getAllByRole("link").map((l) => l.textContent)).toEqual(["PROJ-103", "PROJ-101", "PROJ-104"]);
    expect(within(row("PROJ-101")).getByText("not relevant")).toBeInTheDocument();
    expect(within(row("PROJ-104")).getByText("dropped")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move PROJ-103 up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move PROJ-104 down" })).toBeDisabled();

    const files = screen.getByRole("region", { name: "Changed files" });
    expect(within(files).getByText("1 touched by more than one item.")).toBeInTheDocument();
    expect(within(files).getByText("a/Shared.java").closest("tr")).toHaveClass("overlap");
    expect(within(files).getByText("b/Only.java").closest("tr")).not.toHaveClass("overlap");

    expect(within(screen.getByRole("region", { name: "History" })).getByText("Created with PROJ-103, PROJ-101, PROJ-104")).toBeInTheDocument();
  });

  it("moves a member down", async () => {
    const calls = clusterApi({ "PUT /workspaces/1/clusters/5/order": () => cluster() });
    renderRoute(URL);
    await userEvent.click(await screen.findByRole("button", { name: "Move PROJ-103 down" }));
    expect(calls.find((c) => c.method === "PUT")).toEqual({
      method: "PUT",
      path: "/workspaces/1/clusters/5/order",
      body: { keys: ["PROJ-101", "PROJ-103", "PROJ-104"] },
    });
  });

  it("removes a member, asking before the cluster would dissolve", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = clusterApi({
      "GET /workspaces/1/clusters/5": () => cluster({ members: [member("PROJ-101"), member("PROJ-102")] }),
      "DELETE /workspaces/1/clusters/5/members/PROJ-101": () => ({
        status: 409,
        body: { detail: "This would leave fewer than 2 items in 'Camera' and dissolve it" },
      }),
      "DELETE /workspaces/1/clusters/5/members/PROJ-101?confirm_dissolve=true": () => ({ dissolved: true }),
      "GET /workspaces/1/clusters": () => [],
    });
    const { router } = renderRoute(URL);
    await userEvent.click(await screen.findByRole("button", { name: "Remove PROJ-101" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/workspaces/1/clusters"));
    expect(confirm).toHaveBeenCalledWith("This would leave fewer than 2 items in 'Camera' and dissolve it. Continue?");
    expect(calls.filter((c) => c.method === "DELETE").map((c) => c.path)).toEqual([
      "/workspaces/1/clusters/5/members/PROJ-101",
      "/workspaces/1/clusters/5/members/PROJ-101?confirm_dissolve=true",
    ]);
  });

  it("keeps the cluster when the user declines to dissolve it", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const calls = clusterApi({
      "DELETE /workspaces/1/clusters/5/members/PROJ-101": () => ({ status: 409, body: { detail: "Would dissolve" } }),
    });
    const { router } = renderRoute(URL);
    await userEvent.click(await screen.findByRole("button", { name: "Remove PROJ-101" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(1));
    expect(router.state.location.pathname).toBe(URL);
    expect(screen.queryByText("Would dissolve")).not.toBeInTheDocument();
  });

  it("renames, sets the state of all members and adds a note", async () => {
    const calls = clusterApi({
      "PATCH /workspaces/1/clusters/5": () => cluster({ name: "Camera HAL" }),
      "POST /workspaces/1/items/state": () => ({ changed: ["PROJ-103", "PROJ-104"] }),
      "POST /workspaces/1/clusters/5/notes": () => ({ status: 201, body: { id: 1, text: "Go", created_at: "2026-10-25T20:00:00Z" } }),
    });
    renderRoute(URL);
    const name = await screen.findByRole("textbox", { name: "Name" });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await userEvent.clear(name);
    await userEvent.type(name, "Camera HAL");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ name: "Camera HAL", description: "HAL fixes" });

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "State for all items" }), "Closed");
    await userEvent.click(within(screen.getByRole("region", { name: "Members" })).getByRole("button", { name: "Apply" }));
    expect(calls.find((c) => c.path === "/workspaces/1/items/state")?.body).toEqual({
      keys: ["PROJ-103", "PROJ-101", "PROJ-104"],
      state: "closed",
    });

    await userEvent.type(screen.getByRole("textbox", { name: "New note" }), "Go");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    expect(calls.find((c) => c.path === "/workspaces/1/clusters/5/notes")?.body).toEqual({ text: "Go" });
  });

  it("dissolves after confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = clusterApi({ "DELETE /workspaces/1/clusters/5": () => undefined, "GET /workspaces/1/clusters": () => [] });
    const { router } = renderRoute(URL);
    await userEvent.click(await screen.findByRole("button", { name: "Dissolve cluster" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/workspaces/1/clusters"));
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/workspaces/1/clusters/5")).toBe(true);
    expect(await screen.findByText(/No clusters yet/)).toBeInTheDocument();
  });
});

describe("ClustersPage", () => {
  it("lists clusters with member order and warnings", async () => {
    mockApi({ "GET /workspaces/1/clusters": () => [cluster()] });
    renderRoute("/workspaces/1/clusters");
    expect(await screen.findByRole("link", { name: "Camera" })).toHaveAttribute("href", "/workspaces/1/clusters/5");
    expect(screen.getByText("PROJ-103 → PROJ-101 → PROJ-104")).toBeInTheDocument();
    expect(screen.getByText("2 to check")).toBeInTheDocument();
  });
});

describe("ClusterPage build", () => {
  it("builds the cluster as one unit", async () => {
    const calls = clusterApi({
      "GET /workspaces/1/build-jobs": () => [],
      "POST /workspaces/1/build": () => ({
        status: 202,
        body: {
          id: 4,
          workspace_id: 1,
          scope: "cluster",
          issue_keys: ["PROJ-103", "PROJ-101"],
          cluster_id: 5,
          skipped: [{ key: "PROJ-104", reason: "not pre-scanned" }],
          status: "running",
          done: 0,
          total: 1,
          errors: [],
          error: null,
          cancel_requested: false,
          started_at: "2026-10-26T20:00:00Z",
          finished_at: null,
          unit_keys: null,
          log_tail: null,
        },
      }),
    });
    renderRoute(URL);
    const section = await screen.findByRole("region", { name: "Build" });
    await userEvent.click(within(section).getByRole("button", { name: "Build cluster" }));
    expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/build")?.body).toEqual({ cluster_id: 5 });
    expect(await within(section).findByText(/Building … \(0 \/ 1\)/)).toBeInTheDocument();
    expect(within(section).getByText("1 item not built")).toBeInTheDocument();
  });
});
