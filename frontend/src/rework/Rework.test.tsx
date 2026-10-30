import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { BuildJob, Item, ItemPatch, PlanItem, PrescanJob, ReworkAttempt, ReworkJob, ReworkPlan, ReworkRequest } from "../api";
import { cameraV2, diffHandlers, itemDetail, PATCH } from "../item/fixtures";
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

const CORE = "system/core";
const CPP = "services/camera/libcameraservice/CameraService.cpp";

function item(key: string, overrides: Partial<Item> = {}): Item {
  return {
    id: Number(key.split("-")[1]),
    key,
    summary: `Summary of ${key}`,
    status: "Open",
    labels: [],
    components: [],
    fix_versions: [],
    jira_updated: "2026-09-25T16:00:00Z",
    local_state: "new",
    dropped: false,
    last_import_error: null,
    diff_count: 1,
    diff_subjects: [],
    target_projects: [],
    cluster: null,
    triage_label: "conflicts",
    triage_reason: null,
    stale: [],
    dependencies: [],
    build_status: null,
    build_reason: null,
    rework_status: null,
    rework_reason: null,
    ...overrides,
  };
}

function planItem(key: string, overrides: Partial<PlanItem> = {}): PlanItem {
  return {
    key,
    summary: `Summary of ${key}`,
    local_state: "new",
    triage_label: "conflicts",
    selected: false,
    dependent: false,
    included: true,
    rework: true,
    base: true,
    has_patch: false,
    accepted: false,
    stale: [],
    warning: null,
    reason: null,
    ...overrides,
  };
}

/** PROJ-112 needs PROJ-111 (new, added by default) and PROJ-110 (closed, left out unless ticked). */
function plan(request: ReworkRequest): ReworkPlan {
  const include = request.include ?? [];
  const exclude = request.exclude ?? [];
  const proj111 = exclude.includes("PROJ-111")
    ? planItem("PROJ-111", { included: false, rework: false, reason: "left out: reworked without it" })
    : planItem("PROJ-111");
  const proj110 = include.includes("PROJ-110")
    ? planItem("PROJ-110", { local_state: "closed", warning: "closed" })
    : planItem("PROJ-110", { local_state: "closed", warning: "closed", included: false, rework: false, reason: "left out: reworked without it" });
  const prerequisites = [proj110, proj111];
  const units = [...prerequisites.filter((p) => p.rework).map((p) => [p.key]), ["PROJ-112"]];
  const itemCap = request.item_cap ?? 5;
  return {
    units,
    items: [...prerequisites, planItem("PROJ-112", { selected: true })],
    prerequisites,
    rungs: ["resolve", "port", "reimpl"],
    estimate: { units: units.length, items: units.length, likely_calls: 1, max_calls: itemCap * units.length },
    caps: { item_cap: itemCap, job_cap: request.job_cap ?? 100, build_rounds: request.build_rounds ?? 2 },
  };
}

function reworkJob(overrides: Partial<ReworkJob> = {}): ReworkJob {
  return {
    id: 3,
    workspace_id: 1,
    issue_keys: ["PROJ-112"],
    options: {},
    units: [["PROJ-111"], ["PROJ-112"]],
    skipped: [],
    status: "running",
    done: 0,
    total: 2,
    calls: 0,
    errors: [],
    error: null,
    cancel_requested: false,
    started_at: "2026-10-30T10:00:00Z",
    finished_at: null,
    unit_keys: ["PROJ-111"],
    ...overrides,
  };
}

function buildJob(overrides: Partial<BuildJob> = {}): BuildJob {
  return {
    id: 4,
    workspace_id: 1,
    scope: "keys",
    issue_keys: ["PROJ-101"],
    cluster_id: null,
    skipped: [],
    status: "done",
    done: 1,
    total: 1,
    errors: [],
    error: null,
    cancel_requested: false,
    started_at: "2026-10-25T20:00:00Z",
    finished_at: "2026-10-25T20:05:00Z",
    unit_keys: ["PROJ-101"],
    log_tail: null,
    ...overrides,
  };
}

