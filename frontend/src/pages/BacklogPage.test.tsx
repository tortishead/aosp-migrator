import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Cluster, Facets, ImportJob, Item, ItemPage } from "../api";
import { POLL_MS } from "../backlog/ImportPanel";
import { mockApi, renderRoute, type ApiCall } from "../test-utils";

const workspace = {
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
};

const facets: Facets = {
  statuses: ["In Progress", "Open"],
  labels: ["camera", "oem-patch"],
  components: ["Camera", "Settings"],
  fix_versions: ["2026.Q4"],
};

function item(key: string, overrides: Partial<Item> = {}): Item {
  return {
    id: Number(key.split("-")[1]),
    key,
    summary: `Summary of ${key}`,
    status: "Open",
    labels: ["oem-patch"],
    components: ["Camera"],
    fix_versions: ["2026.Q4"],
    jira_updated: "2026-09-25T16:00:00Z",
    local_state: "new",
    dropped: false,
    last_import_error: null,
    diff_count: 1,
    cluster: null,
    ...overrides,
  };
}

function page(items: Item[], total = items.length, offset = 0): ItemPage {
  return { items, total, offset, limit: 50 };
}

function job(overrides: Partial<ImportJob> = {}): ImportJob {
  return {
    id: 7,
    workspace_id: 1,
    scope: "all",
    issue_keys: [],
    status: "running",
    done: 0,
    total: null,
    errors: [],
    error: null,
    cancel_requested: false,
    started_at: "2026-10-25T20:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

function itemQueries(calls: ApiCall[]): URLSearchParams[] {
  return calls
    .filter((c) => c.method === "GET" && c.path.startsWith("/workspaces/1/items?"))
    .map((c) => new URLSearchParams(c.path.split("?")[1]));
}

function lastItemQuery(calls: ApiCall[]): URLSearchParams {
  const queries = itemQueries(calls);
  return queries[queries.length - 1];
}

function backlogApi(handlers: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({
    "GET /workspaces": () => [workspace],
    "GET /workspaces/1/items": () => page([item("PROJ-101"), item("PROJ-102", { status: "In Progress" })]),
    "GET /workspaces/1/items/facets": () => facets,
    "GET /workspaces/1/import-jobs": () => [],
    ...handlers,
  });
}

describe("BacklogPage", () => {
  it("lists items with flags", async () => {
    backlogApi({
      "GET /workspaces/1/items": () =>
        page([
          item("PROJ-101", { local_state: "not_relevant", labels: ["camera", "oem-patch"] }),
          item("PROJ-102", { dropped: true, last_import_error: "HTTP 500", diff_count: 0 }),
        ]),
    });
    renderRoute("/workspaces/1");
    expect(await screen.findByRole("heading", { name: "Android 15" })).toBeInTheDocument();
    const first = (await screen.findByRole("link", { name: "PROJ-101" })).closest("tr")!;
    expect(screen.getByRole("link", { name: "PROJ-101" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-101");
    expect(within(first).getByText("Not relevant")).toBeInTheDocument();
    expect(within(first).getByText("camera, oem-patch")).toBeInTheDocument();
    const second = screen.getByRole("link", { name: "PROJ-102" }).closest("tr")!;
    expect(within(second).getByText("dropped")).toBeInTheDocument();
    expect(within(second).getByText("import error")).toHaveAttribute("title", "HTTP 500");
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();
  });

  it("shows empty states", async () => {
    backlogApi({ "GET /workspaces/1/items": () => page([]) });
    renderRoute("/workspaces/1");
    expect(await screen.findByText(/No items yet/)).toBeInTheDocument();
  });

  it("reads filters from the URL into the controls and the API query", async () => {
    const calls = backlogApi({ "GET /workspaces/1/items": () => page([], 0) });
    renderRoute("/workspaces/1?q=camera&label=camera&label=oem-patch&state=closed&has_diff=true&sort=-updated&page=3&junk=1");
    expect(await screen.findByText("No items match the filters.")).toBeInTheDocument();

    const query = lastItemQuery(calls);
    expect(query.get("q")).toBe("camera");
    expect(query.getAll("label")).toEqual(["camera", "oem-patch"]);
    expect(query.getAll("state")).toEqual(["closed"]);
    expect(query.get("has_diff")).toBe("true");
    expect(query.get("sort")).toBe("-updated");
    expect([query.get("offset"), query.get("limit")]).toEqual(["100", "50"]);
    expect(query.has("page") || query.has("junk")).toBe(false);

    expect(screen.getByRole("searchbox", { name: "Search" })).toHaveValue("camera");
    expect(screen.getByText("Labels (2)")).toBeInTheDocument();
    const labels = screen.getByRole("group", { name: "Labels" });
    expect(within(labels).getByRole("checkbox", { name: "camera" })).toBeChecked();
    expect(within(screen.getByRole("group", { name: "State" })).getByRole("checkbox", { name: "Closed" })).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Diff" })).toHaveValue("true");
    expect(screen.getByRole("combobox", { name: "Sort" })).toHaveValue("-updated");
  });

  it("writes filter changes to the URL and resets the page", async () => {
    const calls = backlogApi();
    const { router } = renderRoute("/workspaces/1?page=2");
    await screen.findByRole("link", { name: "PROJ-101" });
    const url = () => new URLSearchParams(router.state.location.search);

    await userEvent.click(screen.getByText("Jira status", { selector: "summary" }));
    await userEvent.click(within(screen.getByRole("group", { name: "Jira status" })).getByRole("checkbox", { name: "Open" }));
    await userEvent.click(within(screen.getByRole("group", { name: "Jira status" })).getByRole("checkbox", { name: "In Progress" }));
    expect(url().getAll("status")).toEqual(["Open", "In Progress"]);
    expect(url().has("page")).toBe(false);

    await userEvent.click(within(screen.getByRole("group", { name: "Jira status" })).getByRole("checkbox", { name: "Open" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Dropped" }), "Not dropped");
    expect(url().getAll("status")).toEqual(["In Progress"]);
    expect(url().get("dropped")).toBe("false");

    await userEvent.type(screen.getByRole("searchbox", { name: "Search" }), "CameraService.cpp");
    await waitFor(() => expect(url().get("q")).toBe("CameraService.cpp"));
    await waitFor(() => expect(lastItemQuery(calls).get("q")).toBe("CameraService.cpp"));
    expect(lastItemQuery(calls).getAll("status")).toEqual(["In Progress"]);
    expect(lastItemQuery(calls).get("dropped")).toBe("false");

    // The URL alone restores the same view.
    router.navigate(`/workspaces/1?${url()}`);
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(router.state.location.search).toBe("");
    await waitFor(() => expect(screen.getByRole("searchbox", { name: "Search" })).toHaveValue(""));
  });

  it("pages through results", async () => {
    const calls = backlogApi({
      "GET /workspaces/1/items": () => page([item("PROJ-101")], 120, 0),
    });
    const { router } = renderRoute("/workspaces/1");
    await screen.findByRole("link", { name: "PROJ-101" });
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(router.state.location.search).toBe("?page=2");
    await waitFor(() => expect(lastItemQuery(calls).get("offset")).toBe("50"));
  });

  it("changes the state of selected items", async () => {
    let state: Item["local_state"] = "new";
    const calls = backlogApi({
      "GET /workspaces/1/items": () =>
        page([item("PROJ-101", { local_state: state }), item("PROJ-102", { local_state: state }), item("PROJ-103")]),
      "POST /workspaces/1/items/state": () => {
        state = "not_relevant";
        return { changed: ["PROJ-101", "PROJ-102"] };
      },
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select PROJ-101" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select PROJ-102" }));
    const bulk = screen.getByRole("group", { name: "Bulk actions" });
    expect(within(bulk).getByText("2 selected")).toBeInTheDocument();
    expect(within(bulk).getByRole("button", { name: "Apply" })).toBeDisabled();
    await userEvent.selectOptions(within(bulk).getByRole("combobox", { name: "New state" }), "Not relevant");
    await userEvent.click(within(bulk).getByRole("button", { name: "Apply" }));

    expect(calls.find((c) => c.method === "POST")).toEqual({
      method: "POST",
      path: "/workspaces/1/items/state",
      body: { keys: ["PROJ-101", "PROJ-102"], state: "not_relevant" },
    });
    const row = (key: string) => screen.getByRole("link", { name: key }).closest("tr")!;
    await waitFor(() => expect(within(row("PROJ-101")).getByText("Not relevant")).toBeInTheDocument());
    expect(within(row("PROJ-102")).getByText("Not relevant")).toBeInTheDocument();
    expect(within(row("PROJ-103")).getByText("New")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Bulk actions" })).not.toBeInTheDocument();
  });

  it("selects all rows on the page", async () => {
    backlogApi();
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select all on this page" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select PROJ-102" })).toBeChecked();
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all on this page" }));
    expect(screen.queryByRole("group", { name: "Bulk actions" })).not.toBeInTheDocument();
  });

  it("reimports selected items and shows the job", async () => {
    const calls = backlogApi({
      "POST /workspaces/1/import": () => ({
        status: 202,
        body: job({ scope: "keys", issue_keys: ["PROJ-102"], total: 1 }),
      }),
      "GET /workspaces/1/import-jobs": () => [job({ scope: "keys", issue_keys: ["PROJ-102"], total: 1 })],
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select PROJ-102" }));
    await userEvent.click(screen.getByRole("button", { name: "Reimport selected" }));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ keys: ["PROJ-102"] });
    expect(await screen.findByText(/Reimporting… 0 \/ 1/)).toBeInTheDocument();
  });

  it("shows a bulk action error", async () => {
    backlogApi({
      "POST /workspaces/1/import": () => ({ status: 409, body: { detail: "An import is already running for this workspace" } }),
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select PROJ-101" }));
    await userEvent.click(screen.getByRole("button", { name: "Reimport selected" }));
    expect(await screen.findByText("An import is already running for this workspace")).toBeInTheDocument();
  });
});

describe("ImportPanel", () => {
  it("starts an import, polls progress and refreshes the backlog when done", async () => {
    let polls = 0;
    let jobs: ImportJob[] = [];
    const calls = backlogApi({
      "GET /workspaces/1/import-jobs": () => {
        if (jobs.length) polls += 1;
        if (polls >= 2) {
          jobs = [
            job({
              status: "done",
              done: 25,
              total: 25,
              finished_at: "2026-10-25T20:01:00Z",
              errors: [{ key: "PROJ-110", message: "Jira returned HTTP 500" }],
            }),
          ];
        }
        return jobs;
      },
      "POST /workspaces/1/import": () => {
        jobs = [job({ done: 3, total: 25 })];
        return { status: 202, body: jobs[0] };
      },
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("button", { name: "Import from Jira" }));
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ path: "/workspaces/1/import", body: {} });
    expect(await screen.findByText(/Importing… 3 \/ 25/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import from Jira" })).toBeDisabled();

    const before = itemQueries(calls).length;
    expect(await screen.findByText(/Import done: 25 tickets/, {}, { timeout: POLL_MS * 4 })).toBeInTheDocument();
    await waitFor(() => expect(itemQueries(calls).length).toBeGreaterThan(before));
    expect(screen.getByRole("button", { name: "Import from Jira" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Cancel import" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByText("1 ticket failed"));
    expect(screen.getByRole("link", { name: "PROJ-110" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-110");
    expect(screen.getByText(/Jira returned HTTP 500/)).toBeInTheDocument();
  });

  it("cancels a running import", async () => {
    const calls = backlogApi({
      "GET /workspaces/1/import-jobs": () => [job({ done: 5, total: 25 })],
      "POST /import-jobs/7/cancel": () => ({ status: 202, body: job({ done: 5, total: 25, cancel_requested: true }) }),
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("button", { name: "Cancel import" }));
    expect(calls.some((c) => c.method === "POST" && c.path === "/import-jobs/7/cancel")).toBe(true);
    expect(await screen.findByText(/\(canceling\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel import" })).toBeDisabled();
  });

  it("shows why the last import failed", async () => {
    backlogApi({
      "GET /workspaces/1/import-jobs": () => [job({ status: "failed", error: "Jira rejected the token", finished_at: "2026-10-25T20:01:00Z" })],
    });
    renderRoute("/workspaces/1");
    expect(await screen.findByText(/Import failed: Jira rejected the token/)).toBeInTheDocument();
  });

  it("shows a start error", async () => {
    backlogApi({
      "POST /workspaces/1/import": () => ({ status: 422, body: { detail: "Set the Jira base URL and PAT in the settings first" } }),
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("button", { name: "Import from Jira" }));
    expect(await screen.findByText("Set the Jira base URL and PAT in the settings first")).toBeInTheDocument();
  });
});

function clusterOf(id: number, name: string): Cluster {
  return { id, name, description: "", created_at: "2026-10-25T19:00:00Z", updated_at: "2026-10-25T19:00:00Z", members: [] };
}

describe("Backlog clusters", () => {
  it("shows the cluster column and filters by cluster", async () => {
    const calls = backlogApi({
      "GET /workspaces/1/items": () => page([item("PROJ-101", { cluster: { id: 5, name: "Camera" } }), item("PROJ-102")]),
      "GET /workspaces/1/clusters": () => [clusterOf(5, "Camera")],
    });
    const { router } = renderRoute("/workspaces/1");
    expect(await screen.findByRole("link", { name: "Camera" })).toHaveAttribute("href", "/workspaces/1/clusters/5");
    expect(screen.getByRole("link", { name: "Clusters" })).toHaveAttribute("href", "/workspaces/1/clusters");

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Cluster" }), "Not in a cluster");
    expect(new URLSearchParams(router.state.location.search).get("cluster")).toBe("none");
    await waitFor(() => expect(lastItemQuery(calls).get("cluster")).toBe("none"));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Cluster" }), "Camera");
    await waitFor(() => expect(lastItemQuery(calls).get("cluster")).toBe("5"));
  });

  it("creates a cluster from the selection", async () => {
    const calls = backlogApi({
      "POST /workspaces/1/clusters": () => ({ status: 201, body: clusterOf(9, "PROJ-101 Summary of PROJ-101") }),
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select PROJ-101" }));
    expect(screen.getByRole("button", { name: "New cluster from selected" })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "Select PROJ-102" }));
    await userEvent.click(screen.getByRole("button", { name: "New cluster from selected" }));

    expect(calls.find((c) => c.method === "POST")).toEqual({
      method: "POST",
      path: "/workspaces/1/clusters",
      body: { keys: ["PROJ-101", "PROJ-102"], confirm_dissolve: false },
    });
    const status = await screen.findByRole("status");
    expect(within(status).getByRole("link", { name: "PROJ-101 Summary of PROJ-101" })).toHaveAttribute("href", "/workspaces/1/clusters/9");
    expect(screen.queryByRole("group", { name: "Bulk actions" })).not.toBeInTheDocument();
  });

  it("adds the selection to an existing cluster, confirming a dissolve", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = backlogApi({
      "GET /workspaces/1/clusters": () => [clusterOf(5, "Camera"), clusterOf(6, "Power")],
      "POST /workspaces/1/clusters/6/members": (body) =>
        (body as { confirm_dissolve: boolean }).confirm_dissolve
          ? clusterOf(6, "Power")
          : { status: 409, body: { detail: "This would leave fewer than 2 items in 'Camera' and dissolve it" } },
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select PROJ-102" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Target cluster" }), "Power");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved to cluster Power");
    expect(calls.filter((c) => c.method === "POST").map((c) => c.body)).toEqual([
      { keys: ["PROJ-102"], confirm_dissolve: false },
      { keys: ["PROJ-102"], confirm_dissolve: true },
    ]);
  });
});
