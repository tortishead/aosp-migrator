import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BuildJob, BuildSummary, Cluster, Facets, ImportJob, Item, ItemPage, PrescanJob, PrescanSummary, TriageLabel } from "../api";
import { POLL_MS } from "../backlog/ImportPanel";
import { mockApi, renderRoute, type ApiCall } from "../test-utils";

const workspace = {
  id: 1,
  name: "Android 15",
  jql: "project = OEM",
  target_tree: "/src/aosp15",
  target_tree_type: "repo",
  repo_root: null,
  source_tree: null,
  source_tree_type: null,
  link_types: [],
  envsetup_path: null,
  lunch_target: null,
  build_timeout: 7200,
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
    target_projects: [],
    cluster: null,
    triage_label: null,
    triage_reason: null,
    stale: [],
    dependencies: [],
    build_status: null,
    build_reason: null,
    rework_status: null,
    rework_reason: null,
    browse_url: null,
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

function buildJob(overrides: Partial<BuildJob> = {}): BuildJob {
  return {
    id: 4,
    workspace_id: 1,
    scope: "keys",
    issue_keys: ["PROJ-101", "PROJ-102"],
    cluster_id: null,
    skipped: [],
    status: "running",
    done: 0,
    total: 2,
    errors: [],
    error: null,
    cancel_requested: false,
    started_at: "2026-10-25T20:00:00Z",
    finished_at: null,
    unit_keys: null,
    log_tail: null,
    ...overrides,
  };
}

function buildSummary(statuses: Partial<BuildSummary["statuses"]> = {}, unbuilt = 0): BuildSummary {
  return { statuses: { apply_failed: 0, build_failed: 0, build_successful: 0, ...statuses }, unbuilt };
}

function summary(labels: Partial<Record<TriageLabel, number>> = {}, unlabeled = 0, stale = 0, dependencies = 0): PrescanSummary {
  const zeros = { error: 0, unmapped: 0, unknown: 0, conflicts: 0, partial: 0, "3way": 0, applies: 0, likely_present: 0, present: 0 };
  return { labels: { ...zeros, ...labels }, unlabeled, stale, dependencies };
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

/** The import, pre-scan and build panels live in the "Job status" popup. */
async function openJobs() {
  await userEvent.click(await screen.findByRole("button", { name: /^Job status/ }));
  return screen.getByRole("dialog", { name: "Job status" });
}

function backlogApi(handlers: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({
    "GET /workspaces": () => [workspace],
    "GET /workspaces/1/items": () => page([item("PROJ-101"), item("PROJ-102", { status: "In Progress" })]),
    "GET /workspaces/1/items/facets": () => facets,
    "GET /workspaces/1/import-jobs": () => [],
    "GET /workspaces/1/prescan-jobs": () => [],
    "GET /workspaces/1/prescan-summary": () => summary(),
    "GET /workspaces/1/build-jobs": () => [],
    "GET /workspaces/1/build-summary": () => buildSummary(),
    ...handlers,
  });
}

describe("BacklogPage", () => {
  it("lists items with flags", async () => {
    backlogApi({
      "GET /workspaces/1/items": () =>
        page([
          item("PROJ-101", {
            browse_url: "https://jira.example.com/browse/PROJ-101",
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
    // The key opens the ticket in Jira; without a Jira URL it falls back to the item page.
    expect(screen.getByRole("link", { name: "PROJ-101" })).toHaveAttribute("href", "https://jira.example.com/browse/PROJ-101");
    expect(screen.getByRole("link", { name: "PROJ-101" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "PROJ-102" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-102");
    expect(within(first).getByText("Not relevant")).toBeInTheDocument();
    expect(within(first).getByText("camera, oem-patch")).toBeInTheDocument();
    expect(within(first).getByText("Open")).toHaveClass("badge");
    expect(screen.queryByRole("columnheader", { name: "Components" })).toBeNull();
    expect(screen.queryByRole("columnheader", { name: "Fix version" })).toBeNull();
    expect(within(first).getByText("[PATCH] camera: fix null deref on resume")).toBeInTheDocument();
    const second = screen.getByRole("link", { name: "PROJ-102" }).closest("tr")!;
    expect(within(second).getByText("dropped")).toBeInTheDocument();
    expect(within(second).getByText("import error")).toHaveAttribute("title", "HTTP 500");
    expect(screen.getByRole("link", { name: "Summary of PROJ-102" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-102");
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();
  });

  it("opens the ticket from its summary and shows its target projects", async () => {
    backlogApi({
      "GET /workspaces/1/items": () => page([item("PROJ-101", { target_projects: ["frameworks/av", "system/core"] })]),
    });
    renderRoute("/workspaces/1");
    const summary = await screen.findByRole("link", { name: "Summary of PROJ-101" });
    expect(summary).toHaveAttribute("href", "/workspaces/1/items/PROJ-101");
    expect(screen.getByRole("columnheader", { name: "Target project" })).toBeInTheDocument();
    const row = summary.closest("tr")!;
    expect(within(row).getByText("frameworks/av")).toBeInTheDocument();
    expect(within(row).getByText("system/core")).toBeInTheDocument();
  });

  it("groups by target project", async () => {
    const calls = backlogApi({
      "GET /workspaces/1/items": () =>
        page([
          item("PROJ-101", { target_projects: ["frameworks/av"] }),
          item("PROJ-102", { target_projects: ["frameworks/av"] }),
          item("PROJ-103", { target_projects: ["frameworks/av", "system/core"] }),
          item("PROJ-104"),
        ]),
    });
    renderRoute("/workspaces/1?page=2");
    await screen.findByRole("link", { name: "PROJ-101" });
    expect(screen.queryByRole("rowheader")).toBeNull();
    expect(lastItemQuery(calls).has("group")).toBe(false);

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Group" }), "Group by target project");
    await waitFor(() => expect(lastItemQuery(calls).get("group")).toBe("project"));
    expect(lastItemQuery(calls).get("offset")).toBe("0");
    const headings = await screen.findAllByRole("rowheader");
    expect(headings.map((h) => within(h).getByRole("button").textContent)).toEqual(["▾ frameworks/av", "▾ frameworks/av, system/core", "▾ No target project"]);
    expect(headings.map((h) => h.querySelector(".group-count")?.textContent)).toEqual(["2", "1", "1"]);

    const av = screen.getByRole("button", { name: "frameworks/av" });
    await userEvent.click(av);
    expect(av).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "PROJ-101" })).toBeNull();
    expect(screen.queryByRole("link", { name: "PROJ-102" })).toBeNull();
    expect(screen.getByRole("link", { name: "PROJ-103" })).toBeInTheDocument();
    await userEvent.click(av);
    expect(av).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "PROJ-101" })).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Group" }), "No grouping");
    await waitFor(() => expect(lastItemQuery(calls).has("group")).toBe(false));
    expect(screen.queryByRole("rowheader")).toBeNull();
  });

  it("groups the rows under a heading per group", async () => {
    backlogApi({
      "GET /workspaces/1/items": () =>
        page([
          item("PROJ-104", { triage_label: "conflicts" }),
          item("PROJ-101", { triage_label: "applies" }),
          item("PROJ-102", { triage_label: "applies" }),
          item("PROJ-103"),
        ]),
    });
    renderRoute("/workspaces/1?group=triage");
    await screen.findByRole("link", { name: "PROJ-101" });
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows.map((r) => (r.querySelector("th") ? `# ${r.querySelector("button")!.textContent}` : r.querySelector("a")?.textContent))).toEqual([
      "# ▾ Conflicts",
      "PROJ-104",
      "# ▾ Applies",
      "PROJ-101",
      "PROJ-102",
      "# ▾ Not scanned",
      "PROJ-103",
    ]);
  });

  it("shows the pre-scan and build counts on one line", async () => {
    backlogApi({
      "GET /workspaces/1/prescan-summary": () => summary({ applies: 3 }),
      "GET /workspaces/1/build-summary": () => buildSummary({ build_successful: 1 }),
    });
    renderRoute("/workspaces/1");
    const triage = await screen.findByRole("group", { name: "Pre-scan counts" });
    const build = await screen.findByRole("group", { name: "Build counts" });
    expect(triage.parentElement).toBe(build.parentElement);
  });

  it("shows empty states", async () => {
    backlogApi({ "GET /workspaces/1/items": () => page([]) });
    renderRoute("/workspaces/1");
    expect(await screen.findByText(/No items yet/)).toBeInTheDocument();
  });

  it("reads filters from the URL into the controls and the API query", async () => {
    const calls = backlogApi({ "GET /workspaces/1/items": () => page([], 0) });
    renderRoute("/workspaces/1?q=camera&label=camera&label=oem-patch&has_error=true&sort=-updated&group=triage&page=3&junk=1&state=closed");
    expect(await screen.findByText("No items match the filters.")).toBeInTheDocument();

    const query = lastItemQuery(calls);
    expect(query.get("q")).toBe("camera");
    expect(query.getAll("label")).toEqual(["camera", "oem-patch"]);
    expect(query.get("has_error")).toBe("true");
    expect(query.get("sort")).toBe("-updated");
    expect(query.get("group")).toBe("triage");
    expect([query.get("offset"), query.get("limit")]).toEqual(["100", "50"]);
    expect(query.has("page") || query.has("junk") || query.has("state")).toBe(false);

    expect(screen.getByRole("searchbox", { name: "Search" })).toHaveValue("camera");
    expect(screen.getByText("Labels (2)")).toBeInTheDocument();
    const labels = screen.getByRole("group", { name: "Labels" });
    expect(within(labels).getByRole("checkbox", { name: "camera" })).toBeChecked();
    for (const name of ["State", "Components", "Fix version"]) expect(screen.queryByRole("group", { name })).toBeNull();
    // Import errors and staleness are reached from the row tags and the count bar.
    for (const name of ["Diff", "Dropped", "Import error", "Stale"]) expect(screen.queryByRole("combobox", { name })).toBeNull();
    const view = screen.getByRole("group", { name: "View" });
    expect(within(view).getByRole("combobox", { name: "Sort" })).toHaveValue("-updated");
    expect(within(view).getByRole("combobox", { name: "Group" })).toHaveValue("triage");
  });

  it("closes an open filter dropdown on a click outside it", async () => {
    backlogApi();
    renderRoute("/workspaces/1");
    await screen.findByRole("link", { name: "PROJ-101" });

    const summary = screen.getByText("Jira status", { selector: "summary" });
    await userEvent.click(summary);
    const details = summary.closest("details")!;
    expect(details).toHaveAttribute("open");

    await userEvent.click(within(screen.getByRole("group", { name: "Jira status" })).getByRole("checkbox", { name: "Open" }));
    expect(details).toHaveAttribute("open");

    await userEvent.click(document.body);
    expect(details).not.toHaveAttribute("open");
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
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Group" }), "Group by pre-scan");
    expect(url().getAll("status")).toEqual(["In Progress"]);
    expect(url().get("group")).toBe("triage");

    await userEvent.type(screen.getByRole("searchbox", { name: "Search" }), "CameraService.cpp");
    await waitFor(() => expect(url().get("q")).toBe("CameraService.cpp"));
    await waitFor(() => expect(lastItemQuery(calls).get("q")).toBe("CameraService.cpp"));
    expect(lastItemQuery(calls).getAll("status")).toEqual(["In Progress"]);
    expect(lastItemQuery(calls).get("group")).toBe("triage");

    // The URL alone restores the same view. Clearing the filters keeps the grouping.
    router.navigate(`/workspaces/1?${url()}`);
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(router.state.location.search).toBe("?group=triage");
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
    await waitFor(() => expect(screen.getByRole("button", { name: "Import from Jira" })).toHaveAttribute("title", "Reimporting… 0 / 1"));
    expect(within(await openJobs()).getByText(/Reimporting… 0 \/ 1/)).toBeInTheDocument();
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
    // The status line is the button's tooltip; the popup has the whole job.
    await waitFor(() => expect(screen.getByRole("button", { name: "Import from Jira" })).toHaveAttribute("title", "Importing… 3 / 25"));
    expect(screen.getByRole("button", { name: "Import from Jira" })).toBeDisabled();
    const popup = await openJobs();
    expect(within(popup).getByText(/Importing… 3 \/ 25/)).toBeInTheDocument();

    const before = itemQueries(calls).length;
    const done = await within(popup).findByText(/Import done: 25 tickets/, {}, { timeout: POLL_MS * 4 });
    // The finish time sits in the tooltip, not the line.
    expect(done).toHaveTextContent(/^Import done: 25 tickets$/);
    expect(done).toHaveAttribute("title", `Finished ${new Date("2026-10-25T20:01:00Z").toLocaleString()}`);
    await waitFor(() => expect(itemQueries(calls).length).toBeGreaterThan(before));
    expect(screen.getByRole("button", { name: "Import from Jira" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Cancel import" })).not.toBeInTheDocument();

    expect(screen.getByRole("button", { name: /^Job status/ })).toHaveAccessibleName("Job status with errors");
    await userEvent.click(within(popup).getByText("1 ticket failed"));
    expect(screen.getByRole("link", { name: "PROJ-110" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-110");
    expect(screen.getByText(/Jira returned HTTP 500/)).toBeInTheDocument();
  });

  it("cancels a running import", async () => {
    const calls = backlogApi({
      "GET /workspaces/1/import-jobs": () => [job({ done: 5, total: 25 })],
      "POST /import-jobs/7/cancel": () => ({ status: 202, body: job({ done: 5, total: 25, cancel_requested: true }) }),
    });
    renderRoute("/workspaces/1");
    expect(await screen.findByRole("button", { name: "Job status running" })).toBeInTheDocument();
    await openJobs();
    await userEvent.click(await screen.findByRole("button", { name: "Cancel import" }));
    expect(calls.some((c) => c.method === "POST" && c.path === "/import-jobs/7/cancel")).toBe(true);
    expect(await screen.findByText(/\(canceling\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel import" })).toBeDisabled();
  });

  it("shows a running import waiting out Jira's rate limit", async () => {
    backlogApi({ "GET /workspaces/1/import-jobs": () => [job({ done: 40, total: 120, rate_limit_wait: 12 })] });
    renderRoute("/workspaces/1");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Import from Jira" })).toHaveAttribute(
        "title",
        "Importing… 40 / 120 (Jira rate limit, resuming in 12s)",
      ),
    );
  });

  it("shows why the last import failed", async () => {
    backlogApi({
      "GET /workspaces/1/import-jobs": () => [job({ status: "failed", error: "Jira rejected the token", finished_at: "2026-10-25T20:01:00Z" })],
    });
    renderRoute("/workspaces/1");
    const jobs = await openJobs();
    expect(await within(jobs).findByText(/Import failed: Jira rejected the token/)).toBeInTheDocument();
    expect(within(jobs).getByText("No pre-scan yet.")).toBeInTheDocument();
    expect(within(jobs).getByText("No build yet.")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
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
      const button = screen.getByRole("button", { name: "Pre-scan backlog" });
      await waitFor(() => expect(button).toHaveAttribute("title", "Pre-scanning… 0 / 25"));
      expect(button).toBeDisabled();
      await vi.advanceTimersByTimeAsync(POLL_MS);
      await waitFor(() => expect(button).toHaveAttribute("title", "Pre-scanning… 10 / 25"));
      await vi.advanceTimersByTimeAsync(POLL_MS);
      await waitFor(() => expect(button).toHaveAttribute("title", "Pre-scan done: 25 items"));
    } finally {
      vi.useRealTimers();
    }
    await openJobs();
    expect(await screen.findByText(/Pre-scan done: 25 items/)).toBeInTheDocument();
    await userEvent.click(screen.getByText("1 item failed to pre-scan"));
    expect(screen.getByRole("link", { name: "PROJ-105" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-105");
  });

  it("cancels a running pre-scan and shows why one failed", async () => {
    const calls = backlogApi({
      "GET /workspaces/1/prescan-jobs": () => [prescanJob({ done: 5 })],
      "POST /prescan-jobs/3/cancel": () => ({ status: 202, body: prescanJob({ done: 5, cancel_requested: true }) }),
    });
    renderRoute("/workspaces/1");
    await openJobs();
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
    await openJobs();
    expect(await screen.findByText(/Pre-scan failed: .*run `repo sync`/)).toBeInTheDocument();
  });

  it("pre-scans the selected items", async () => {
    let jobs: PrescanJob[] = [];
    const calls = backlogApi({
      "POST /workspaces/1/prescan": () => {
        jobs = [prescanJob({ scope: "keys", issue_keys: ["PROJ-102"], total: 1 })];
        return { status: 202, body: jobs[0] };
      },
      "GET /workspaces/1/prescan-jobs": () => jobs,
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select PROJ-102" }));
    await userEvent.click(screen.getByRole("button", { name: "Pre-scan selected" }));
    expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/prescan")?.body).toEqual({ keys: ["PROJ-102"] });
    await openJobs();
    expect(await screen.findByText(/Pre-scanning… 0 \/ 1/)).toBeInTheDocument();
  });

  it("stacks the selected items in the order dragged into", async () => {
    let jobs: PrescanJob[] = [];
    const calls = backlogApi({
      "GET /workspaces/1/items": () =>
        page([item("PROJ-10"), item("PROJ-9", { local_state: "closed" }), item("PROJ-11", { dropped: true }), item("PROJ-12")]),
      "POST /workspaces/1/prescan": () => {
        jobs = [prescanJob({ scope: "stacked", issue_keys: ["PROJ-10", "PROJ-9", "PROJ-11"], total: 3 })];
        return { status: 202, body: jobs[0] };
      },
      "GET /workspaces/1/prescan-jobs": () => jobs,
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select PROJ-11" }));
    expect(screen.getByRole("button", { name: "Stacked pre-scan…" })).toBeDisabled();
    for (const key of ["PROJ-10", "PROJ-9"]) await userEvent.click(screen.getByRole("checkbox", { name: `Select ${key}` }));
    await userEvent.click(screen.getByRole("button", { name: "Stacked pre-scan…" }));

    const dialog = screen.getByRole("dialog", { name: "Stacked pre-scan" });
    const rows = () => within(dialog).getAllByRole("listitem");
    // By key, PROJ-9 before PROJ-10; closed and dropped items are only warned about.
    expect(rows().map((r) => r.querySelector("strong")?.textContent)).toEqual(["PROJ-9", "PROJ-10", "PROJ-11"]);
    expect(within(rows()[0]).getByText("closed")).toBeInTheDocument();
    expect(within(rows()[2]).getByText("dropped")).toBeInTheDocument();

    // Drag PROJ-9 down onto PROJ-10.
    fireEvent.dragStart(rows()[0], { dataTransfer: { effectAllowed: "", setData: vi.fn() } });
    fireEvent.dragEnter(rows()[1]);
    fireEvent.dragEnd(rows()[1]);
    expect(rows().map((r) => r.querySelector("strong")?.textContent)).toEqual(["PROJ-10", "PROJ-9", "PROJ-11"]);

    await userEvent.click(within(dialog).getByRole("button", { name: "Start stacked pre-scan" }));
    expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/prescan")?.body).toEqual({
      keys: ["PROJ-10", "PROJ-9", "PROJ-11"],
      stacked: true,
    });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Stacked pre-scan" })).not.toBeInTheDocument());
    await openJobs();
    expect(await screen.findByText(/Stacking… 0 \/ 3/)).toBeInTheDocument();
  });
});

describe("Backlog pre-scan overview", () => {
  it("shows the label with its reason and a stale marker", async () => {
    backlogApi({
      "GET /workspaces/1/items": () =>
        page([
          item("PROJ-101", { triage_label: "present", triage_reason: "Change-Id I1234 in abc1234" }),
          item("PROJ-102", {
            triage_label: "conflicts",
            triage_reason: "0001.patch: Foo.java",
            stale: ["frameworks/base HEAD moved"],
            dependencies: ["needs PROJ-101 (Foo.java)", "icon.png: base in no diff"],
          }),
          item("PROJ-103", { diff_count: 0 }),
        ]),
    });
    renderRoute("/workspaces/1");
    const first = (await screen.findByRole("link", { name: "PROJ-101" })).closest("tr")!;
    expect(within(first).getByText("Present")).toHaveAttribute("title", "Change-Id I1234 in abc1234");
    expect(within(first).queryByText("stale")).not.toBeInTheDocument();
    expect(within(first).queryByText("dependency")).not.toBeInTheDocument();
    const second = screen.getByRole("link", { name: "PROJ-102" }).closest("tr")!;
    expect(within(second).getByText("Conflicts")).toHaveClass("tag-error");
    expect(within(second).getByText("stale")).toHaveAttribute("title", "Out of date: frameworks/base HEAD moved");
    expect(within(second).getByText("dependency")).toHaveAttribute("title", "needs PROJ-101 (Foo.java); icon.png: base in no diff");
    const third = screen.getByRole("link", { name: "PROJ-103" }).closest("tr")!;
    expect(third.querySelectorAll(".tag")).toHaveLength(0);
  });

  it("counts items per label and filters by a click", async () => {
    const calls = backlogApi({ "GET /workspaces/1/prescan-summary": () => summary({ present: 12, partial: 3, applies: 30 }, 4, 2, 5) });
    const { router } = renderRoute("/workspaces/1");
    const bar = await screen.findByRole("group", { name: "Pre-scan counts" });
    expect(within(bar).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "3 partially present",
      "30 applies",
      "12 present",
      "4 not scanned",
      "2 stale",
      "5 dependencies",
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

    await userEvent.click(within(bar).getByRole("button", { name: "5 dependencies" }));
    await waitFor(() => expect(lastItemQuery(calls).get("depends")).toBe("true"));
    await userEvent.click(within(bar).getByRole("button", { name: "5 dependencies" }));
    await waitFor(() => expect(lastItemQuery(calls).has("depends")).toBe(false));
  });

  it("hides the count bar until something is scanned", async () => {
    backlogApi({ "GET /workspaces/1/prescan-summary": () => summary({}, 25) });
    renderRoute("/workspaces/1");
    await screen.findByRole("link", { name: "PROJ-101" });
    expect(screen.queryByRole("group", { name: "Pre-scan counts" })).not.toBeInTheDocument();
  });

  it("filters by label", async () => {
    const calls = backlogApi();
    renderRoute("/workspaces/1");
    await screen.findByRole("link", { name: "PROJ-101" });
    await userEvent.click(screen.getByText("Pre-scan", { selector: "summary" }));
    const labels = screen.getByRole("group", { name: "Pre-scan" });
    await userEvent.click(within(labels).getByLabelText("Conflicts"));
    await userEvent.click(within(labels).getByLabelText("Not scanned"));
    await waitFor(() => expect(lastItemQuery(calls).getAll("triage")).toEqual(["conflicts", "none"]));
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
      const button = await screen.findByRole("button", { name: "Pre-scan backlog" });
      await waitFor(() => expect(button).toHaveAttribute("title", "Pre-scanning… 1 / 25"));
      const before = calls.filter((c) => c.path === "/workspaces/1/prescan-summary").length;
      await vi.advanceTimersByTimeAsync(POLL_MS);
      await waitFor(() => expect(button).toHaveAttribute("title", "Pre-scan done: 25 items"));
      await waitFor(() => expect(calls.filter((c) => c.path === "/workspaces/1/prescan-summary").length).toBeGreaterThan(before));
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("Backlog build", () => {
  it("builds the selected items stacked, in the order dragged into", async () => {
    let jobs: BuildJob[] = [];
    const calls = backlogApi({
      "POST /workspaces/1/build": () => {
        jobs = [buildJob({ scope: "stacked", issue_keys: ["PROJ-102", "PROJ-101"], total: 1, unit_keys: ["PROJ-102", "PROJ-101"] })];
        return { status: 202, body: jobs[0] };
      },
      "GET /workspaces/1/build-jobs": () => jobs,
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select PROJ-101" }));
    expect(screen.getByRole("button", { name: "Stacked build…" })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "Select PROJ-102" }));
    await userEvent.click(screen.getByRole("button", { name: "Stacked build…" }));

    const dialog = screen.getByRole("dialog", { name: "Stacked build" });
    const rows = () => within(dialog).getAllByRole("listitem");
    expect(rows().map((r) => r.querySelector("strong")?.textContent)).toEqual(["PROJ-101", "PROJ-102"]);
    fireEvent.dragStart(rows()[1], { dataTransfer: { effectAllowed: "", setData: vi.fn() } });
    fireEvent.dragEnter(rows()[0]);
    fireEvent.dragEnd(rows()[0]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Start stacked build" }));
    expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/build")?.body).toEqual({
      keys: ["PROJ-102", "PROJ-101"],
      stacked: true,
    });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Stacked build" })).not.toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Job status/ })).toHaveAttribute("title", "Building stack PROJ-102, PROJ-101 (0 / 1)"),
    );
  });

  it("builds the selected items and lists the skipped ones", async () => {
    let jobs: BuildJob[] = [];
    const calls = backlogApi({
      "POST /workspaces/1/build": () => {
        jobs = [buildJob({ issue_keys: ["PROJ-101"], total: 1, skipped: [{ key: "PROJ-102", reason: "pre-scan: conflicts" }] })];
        return { status: 202, body: jobs[0] };
      },
      "GET /workspaces/1/build-jobs": () => jobs,
    });
    renderRoute("/workspaces/1");
    expect(await screen.findByRole("link", { name: "PROJ-101" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Build" })).toBeNull();
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all on this page" }));
    await userEvent.click(screen.getByRole("button", { name: "Build selected" }));
    expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/build")?.body).toEqual({ keys: ["PROJ-101", "PROJ-102"] });
    await waitFor(() => expect(screen.getByRole("button", { name: /^Job status/ })).toHaveAttribute("title", "Building … (0 / 1)"));
    await openJobs();
    const panel = await screen.findByRole("region", { name: "Build" });
    expect(within(panel).getByRole("status")).toHaveTextContent("Building … (0 / 1)");
    await userEvent.click(within(panel).getByText("1 item not built"));
    expect(within(panel).getByRole("link", { name: "PROJ-102" }).closest("li")).toHaveTextContent("PROJ-102: pre-scan: conflicts");
  });

  it("shows the build in progress with its live log, refreshes the backlog as units finish, and cancels", async () => {
    let polls = 0;
    const calls = backlogApi({
      "GET /workspaces/1/build-jobs": () => {
        polls += 1;
        // The header's fetch and the popup's on opening.
        if (polls <= 2) return [buildJob({ unit_keys: ["PROJ-101"], log_tail: "lunch aosp_x86_64-eng\nmmm system/core" })];
        return [buildJob({ done: 1, unit_keys: ["PROJ-102"], log_tail: "mmm packages/apps/Settings" })];
      },
      "POST /build-jobs/4/cancel": () => ({ status: 202, body: buildJob({ done: 1, unit_keys: ["PROJ-102"], cancel_requested: true }) }),
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderRoute("/workspaces/1");
      await openJobs();
      const panel = await screen.findByRole("region", { name: "Build" });
      expect(within(panel).getByRole("status")).toHaveTextContent("Building PROJ-101 (0 / 2)");
      await userEvent.click(within(panel).getByText("Live log"));
      expect(within(panel).getByLabelText("Build log tail")).toHaveTextContent("mmm system/core");
      const before = calls.filter((c) => c.path === "/workspaces/1/build-summary").length;
      await vi.advanceTimersByTimeAsync(POLL_MS);
      expect(await within(panel).findByText(/Building PROJ-102 \(1 \/ 2\)/)).toBeInTheDocument();
      expect(within(panel).getByLabelText("Build log tail")).toHaveTextContent("mmm packages/apps/Settings");
      await waitFor(() => expect(calls.filter((c) => c.path === "/workspaces/1/build-summary").length).toBeGreaterThan(before));
    } finally {
      vi.useRealTimers();
    }
    await userEvent.click(screen.getByRole("button", { name: "Cancel build" }));
    expect(calls.some((c) => c.method === "POST" && c.path === "/build-jobs/4/cancel")).toBe(true);
    expect(await screen.findByText(/\(canceling\)/)).toBeInTheDocument();
  });

  it.each([
    [buildJob({ status: "done", done: 2, finished_at: "2026-10-25T20:05:00Z" }), "Build done: 2 units"],
    [buildJob({ status: "done", total: 0, skipped: [{ key: "PROJ-102", reason: "not pre-scanned" }] }), "Build: nothing eligible"],
    [buildJob({ status: "canceled", done: 1 }), "Build canceled after 1 of 2 units"],
    [buildJob({ status: "failed", error: "envsetup.sh not found: /src/aosp15/build/envsetup.sh" }), "Build failed: envsetup.sh not found: /src/aosp15/build/envsetup.sh"],
  ])("shows a finished build: %#", async (job, text) => {
    backlogApi({ "GET /workspaces/1/build-jobs": () => [job] });
    renderRoute("/workspaces/1");
    await openJobs();
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel build" })).toBeNull();
  });

  it("lists units that failed unexpectedly", async () => {
    backlogApi({ "GET /workspaces/1/build-jobs": () => [buildJob({ status: "done", done: 2, errors: [{ keys: ["PROJ-102"], message: "disk full" }] })] });
    renderRoute("/workspaces/1");
    await openJobs();
    await userEvent.click(await screen.findByText("1 unit failed unexpectedly"));
    expect(screen.getByText("PROJ-102: disk full")).toBeInTheDocument();
  });

  it("shows the build column with the reason", async () => {
    backlogApi({
      "GET /workspaces/1/items": () =>
        page([
          item("PROJ-101", { build_status: "build_successful", build_reason: "" }),
          item("PROJ-102", { build_status: "build_failed", build_reason: "FAILED: out/target/Settings.apk" }),
          item("PROJ-103"),
        ]),
    });
    renderRoute("/workspaces/1");
    expect(await screen.findByRole("columnheader", { name: "Build" })).toBeInTheDocument();
    const first = (await screen.findByRole("link", { name: "PROJ-101" })).closest("tr")!;
    expect(within(first).getByText("Built")).toHaveClass("tag-ok");
    expect(within(first).getByText("Built")).not.toHaveAttribute("title");
    const second = screen.getByRole("link", { name: "PROJ-102" }).closest("tr")!;
    expect(within(second).getByText("Build failed")).toHaveAttribute("title", "FAILED: out/target/Settings.apk");
    expect(within(second).getByText("Build failed")).toHaveClass("tag-error");
    const third = screen.getByRole("link", { name: "PROJ-103" }).closest("tr")!;
    expect(third.querySelectorAll(".tag")).toHaveLength(0);
  });

  it("counts items per build status and filters by a click or the filter", async () => {
    const calls = backlogApi({ "GET /workspaces/1/build-summary": () => buildSummary({ build_failed: 2, build_successful: 5 }, 18) });
    const { router } = renderRoute("/workspaces/1");
    const bar = await screen.findByRole("group", { name: "Build counts" });
    expect(within(bar).getAllByRole("button").map((b) => b.textContent)).toEqual(["2 build failed", "5 built", "18 not built"]);
    await userEvent.click(within(bar).getByRole("button", { name: "2 build failed" }));
    expect(new URLSearchParams(router.state.location.search).getAll("build")).toEqual(["build_failed"]);
    await waitFor(() => expect(lastItemQuery(calls).getAll("build")).toEqual(["build_failed"]));
    await userEvent.click(within(bar).getByRole("button", { name: "2 build failed" }));
    await waitFor(() => expect(lastItemQuery(calls).has("build")).toBe(false));

    await userEvent.click(screen.getByText("Build", { selector: "summary" }));
    const statuses = screen.getByRole("group", { name: "Build" });
    await userEvent.click(within(statuses).getByLabelText("Apply failed"));
    await userEvent.click(within(statuses).getByLabelText("Not built"));
    await waitFor(() => expect(lastItemQuery(calls).getAll("build")).toEqual(["apply_failed", "none"]));
  });

  it("hides the build count bar until something is built", async () => {
    backlogApi({ "GET /workspaces/1/build-summary": () => buildSummary({}, 25) });
    renderRoute("/workspaces/1");
    await screen.findByRole("link", { name: "PROJ-101" });
    expect(screen.queryByRole("group", { name: "Build counts" })).not.toBeInTheDocument();
  });
});