function prescanJob(overrides: Partial<PrescanJob> = {}): PrescanJob {
  return {
    id: 5,
    workspace_id: 1,
    scope: "all",
    issue_keys: [],
    status: "done",
    stage: null,
    done: 3,
    total: 3,
    errors: [],
    error: null,
    cancel_requested: false,
    started_at: "2026-10-25T20:00:00Z",
    finished_at: "2026-10-25T20:01:00Z",
    ...overrides,
  };
}

function backlogApi(handlers: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({
    "GET /workspaces": () => [workspace],
    "GET /workspaces/1/items": () => ({ items: [item("PROJ-111"), item("PROJ-112")], total: 2, offset: 0, limit: 50 }),
    "GET /workspaces/1/items/facets": () => ({ statuses: [], labels: [], components: [], fix_versions: [] }),
    "GET /workspaces/1/import-jobs": () => [],
    "GET /workspaces/1/prescan-jobs": () => [],
    "GET /workspaces/1/build-jobs": () => [],
    "GET /workspaces/1/rework-jobs": () => [],
    "POST /workspaces/1/rework/plan": (body) => plan(body as ReworkRequest),
    ...handlers,
  });
}

function planCalls(calls: ApiCall[]): ReworkRequest[] {
  return calls.filter((c) => c.method === "POST" && c.path === "/workspaces/1/rework/plan").map((c) => c.body as ReworkRequest);
}

async function openDialog(keys: string[]) {
  for (const key of keys) await userEvent.click(await screen.findByRole("checkbox", { name: `Select ${key}` }));
  await userEvent.click(screen.getByRole("button", { name: "Rework…" }));
  return screen.getByRole("dialog", { name: "Rework" });
}

