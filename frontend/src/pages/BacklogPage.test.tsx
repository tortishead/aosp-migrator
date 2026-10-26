import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Cluster, Facets, ImportJob, Item, ItemPage, PrescanJob, PrescanSummary, TriageLabel } from "../api";
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
    diff_subjects: [],
    cluster: null,
    triage_label: null,
    triage_reason: null,
    apply: null,
    stale: [],
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
    rate_limit_wait: null,
    started_at: "2026-10-25T20:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

function prescanJob(overrides: Partial<PrescanJob> = {}): PrescanJob {
  return {
    id: 3,
    workspace_id: 1,
    scope: "all",
    issue_keys: [],
    status: "running",
    stage: "scan",
    done: 0,
    total: 25,
    errors: [],
    error: null,
    cancel_requested: false,
    started_at: "2026-10-25T20:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

function summary(labels: Partial<Record<TriageLabel, number>> = {}, unlabeled = 0, stale = 0): PrescanSummary {
  const zeros = { error: 0, unmapped: 0, unknown: 0, conflicts: 0, partial: 0, "3way": 0, applies: 0, likely_present: 0, present: 0 };
  return { labels: { ...zeros, ...labels }, unlabeled, stale };
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
    "GET /workspaces/1/prescan-jobs": () => [],
    "GET /workspaces/1/prescan-summary": () => summary(),
    ...handlers,
  });
}

