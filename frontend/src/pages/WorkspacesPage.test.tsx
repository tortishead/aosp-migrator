import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Workspace } from "../api";
import { mockApi, renderRoute } from "../test-utils";

function workspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: 1,
    name: "Android 15",
    jql: "project = OEM",
    target_tree: "/src/aosp15",
    target_tree_type: "repo",
    source_tree: null,
    source_tree_type: null,
    link_types: [],
    created_at: "2026-10-25T19:00:00Z",
    updated_at: "2026-10-25T19:00:00Z",
    ...overrides,
  };
}

describe("WorkspacesPage", () => {
  it("lists workspaces with tree types", async () => {
    mockApi({
      "GET /workspaces": () => [
        workspace(),
        workspace({ id: 2, name: "Vendor", target_tree: "/src/vendor", target_tree_type: "git", source_tree: "/src/old", source_tree_type: "plain" }),
      ],
    });
    renderRoute("/");
    const row = (await screen.findByRole("link", { name: "Vendor" })).closest("tr")!;
    expect(within(row).getByText("/src/vendor")).toBeInTheDocument();
    expect(within(row).getByText("git")).toBeInTheDocument();
    expect(within(row).getByText("plain")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Android 15" })).toHaveAttribute("href", "/workspaces/1");
  });

  it("shows an empty state", async () => {
    mockApi({});
    renderRoute("/");
    expect(await screen.findByText(/No workspaces yet/)).toBeInTheDocument();
  });

  it("creates a workspace and returns to the list", async () => {
    let list: Workspace[] = [];
    const calls = mockApi({
      "GET /workspaces": () => list,
      "POST /workspaces": () => {
        list = [workspace()];
        return { status: 201, body: list[0] };
      },
    });
    renderRoute("/");
    await userEvent.click(await screen.findByRole("link", { name: "New workspace" }));
    await userEvent.type(await screen.findByLabelText("Name"), "Android 15");
    await userEvent.type(screen.getByLabelText("JQL"), "project = OEM");
    await userEvent.type(screen.getByLabelText("Target tree path"), "/src/aosp15");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("link", { name: "Android 15" })).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "Android 15",
      jql: "project = OEM",
      target_tree: "/src/aosp15",
      source_tree: "",
      link_types: [],
    });
  });

  it("shows path validation errors from the server", async () => {
    mockApi({
      "POST /workspaces": () => ({ status: 422, body: { detail: "Target tree does not exist: /nope" } }),
    });
    renderRoute("/workspaces/new");
    await userEvent.type(await screen.findByLabelText("Name"), "x");
    await userEvent.type(screen.getByLabelText("JQL"), "project = X");
    await userEvent.type(screen.getByLabelText("Target tree path"), "/nope");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Target tree does not exist: /nope")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "New workspace" })).toBeInTheDocument();
  });

  it("edits an existing workspace", async () => {
    const calls = mockApi({
      "GET /workspaces": () => [workspace()],
      "GET /workspaces/1": () => workspace({ link_types: ["Relates"] }),
      "PUT /workspaces/1": (body) => workspace(body as Partial<Workspace>),
    });
    renderRoute("/");
    await userEvent.click(await screen.findByRole("link", { name: "Edit Android 15" }));
    const jql = await screen.findByLabelText("JQL");
    expect(jql).toHaveValue("project = OEM");
    await userEvent.clear(jql);
    await userEvent.type(jql, "project = NEW");
    const linkTypes = screen.getByLabelText("Relevant link types");
    expect(linkTypes).toHaveValue("Relates");
    await userEvent.type(linkTypes, ", is blocked by");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("heading", { name: "Workspaces" })).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      name: "Android 15",
      jql: "project = NEW",
      target_tree: "/src/aosp15",
      source_tree: "",
      link_types: ["Relates", "is blocked by"],
    });
  });

  it("deletes a workspace after confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = mockApi({
      "GET /workspaces/1": () => workspace(),
      "DELETE /workspaces/1": () => undefined,
    });
    renderRoute("/workspaces/1/edit");
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));
    expect(await screen.findByRole("heading", { name: "Workspaces" })).toBeInTheDocument();
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/workspaces/1")).toBe(true);
  });
});

describe("workspace switcher", () => {
  it("is hidden without workspaces", async () => {
    mockApi({});
    renderRoute("/");
    await screen.findByText(/No workspaces yet/);
    expect(screen.queryByRole("combobox", { name: "Workspace" })).not.toBeInTheDocument();
  });

  it("shows the current workspace and navigates to another", async () => {
    mockApi({ "GET /workspaces": () => [workspace(), workspace({ id: 2, name: "Vendor" })] });
    renderRoute("/workspaces/1");
    const switcher = await screen.findByRole("combobox", { name: "Workspace" });
    expect(switcher).toHaveValue("1");
    await userEvent.selectOptions(switcher, "Vendor");
    expect(await screen.findByRole("heading", { name: "Vendor" })).toBeInTheDocument();
    expect(switcher).toHaveValue("2");
  });
});