describe("Rework dialog", () => {
  it("lists missing prerequisites, added by default, a closed one only when ticked, and replans", async () => {
    const calls = backlogApi();
    renderRoute("/workspaces/1");
    const dialog = await openDialog(["PROJ-112"]);
    expect(planCalls(calls)[0]).toEqual({ keys: ["PROJ-112"], include_dependents: false, include: [], exclude: [], together: false });

    const prerequisites = await within(dialog).findByRole("region", { name: "Prerequisites" });
    expect(within(prerequisites).getByRole("checkbox", { name: "Include PROJ-111" })).toBeChecked();
    const closed = within(prerequisites).getByRole("checkbox", { name: "Include PROJ-110" });
    expect(closed).not.toBeChecked();
    expect(within(prerequisites).getByText("closed")).toHaveClass("tag-warn");
    const units = within(dialog).getByRole("region", { name: "Units" });
    expect(within(units).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["PROJ-111", "PROJ-112"]);

    await userEvent.click(closed);
    await waitFor(() => expect(planCalls(calls).at(-1)?.include).toEqual(["PROJ-110"]));
    await waitFor(() => expect(within(units).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["PROJ-110", "PROJ-111", "PROJ-112"]));

    await userEvent.click(within(prerequisites).getByRole("checkbox", { name: "Include PROJ-111" }));
    await waitFor(() => expect(planCalls(calls).at(-1)?.exclude).toEqual(["PROJ-111"]));
    expect(await within(prerequisites).findByText("left out: reworked without it")).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("checkbox", { name: "Also rework the items that need these" }));
    await waitFor(() => expect(planCalls(calls).at(-1)?.include_dependents).toBe(true));
  });

  it("preselects the plan's rungs, sends the chosen ones and the caps, and closes once started", async () => {
    let started: ReworkJob[] = [];
    const calls = backlogApi({
      "POST /workspaces/1/rework": () => {
        started = [reworkJob()];
        return { status: 202, body: started[0] };
      },
      "GET /workspaces/1/rework-jobs": () => started,
    });
    renderRoute("/workspaces/1");
    const dialog = await openDialog(["PROJ-111", "PROJ-112"]);

    const rungs = within(dialog).getByRole("group", { name: "Copilot rungs" });
    await waitFor(() => expect(within(rungs).getByRole("checkbox", { name: "Re-implement" })).toBeChecked());
    expect(within(rungs).getByRole("checkbox", { name: "Resolve conflicts" })).toBeChecked();
    await userEvent.click(within(rungs).getByRole("checkbox", { name: "Re-implement" }));

    const caps = within(dialog).getByRole("group", { name: "Caps" });
    const itemCap = within(caps).getByRole("spinbutton", { name: "Copilot calls per item" });
    expect(itemCap).toHaveAttribute("placeholder", "5");
    expect(within(dialog).getByRole("status", { name: "Estimate" })).toHaveTextContent("Copilot calls likely 1, at most 10");
    await userEvent.type(itemCap, "3");
    await waitFor(() => expect(planCalls(calls).at(-1)?.item_cap).toBe(3));
    await waitFor(() => expect(within(dialog).getByRole("status", { name: "Estimate" })).toHaveTextContent("at most 6"));

    // Out of range: no start.
    const rounds = within(caps).getByRole("spinbutton", { name: "Build-fix rounds" });
    await userEvent.type(rounds, "11");
    expect(within(dialog).getByRole("button", { name: "Start rework" })).toBeDisabled();
    await userEvent.clear(rounds);

    await userEvent.click(within(dialog).getByRole("checkbox", { name: "Rework the selected items together, as one unit" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Start rework" })).toBeEnabled());
    await userEvent.click(within(dialog).getByRole("button", { name: "Start rework" }));
    expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/rework")?.body).toEqual({
      keys: ["PROJ-111", "PROJ-112"],
      include_dependents: false,
      include: [],
      exclude: [],
      together: true,
      item_cap: 3,
      rungs: ["resolve", "port"],
    });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Rework" })).not.toBeInTheDocument());
  });

  it("shows why the plan or the start was refused", async () => {
    backlogApi({
      "POST /workspaces/1/rework": () => ({ status: 409, body: { detail: "A build is running" } }),
    });
    renderRoute("/workspaces/1");
    const dialog = await openDialog(["PROJ-112"]);
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Start rework" })).toBeEnabled());
    await userEvent.click(within(dialog).getByRole("button", { name: "Start rework" }));
    expect(await within(dialog).findByText("A build is running")).toBeInTheDocument();
  });
});