describe("BacklogPage", () => {
  it("lists items with flags", async () => {
    backlogApi({
      "GET /workspaces/1/items": () =>
        page([
          item("PROJ-101", {
            local_state: "not_relevant",
            labels: ["camera", "oem-patch"],
            diff_subjects: ["[PATCH] camera: fix null deref on resume"],
          }),
          item("PROJ-102", { dropped: true, last_import_error: "HTTP 500", diff_count: 0 }),
        ]),
    });
    renderRoute("/workspaces/1");
    expect(await screen.findByRole("heading", { name: "Android 15" })).toBeInTheDocument();
    const first = (await screen.findByRole("link", { name: "PROJ-101" })).closest("tr")!;
    expect(screen.getByRole("link", { name: "PROJ-101" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-101");
    expect(within(first).getByText("Not relevant")).toBeInTheDocument();
    expect(within(first).getByText("camera, oem-patch")).toBeInTheDocument();
    expect(within(first).getByText("[PATCH] camera: fix null deref on resume")).toBeInTheDocument();
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

  it("shows a running import waiting out Jira's rate limit", async () => {
    backlogApi({ "GET /workspaces/1/import-jobs": () => [job({ done: 40, total: 120, rate_limit_wait: 12 })] });
    renderRoute("/workspaces/1");
    expect(await screen.findByText(/Importing… 40 \/ 120 \(Jira rate limit, resuming in 12s\)/)).toBeInTheDocument();
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

  it("runs a pre-scan of the backlog and shows its progress and item errors", async () => {
    let polls = 0;
    const calls = backlogApi({
      "POST /workspaces/1/prescan": () => ({ status: 202, body: prescanJob() }),
      "GET /workspaces/1/prescan-jobs": () => {
        polls += 1;
        if (polls === 1) return [];
        if (polls === 2) return [prescanJob({ done: 10 })];
        return [
          prescanJob({
            status: "done",
            stage: null,
            done: 25,
            finished_at: "2026-10-25T20:01:00Z",
            errors: [{ key: "PROJ-105", message: "0001.patch: downloaded file is missing; reimport the ticket" }],
          }),
        ];
      },
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderRoute("/workspaces/1");
      await userEvent.click(await screen.findByRole("button", { name: "Pre-scan backlog" }));
      expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/prescan")?.body).toEqual({});
      expect(await screen.findByText(/Pre-scanning… 0 \/ 25/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Pre-scan backlog" })).toBeDisabled();
      await vi.advanceTimersByTimeAsync(POLL_MS);
      expect(await screen.findByText(/Pre-scanning… 10 \/ 25/)).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(POLL_MS);
      expect(await screen.findByText(/Pre-scan done: 25 items/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
    await userEvent.click(screen.getByText("1 item failed to pre-scan"));
    expect(screen.getByRole("link", { name: "PROJ-105" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-105");
  });

  it("cancels a running pre-scan and shows why one failed", async () => {
    const calls = backlogApi({
      "GET /workspaces/1/prescan-jobs": () => [prescanJob({ done: 5 })],
      "POST /prescan-jobs/3/cancel": () => ({ status: 202, body: prescanJob({ done: 5, cancel_requested: true }) }),
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("button", { name: "Cancel pre-scan" }));
    expect(calls.some((c) => c.method === "POST" && c.path === "/prescan-jobs/3/cancel")).toBe(true);
    expect(await screen.findByText(/\(canceling\)/)).toBeInTheDocument();
  });

  it("shows a failed pre-scan", async () => {
    backlogApi({
      "GET /workspaces/1/prescan-jobs": () => [
        prescanJob({ status: "failed", error: "/src/aosp15/.repo/project.list not found; run `repo sync` in the target tree" }),
      ],
    });
    renderRoute("/workspaces/1");
    expect(await screen.findByText(/Pre-scan failed: .*run `repo sync`/)).toBeInTheDocument();
  });

  it("pre-scans the selected items", async () => {
    const calls = backlogApi({
      "POST /workspaces/1/prescan": () => ({ status: 202, body: prescanJob({ scope: "keys", issue_keys: ["PROJ-102"], total: 1 }) }),
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select PROJ-102" }));
    await userEvent.click(screen.getByRole("button", { name: "Pre-scan selected" }));
    expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/prescan")?.body).toEqual({ keys: ["PROJ-102"] });
    expect(await screen.findByText(/Pre-scanning… 0 \/ 1/)).toBeInTheDocument();
  });
});

describe("Backlog pre-scan overview", () => {
  it("shows the label with its reason and a stale marker", async () => {
    backlogApi({
      "GET /workspaces/1/items": () =>
        page([
          item("PROJ-101", { triage_label: "present", triage_reason: "Change-Id I1234 in abc1234" }),
          item("PROJ-102", { triage_label: "conflicts", triage_reason: "0001.patch: Foo.java", stale: ["frameworks/base HEAD moved"] }),
          item("PROJ-103", { diff_count: 0 }),
          item("PROJ-104", { triage_label: "partial", triage_reason: "frameworks/base: Foo.java already in", apply: "3way" }),
          item("PROJ-105", { triage_label: "applies", apply: "applies" }),
        ]),
    });
    renderRoute("/workspaces/1");
    const first = (await screen.findByRole("link", { name: "PROJ-101" })).closest("tr")!;
    expect(within(first).getByText("Present")).toHaveAttribute("title", "Change-Id I1234 in abc1234");
    expect(within(first).queryByText("stale")).not.toBeInTheDocument();
    const second = screen.getByRole("link", { name: "PROJ-102" }).closest("tr")!;
    expect(within(second).getByText("Conflicts")).toHaveClass("tag-error");
    expect(within(second).getByText("stale")).toHaveAttribute("title", "Out of date: frameworks/base HEAD moved");
    const third = screen.getByRole("link", { name: "PROJ-103" }).closest("tr")!;
    expect(third.querySelectorAll(".tag")).toHaveLength(0);
    // The apply result shows next to a label that does not already say it.
    const fourth = screen.getByRole("link", { name: "PROJ-104" }).closest("tr")!;
    expect([...fourth.querySelectorAll(".tag")].map((t) => t.textContent)).toEqual(["Partially present", "3-way merge"]);
    const fifth = screen.getByRole("link", { name: "PROJ-105" }).closest("tr")!;
    expect([...fifth.querySelectorAll(".tag")].map((t) => t.textContent)).toEqual(["Applies"]);
  });

  it("counts items per label and filters by a click", async () => {
    const calls = backlogApi({ "GET /workspaces/1/prescan-summary": () => summary({ present: 12, partial: 3, applies: 30 }, 4, 2) });
    const { router } = renderRoute("/workspaces/1");
    const bar = await screen.findByRole("group", { name: "Pre-scan counts" });
    expect(within(bar).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "3 partially present",
      "30 applies",
      "12 present",
      "4 not scanned",
      "2 stale",
    ]);

    await userEvent.click(within(bar).getByRole("button", { name: "12 present" }));
    expect(new URLSearchParams(router.state.location.search).getAll("triage")).toEqual(["present"]);
    await waitFor(() => expect(lastItemQuery(calls).getAll("triage")).toEqual(["present"]));
    expect(within(bar).getByRole("button", { name: "12 present" })).toHaveAttribute("aria-pressed", "true");

    // Another label replaces it, the same one again clears it.
    await userEvent.click(within(bar).getByRole("button", { name: "4 not scanned" }));
    await waitFor(() => expect(lastItemQuery(calls).getAll("triage")).toEqual(["none"]));
    await userEvent.click(within(bar).getByRole("button", { name: "4 not scanned" }));
    await waitFor(() => expect(lastItemQuery(calls).has("triage")).toBe(false));

    await userEvent.click(within(bar).getByRole("button", { name: "2 stale" }));
    await waitFor(() => expect(lastItemQuery(calls).get("stale")).toBe("true"));
    await userEvent.click(within(bar).getByRole("button", { name: "2 stale" }));
    await waitFor(() => expect(lastItemQuery(calls).has("stale")).toBe(false));
  });

  it("hides the count bar until something is scanned", async () => {
    backlogApi({ "GET /workspaces/1/prescan-summary": () => summary({}, 25) });
    renderRoute("/workspaces/1");
    await screen.findByRole("link", { name: "PROJ-101" });
    expect(screen.queryByRole("group", { name: "Pre-scan counts" })).not.toBeInTheDocument();
  });

  it("filters by label and staleness", async () => {
    const calls = backlogApi();
    renderRoute("/workspaces/1");
    await screen.findByRole("link", { name: "PROJ-101" });
    await userEvent.click(screen.getByText("Pre-scan", { selector: "summary" }));
    const labels = screen.getByRole("group", { name: "Pre-scan" });
    await userEvent.click(within(labels).getByLabelText("Conflicts"));
    await userEvent.click(within(labels).getByLabelText("Not scanned"));
    await waitFor(() => expect(lastItemQuery(calls).getAll("triage")).toEqual(["conflicts", "none"]));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Stale" }), "Not stale");
    await waitFor(() => expect(lastItemQuery(calls).get("stale")).toBe("false"));
  });

  it("refreshes labels and counts while a pre-scan runs", async () => {
    let polls = 0;
    const calls = backlogApi({
      "GET /workspaces/1/prescan-jobs": () => {
        polls += 1;
        return [polls === 1 ? prescanJob({ done: 1 }) : prescanJob({ status: "done", stage: null, done: 25 })];
      },
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderRoute("/workspaces/1");
      await screen.findByText(/Pre-scanning… 1 \/ 25/);
      const before = calls.filter((c) => c.path === "/workspaces/1/prescan-summary").length;
      await vi.advanceTimersByTimeAsync(POLL_MS);
      await screen.findByText(/Pre-scan done/);
      await waitFor(() => expect(calls.filter((c) => c.path === "/workspaces/1/prescan-summary").length).toBeGreaterThan(before));
    } finally {
      vi.useRealTimers();
    }
  });
});