describe("Backlog rework", () => {
  it("shows the rework column, counts and filter", async () => {
    const calls = backlogApi({
      "GET /workspaces/1/items": () => ({
        items: [item("PROJ-111", { rework_status: "proposed" }), item("PROJ-112", { rework_status: "rework_failed", rework_reason: "build failed: x.cpp" })],
        total: 2,
        offset: 0,
        limit: 50,
      }),
      "GET /workspaces/1/rework-summary": () => ({ statuses: { rework_failed: 1, proposed: 1, rejected: 0, accepted: 0 }, unreworked: 5 }),
    });
    const { router } = renderRoute("/workspaces/1");
    expect(await screen.findByText("Rework failed", { selector: ".tag" })).toHaveAttribute("title", "build failed: x.cpp");
    const bar = await screen.findByRole("group", { name: "Rework counts" });
    expect(within(bar).getAllByRole("button").map((b) => b.textContent)).toEqual(["1 rework failed", "1 proposed"]);
    await userEvent.click(within(bar).getByRole("button", { name: "1 proposed" }));
    expect(new URLSearchParams(router.state.location.search).getAll("rework")).toEqual(["proposed"]);
    await waitFor(() => expect(calls.some((c) => c.path.startsWith("/workspaces/1/items?") && c.path.includes("rework=proposed"))).toBe(true));
  });

  it("shows the rework job in the job popup and cancels it", async () => {
    let job = reworkJob({ calls: 2 });
    const calls = backlogApi({
      "GET /workspaces/1/rework-jobs": () => [job],
      "POST /rework-jobs/3/cancel": () => {
        job = { ...job, cancel_requested: true };
        return { status: 202, body: job };
      },
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("button", { name: /^Job status/ }));
    const panel = within(screen.getByRole("dialog", { name: "Job status" })).getByRole("region", { name: "Rework" });
    expect(within(panel).getByRole("status")).toHaveTextContent("Reworking PROJ-111 (0 / 2, 2 Copilot calls)");
    await userEvent.click(within(panel).getByRole("button", { name: "Cancel rework" }));
    expect(calls.some((c) => c.method === "POST" && c.path === "/rework-jobs/3/cancel")).toBe(true);
    await waitFor(() => expect(within(panel).getByRole("status")).toHaveTextContent("(canceling)"));
  });

  it.each([
    ["build", { "GET /workspaces/1/build-jobs": () => [buildJob({ rework_candidates: ["PROJ-112"] })] }, "Build"],
    ["pre-scan", { "GET /workspaces/1/prescan-jobs": () => [prescanJob({ rework_candidates: ["PROJ-112"] })] }, "Pre-scan"],
  ])("offers a rework of a finished %s job's candidates", async (_, handlers, region) => {
    const calls = backlogApi(handlers);
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("button", { name: /^Job status/ }));
    const panel = within(screen.getByRole("dialog", { name: "Job status" })).getByRole("region", { name: region });
    expect(within(panel).getByText("1 item worth a rework: PROJ-112")).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "Rework these?" }));
    expect(screen.getByRole("dialog", { name: "Rework" })).toBeInTheDocument();
    await waitFor(() => expect(planCalls(calls)[0]?.keys).toEqual(["PROJ-112"]));
  });

  it("offers nothing while running or without candidates", async () => {
    backlogApi({
      "GET /workspaces/1/build-jobs": () => [buildJob({ rework_candidates: [] })],
      "GET /workspaces/1/prescan-jobs": () => [prescanJob({ status: "running", rework_candidates: ["PROJ-112"] })],
    });
    renderRoute("/workspaces/1");
    await userEvent.click(await screen.findByRole("button", { name: /^Job status/ }));
    expect(within(screen.getByRole("dialog", { name: "Job status" })).queryByRole("button", { name: "Rework these?" })).toBeNull();
  });
});

function reworkPatch(overrides: Partial<ItemPatch> = {}): ItemPatch {
  return {
    id: 21,
    attachment_id: null,
    jira_id: null,
    filename: null,
    project: "frameworks/av",
    source: "ai",
    level: "resolve",
    base_head: "c".repeat(40),
    base_patch_ids: [],
    bases: [],
    review: "proposed",
    review_note: null,
    findings: [],
    attempt_id: 8,
    stale: [],
    author: null,
    message: null,
    current: true,
    created_at: "2026-10-30T10:05:00Z",
    ...overrides,
  };
}

function attempt(overrides: Partial<ReworkAttempt> = {}): ReworkAttempt {
  return {
    id: 8,
    job_id: 3,
    keys: ["PROJ-101"],
    heads: { "frameworks/av": "c".repeat(40) },
    base_patch_ids: [],
    rungs: ["stack", "resolve"],
    files: [{ project: "frameworks/av", path: CPP, status: "resolved", rung: "resolve", error: null, notes: ["build fix 1"] }],
    calls: [
      {
        project: "frameworks/av",
        rung: "resolve",
        round: 0,
        build_round: 0,
        model: "gpt-5-mini",
        status: "ok",
        prompt_path: "/data/rework-prompts/8/call-1.txt",
        parts: {},
        answer: "CameraService.cpp: kept the null check, adapted to the new mLock.",
        error: "",
        usage: null,
        duration: 12.3,
      },
      {
        project: "frameworks/av",
        rung: "resolve",
        round: 1,
        build_round: 1,
        model: "gpt-5-mini",
        status: "ok",
        prompt_path: "/data/rework-prompts/8/call-2.txt",
        parts: {},
        answer: "CameraService.cpp: added the missing include.",
        error: "",
        usage: null,
        duration: 8,
      },
    ],
    build_runs: [
      { id: 30, status: "build_failed", summary: "CameraService.cpp:3: error: unknown type", log_url: "/api/workspaces/1/build-runs/30/log" },
      { id: 31, status: "build_successful", summary: null, log_url: null },
    ],
    notes: [],
    patch_ids: [21],
    status: "patched",
    reason: null,
    started_at: "2026-10-30T10:00:00Z",
    finished_at: "2026-10-30T10:05:00Z",
    ...overrides,
  };
}

function migrationApi(patches: () => ItemPatch[], handlers: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({
    "GET /workspaces/1/items/PROJ-101": () => itemDetail(),
    "GET /workspaces/1/items/PROJ-101/patches": patches,
    "GET /workspaces/1/patches/21": () => ({ ...cameraV2, format: "plain", commits: [] }),
    "GET /workspaces/1/items/PROJ-101/builds": () => [],
    "GET /workspaces/1/items/PROJ-101/rework-attempts": () => [attempt()],
    "GET /workspaces/1/build-jobs": () => [],
    "GET /workspaces/1/rework-jobs": () => [],
    "GET /workspaces/1/build-runs/30/log": () => "mmm frameworks/av\nerror: unknown type",
    ...diffHandlers,
    ...handlers,
  });
}

const MIGRATION = "/workspaces/1/items/PROJ-101?tab=migration";

async function review() {
  return screen.findByRole("region", { name: "Review" });
}

describe("Migration tab review", () => {
  it("shows a rework patch's review, findings and Copilot's explanation, and accepts it", async () => {
    let patch = reworkPatch({
      findings: [
        { kind: "intent", severity: "warning", path: CPP, message: "added line missing: if (client == nullptr)" },
        { kind: "check", severity: "error", path: null, message: "conflict marker left" },
      ],
    });
    const calls = migrationApi(() => [patch], {
      "POST /workspaces/1/patches/21/accept": () => {
        patch = { ...patch, review: "accepted" };
        return { id: 21, review: "accepted", review_note: null, rework_status: "accepted" };
      },
    });
    renderRoute(MIGRATION);
    const box = await review();
    expect(within(box).getByText("proposed")).toBeInTheDocument();
    expect(within(box).getByLabelText("Base chain")).toHaveTextContent(`Made on HEAD ${"c".repeat(12)}`);
    const findings = within(box).getByRole("region", { name: "Findings" });
    expect(within(findings).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      `warning intent ${CPP}: added line missing: if (client == nullptr)`,
      "error check conflict marker left",
    ]);
    const explanation = await within(box).findByRole("region", { name: "Copilot's explanation" });
    expect(explanation).toHaveTextContent("kept the null check, adapted to the new mLock.");
    expect(explanation).toHaveTextContent("added the missing include.");

    const rows = within(screen.getByRole("table", { name: "Patches" })).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Conflicts resolved");
    expect(rows[0]).toHaveTextContent("proposed");

    await userEvent.click(within(box).getByRole("button", { name: "Accept" }));
    expect(calls.some((c) => c.method === "POST" && c.path === "/workspaces/1/patches/21/accept")).toBe(true);
    await waitFor(() => expect(within(box).getByText("accepted")).toBeInTheDocument());
    expect(within(box).queryByRole("button", { name: "Accept" })).toBeNull();
  });

  it("rejects with a note", async () => {
    let patch = reworkPatch({ review: "accepted" });
    const calls = migrationApi(() => [patch], {
      "POST /workspaces/1/patches/21/reject": (body) => {
        patch = { ...patch, review: "rejected", review_note: (body as { note: string }).note };
        return { id: 21, review: "rejected", review_note: patch.review_note, rework_status: "rejected" };
      },
    });
    renderRoute(MIGRATION);
    const box = await review();
    expect(within(box).queryByRole("button", { name: "Accept" })).toBeNull();
    await userEvent.type(within(box).getByRole("textbox", { name: "Reject note" }), "keep the old lock order");
    await userEvent.click(within(box).getByRole("button", { name: "Reject" }));
    expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/patches/21/reject")?.body).toEqual({ note: "keep the old lock order" });
    expect(await within(box).findByText("Note: keep the old lock order")).toBeInTheDocument();
    expect(within(box).queryByRole("button", { name: "Reject" })).toBeNull();
  });

  it("blocks accept while a base patch is not accepted, and shows a refused accept", async () => {
    const base = { id: 20, key: "PROJ-100", project: CORE, review: "proposed" as const };
    let patches = [reworkPatch({ source: "original", level: "stacked", base_patch_ids: [20], bases: [base] })];
    migrationApi(() => patches, {
      "POST /workspaces/1/patches/21/accept": () => ({ status: 409, body: { detail: "Base patch of PROJ-100 (system/core) is rejected: accept it first" } }),
    });
    const { unmount } = renderRoute(MIGRATION);
    let box = await review();
    expect(within(box).getByLabelText("Base chain")).toHaveTextContent(`+ PROJ-100 (${CORE}) proposed`);
    expect(within(box).getByRole("link", { name: "PROJ-100" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-100?tab=migration");
    expect(within(box).getByText(`Accept the base patch first: PROJ-100 (${CORE}, proposed)`)).toBeInTheDocument();
    expect(within(box).getByRole("button", { name: "Accept" })).toBeDisabled();
    expect(within(screen.getByRole("table", { name: "Patches" })).getAllByRole("row")[1]).toHaveTextContent("+ PROJ-100");
    unmount();

    // The server has the last word: the base changed since the list was fetched.
    patches = [reworkPatch({ base_patch_ids: [20], bases: [{ ...base, review: "accepted" }] })];
    renderRoute(MIGRATION);
    box = await review();
    await userEvent.click(within(box).getByRole("button", { name: "Accept" }));
    expect(await within(box).findByText("Base patch of PROJ-100 (system/core) is rejected: accept it first")).toBeInTheDocument();
  });

  it("says why a patch is stale", async () => {
    migrationApi(() => [reworkPatch({ stale: ["base PROJ-100 changed", "HEAD moved"] })]);
    renderRoute(MIGRATION);
    const box = await review();
    expect(within(box).getByLabelText("Stale reason")).toHaveTextContent("Out of date: base PROJ-100 changed; HEAD moved.");
    expect(within(screen.getByRole("table", { name: "Patches" })).getByText("stale")).toHaveAttribute("title", "Out of date: base PROJ-100 changed; HEAD moved");
  });

  it("shows no review for a pre-scan patch", async () => {
    migrationApi(() => [reworkPatch({ source: "original", level: "plain", attempt_id: null, filename: PATCH, review: "accepted" })]);
    renderRoute(MIGRATION);
    await screen.findByRole("region", { name: "Patch" });
    expect(screen.queryByRole("region", { name: "Review" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Next to the Jira diff" })).toBeNull();
  });

  it("shows the Jira diff next to the proposed patch per file", async () => {
    migrationApi(() => [reworkPatch()]);
    renderRoute(MIGRATION);
    await review();
    await userEvent.click(screen.getByRole("checkbox", { name: "Next to the Jira diff" }));
    const row = await screen.findByRole("group", { name: `Compare ${CPP}` });
    // The current Jira diff's file on the left, the patch's on the right.
    expect(within(row).getAllByRole("region", { name: CPP })).toHaveLength(2);
  });

  it("lists the attempts with rungs, calls, build rounds and their logs", async () => {
    migrationApi(() => [], {
      "GET /workspaces/1/items/PROJ-101/rework-attempts": () => [
        attempt({ id: 9, status: "failed", reason: "build failed: CameraService.cpp:3 (after 2 build-fix rounds)", keys: ["PROJ-100", "PROJ-101"], patch_ids: [] }),
        attempt(),
      ],
    });
    renderRoute(MIGRATION);
    const list = await screen.findByRole("list", { name: "Rework attempts" });
    const failed = within(list).getByRole("listitem", { name: "Attempt 9" });
    expect(failed).toHaveTextContent("Failed");
    expect(failed).toHaveTextContent("rungs: stack → resolve");
    expect(failed).toHaveTextContent("2 Copilot calls");
    expect(failed).toHaveTextContent("1 build-fix round");
    expect(failed).toHaveTextContent("with PROJ-100");
    expect(within(failed).getByText("build failed: CameraService.cpp:3 (after 2 build-fix rounds)")).toHaveClass("error");

    const patched = within(list).getByRole("listitem", { name: "Attempt 8" });
    await userEvent.click(within(patched).getByText("Details"));
    const callRows = within(within(patched).getByRole("table", { name: "Copilot calls" })).getAllByRole("row").slice(1);
    expect(callRows[1]).toHaveTextContent("fix 1 (build fix 1)");
    expect(within(within(patched).getByRole("table", { name: "Files" })).getAllByRole("row")[1]).toHaveTextContent("resolvedresolvebuild fix 1");
    await userEvent.click(within(patched).getByRole("button", { name: "Full log" }));
    expect(await within(patched).findByText(/error: unknown type$/)).toBeInTheDocument();
  });

  it("opens the Rework dialog for the item", async () => {
    const calls = migrationApi(() => [], { "POST /workspaces/1/rework/plan": (body) => plan(body as ReworkRequest) });
    renderRoute(MIGRATION);
    await userEvent.click(await screen.findByRole("button", { name: "Rework…" }));
    expect(screen.getByRole("dialog", { name: "Rework" })).toBeInTheDocument();
    await waitFor(() => expect(planCalls(calls)[0]?.keys).toEqual(["PROJ-101"]));
  });

  it("describes rework events in the history", async () => {
    mockApi({
      "GET /workspaces/1/items/PROJ-101": () => ({
        ...itemDetail(),
        history: [
          { id: 1, type: "rework_started", payload: { attempt_id: 8, job_id: 3 }, created_at: "2026-10-30T10:00:00Z" },
          { id: 2, type: "rework_finished", payload: { attempt_id: 8, status: "failed", reason: "budget reached" }, created_at: "2026-10-30T10:05:00Z" },
          { id: 3, type: "patch_rejected", payload: { patch_id: 21, project: CORE, note: "wrong lock" }, created_at: "2026-10-30T10:06:00Z" },
        ],
      }),
      "GET /workspaces/1/items/PROJ-101/prescan": () => ({ scanned: false, label: null, label_reason: null, stale: [], diffs: [], depends_on: [], needed_by: [] }),
      "GET /workspaces/1/prescan-jobs": () => [],
    });
    renderRoute("/workspaces/1/items/PROJ-101");
    const history = await screen.findByRole("region", { name: "History" });
    expect(within(history).getByText("Rework started (attempt 8)")).toBeInTheDocument();
    expect(within(history).getByText("Rework: failed (attempt 8)")).toBeInTheDocument();
    expect(within(history).getByText("budget reached")).toBeInTheDocument();
    expect(within(history).getByText(`Patch rejected (${CORE})`)).toBeInTheDocument();
    expect(within(history).getByText("wrong lock")).toBeInTheDocument();
  });
});
