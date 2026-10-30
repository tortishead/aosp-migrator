import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  type BuildJob,
  type BuildRun,
  type DiffScan,
  type HistoryEvent,
  type ImportJob,
  type ItemDetail,
  type ItemPatch,
  type ItemPrescan,
  type Note,
  type PrescanJob,
  TRIAGE_LABELS,
} from "../api";
import { POLL_MS } from "../backlog/ImportPanel";
import { cameraV1, cameraV2, diffHandlers, itemDetail, PATCH } from "../item/fixtures";
import { mockApi, renderRoute } from "../test-utils";

const URL = "/workspaces/1/items/PROJ-101";

function itemApi(handlers: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({
    "GET /workspaces/1/items/PROJ-101": () => itemDetail(),
    "GET /workspaces/1/items/PROJ-101/prescan": () => ({ scanned: false, label: null, label_reason: null, stale: [], diffs: [], depends_on: [], needed_by: [] }),
    "GET /workspaces/1/prescan-jobs": () => [],
    ...diffHandlers,
    ...handlers,
  });
}

function job(overrides: Partial<ImportJob> = {}): ImportJob {
  return {
    id: 9,
    workspace_id: 1,
    scope: "keys",
    issue_keys: ["PROJ-101"],
    status: "running",
    done: 0,
    total: 1,
    errors: [],
    error: null,
    cancel_requested: false,
    rate_limit_wait: null,
    started_at: "2026-10-25T20:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

describe("ItemPage", () => {
  it("shows the ticket with sanitized description, comments, attachments and links", async () => {
    itemApi();
    renderRoute(URL);
    expect(await screen.findByRole("heading", { name: "PROJ-101 Camera HAL crash on resume" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "PROJ-101" })).toHaveAttribute("href", "https://jira.example.com/browse/PROJ-101");
    expect(screen.getByText("oem-patch, camera")).toBeInTheDocument();

    // The pre-scan verdict leads the overview, above the description.
    const prescan = screen.getByRole("region", { name: "Pre-scan" });
    const description = screen.getByRole("region", { name: "Description" });
    expect(prescan.compareDocumentPosition(description) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(description).getByRole("table")).toBeInTheDocument();
    expect(within(description).getByRole("cell", { name: "Pixel 8" })).toBeInTheDocument();
    expect(description.querySelector("script")).toBeNull();
    expect((window as { pwned?: boolean }).pwned).toBeUndefined();

    const comments = screen.getByRole("region", { name: "Comments" });
    expect(within(comments).getByText("Raj Patel")).toBeInTheDocument();
    expect(within(comments).getByText("AP2A").tagName).toBe("B");

    // Diffs sit on the Patches tab; everything else is listed with size and a Jira link.
    expect(screen.queryByRole("region", { name: "Diffs" })).toBeNull();
    const others = screen.getByRole("region", { name: "Other attachments" });
    expect(within(others).getAllByRole("row").map((r) => r.firstChild!.textContent)).toEqual(["screenshot.png", "logs.zip"]);
    expect(within(others).getByRole("link", { name: "logs.zip" })).toHaveAttribute(
      "href",
      "https://jira.example.com/secure/attachment/14/logs.zip",
    );
    expect(within(others).getByText("3.0 MiB")).toBeInTheDocument();
    expect(within(others).getByText("not downloaded")).toBeInTheDocument();

    const links = screen.getByRole("region", { name: "Linked tickets" });
    expect(within(links).getByText("PROJ-201")).toBeInTheDocument();
    expect(within(links).getByText("relates to")).toBeInTheDocument();
    // Linked-only ticket: no backlog item to open.
    expect(within(links).queryByRole("link", { name: "Open backlog item" })).toBeNull();
  });

  it("shows diffs on the Patches tab, kept in the URL", async () => {
    itemApi();
    const { router } = renderRoute(URL);
    const patches = await screen.findByRole("tab", { name: /^Patches/ });
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(patches);
    expect(router.state.location.search).toBe("?tab=patches");
    expect(patches).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("region", { name: "services/camera/libcameraservice/CameraService.cpp" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Pre-scan" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Description" })).toBeNull();

    await userEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(router.state.location.search).toBe("");
    expect(screen.getByRole("region", { name: "Description" })).toBeInTheDocument();
  });

  it("links to a linked ticket that is a backlog item", async () => {
    const detail = itemDetail();
    detail.links[0].item_id = 2;
    itemApi({ "GET /workspaces/1/items/PROJ-101": () => detail });
    renderRoute(URL);
    const links = await screen.findByRole("region", { name: "Linked tickets" });
    expect(within(links).getByRole("link", { name: "Open backlog item" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-201");
  });

  it("appends notes and shows them in the history", async () => {
    const notes: Note[] = [];
    const history: HistoryEvent[] = [...itemDetail().history];
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101": () => itemDetail({ notes: [...notes], history: [...history] }),
      "POST /workspaces/1/items/PROJ-101/notes": (body) => {
        const { text } = body as { text: string };
        const note = { id: notes.length + 1, text, created_at: "2026-10-25T21:00:00Z" };
        notes.push(note);
        history.push({ id: history.length + 1, type: "note_added", payload: { note_id: note.id, text }, created_at: note.created_at });
        return { status: 201, body: note };
      },
    });
    renderRoute(URL);
    const section = await screen.findByRole("region", { name: "Notes" });
    expect(within(section).getByText("No notes yet.")).toBeInTheDocument();
    const add = within(section).getByRole("button", { name: "Add note" });
    expect(add).toBeDisabled();

    await userEvent.type(within(section).getByRole("textbox", { name: "New note" }), "Already upstream in 15 QPR1.");
    await userEvent.click(add);
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ text: "Already upstream in 15 QPR1." });
    expect(await within(section).findByText("Already upstream in 15 QPR1.")).toBeInTheDocument();
    expect(within(section).getByRole("textbox", { name: "New note" })).toHaveValue("");

    await userEvent.type(within(section).getByRole("textbox", { name: "New note" }), "Second note");
    await userEvent.click(add);
    await within(section).findByText("Second note");
    expect(within(section).getAllByRole("listitem").map((li) => li.querySelector("p")!.textContent)).toEqual([
      "Already upstream in 15 QPR1.",
      "Second note",
    ]);

    // History, newest first.
    const timeline = screen.getByRole("list", { name: "History" });
    const entries = within(timeline).getAllByRole("listitem").filter((li) => li.parentElement === timeline);
    expect(entries.map((li) => li.querySelector("strong")!.textContent)).toEqual(["Note", "Note", "Imported from Jira"]);
    expect(within(entries[0]).getByText("Second note")).toBeInTheDocument();
  });

  it("changes the state", async () => {
    let detail: ItemDetail = itemDetail();
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101": () => detail,
      "POST /workspaces/1/items/state": () => {
        detail = itemDetail({
          local_state: "not_relevant",
          history: [...detail.history, { id: 2, type: "state_changed", payload: { old: "new", new: "not_relevant" }, created_at: "2026-10-25T21:00:00Z" }],
        });
        return { changed: ["PROJ-101"] };
      },
    });
    renderRoute(URL);
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "State" }), "Not relevant");
    expect(calls.find((c) => c.method === "POST")).toEqual({
      method: "POST",
      path: "/workspaces/1/items/state",
      body: { keys: ["PROJ-101"], state: "not_relevant" },
    });
    expect(await screen.findByText("State: New → Not relevant")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "State" })).toHaveValue("not_relevant");
  });

  it("reimports the ticket and reloads it when done", async () => {
    let status: ImportJob["status"] = "running";
    let summary = "Camera HAL crash on resume";
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101": () => {
        const detail = itemDetail();
        detail.issue.summary = summary;
        return detail;
      },
      "POST /workspaces/1/import": () => ({ status: 202, body: job() }),
      "GET /import-jobs/9": () => job({ status, done: status === "done" ? 1 : 0 }),
    });
    renderRoute(URL);
    await userEvent.click(await screen.findByRole("button", { name: "Reimport from Jira" }));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ keys: ["PROJ-101"] });
    expect(await screen.findByText("Reimporting…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reimport from Jira" })).toBeDisabled();

    summary = "Camera HAL crash on resume (v2)";
    status = "done";
    expect(await screen.findByText("Reimported.", {}, { timeout: 3000 })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Camera HAL crash on resume (v2)"));
  });

  it("shows a reimport conflict", async () => {
    itemApi({ "POST /workspaces/1/import": () => ({ status: 409, body: { detail: "An import is already running" } }) });
    renderRoute(URL);
    await userEvent.click(await screen.findByRole("button", { name: "Reimport from Jira" }));
    expect(await screen.findByText("An import is already running")).toBeInTheDocument();
  });

  it("shows flags and a missing item", async () => {
    itemApi({ "GET /workspaces/1/items/PROJ-101": () => itemDetail({ dropped: true, last_import_error: "HTTP 500" }) });
    renderRoute(URL);
    expect(await screen.findByText(/Dropped: no longer matched/)).toBeInTheDocument();
    expect(screen.getByText("Last import failed: HTTP 500")).toBeInTheDocument();

    mockApi({});
    renderRoute("/workspaces/1/items/PROJ-404");
    expect(await screen.findByText("Not mocked")).toBeInTheDocument();
  });
});

describe("ItemPage cluster", () => {
  it("shows the cluster with siblings and removes the item from it", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const detail = itemDetail({
      cluster: {
        id: 5,
        name: "Camera",
        members: [
          { key: "PROJ-103", summary: "Battery", local_state: "closed" },
          { key: "PROJ-101", summary: "Camera HAL crash on resume", local_state: "new" },
        ],
      },
    });
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101": () => detail,
      "DELETE /workspaces/1/clusters/5/members/PROJ-101": () => ({ status: 409, body: { detail: "Would dissolve 'Camera'" } }),
      "DELETE /workspaces/1/clusters/5/members/PROJ-101?confirm_dissolve=true": () => ({ dissolved: true }),
    });
    renderRoute(URL);
    const box = await screen.findByRole("region", { name: "Cluster" });
    expect(within(box).getByRole("link", { name: "Camera" })).toHaveAttribute("href", "/workspaces/1/clusters/5");
    const siblings = within(box).getByRole("list", { name: "Cluster items" });
    expect(within(siblings).getByRole("link", { name: "PROJ-103" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-103");
    expect(within(siblings).getByText("closed")).toBeInTheDocument();
    expect(within(siblings).queryByRole("link", { name: "PROJ-101" })).toBeNull();

    await userEvent.click(within(box).getByRole("button", { name: "Remove from cluster" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(2));
  });

  it("adds a linked backlog item to a new cluster", async () => {
    const detail = itemDetail();
    detail.links[0].item_id = 2;
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101": () => detail,
      "POST /workspaces/1/clusters": () => ({ status: 201, body: { id: 9, name: "PROJ-101 Camera" } }),
    });
    renderRoute(URL);
    expect(await screen.findByText(/Not in a cluster/)).toBeInTheDocument();
    const links = screen.getByRole("region", { name: "Linked tickets" });
    await userEvent.click(within(links).getByRole("button", { name: "Add to my cluster" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "POST")?.body).toEqual({ keys: ["PROJ-101", "PROJ-201"], confirm_dissolve: false }),
    );
  });

  it("adds a linked backlog item to the item's cluster, or says it is already in it", async () => {
    const detail = itemDetail({ cluster: { id: 5, name: "Camera", members: [] } });
    detail.links[0].item_id = 2;
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101": () => detail,
      "POST /workspaces/1/clusters/5/members": () => ({ id: 5, name: "Camera" }),
    });
    const { unmount } = renderRoute(URL);
    const links = await screen.findByRole("region", { name: "Linked tickets" });
    await userEvent.click(within(links).getByRole("button", { name: "Add to my cluster" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ keys: ["PROJ-201"], confirm_dissolve: false }));
    unmount();

    detail.links[0].cluster = { id: 5, name: "Camera" };
    renderRoute(URL);
    const again = await screen.findByRole("region", { name: "Linked tickets" });
    expect(within(again).getByText("In this cluster")).toBeInTheDocument();
    expect(within(again).getByText("cluster: Camera")).toBeInTheDocument();
  });
});

function unmappedDiff(overrides: Partial<DiffScan> = {}): DiffScan {
  return {
    attachment_id: 3,
    jira_id: "12",
    filename: "build.patch",
    scanned_at: "2026-10-26T10:00:00Z",
    project: null,
    mapping_source: null,
    candidates: ["system/core", "hardware/interfaces"],
    presence: null,
    presence_detail: null,
    apply: null,
    apply_detail: null,
    target_head: null,
    patch: null,
    ...overrides,
  };
}

function scannedItem(overrides: Partial<ItemPrescan> = {}): ItemPrescan {
  return {
    scanned: true,
    label: "unmapped",
    label_reason: "build.patch: one of system/core, hardware/interfaces",
    stale: [],
    diffs: [unmappedDiff()],
    depends_on: [],
    needed_by: [],
    ...overrides,
  };
}

describe("ItemPage pre-scan", () => {
  it("shows pre-scan results per diff, staleness and candidate projects", async () => {
    const scanned: ItemPrescan = {
      scanned: true,
      label: "conflicts",
      label_reason: "frameworks/av: icon.png conflict",
      stale: ["frameworks/av HEAD moved"],
      depends_on: [],
      needed_by: [],
      diffs: [
        {
          attachment_id: 2,
          jira_id: "11",
          filename: "0001-camera-fix-null-deref.patch",
          scanned_at: "2026-10-26T10:00:00Z",
          project: "frameworks/av",
          mapping_source: "hint",
          candidates: [],
          presence: "partial",
          presence_detail: { files: { "CameraService.cpp": "present", "CameraService.h": "not_present" } },
          apply: "conflicts",
          apply_detail: { files: { "CameraService.h": "conflicts" }, errors: { "CameraService.h": "3-way merge conflicts" } },
          target_head: "a".repeat(40),
          patch: null,
        },
        {
          attachment_id: 3,
          jira_id: "12",
          filename: "build.patch",
          scanned_at: "2026-10-26T10:00:00Z",
          project: null,
          mapping_source: null,
          candidates: ["system/core", "hardware/interfaces"],
          presence: null,
          presence_detail: null,
          apply: null,
          apply_detail: null,
          target_head: null,
          patch: null,
        },
      ],
    };
    itemApi({ "GET /workspaces/1/items/PROJ-101/prescan": () => scanned });
    renderRoute(URL);
    const section = await screen.findByRole("region", { name: "Pre-scan" });
    expect(await within(section).findByText("Stale: frameworks/av HEAD moved")).toBeInTheDocument();
    expect(within(section).getByText(/^Label:/)).toHaveTextContent("Label: Conflicts");
    expect(within(section).getByText("frameworks/av: icon.png conflict")).toBeInTheDocument();
    const rows = within(section).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("frameworks/av (path in description)");
    expect(rows[0]).toHaveTextContent("CameraService.h: Not present");
    expect(rows[0]).toHaveTextContent("ConflictsCameraService.h: Conflicts (3-way merge conflicts)");
    expect(rows[1]).toHaveTextContent("Unmapped candidates: system/core, hardware/interfaces");
  });

  it.each(TRIAGE_LABELS)("shows the %s label with its reason", async (label) => {
    itemApi({ "GET /workspaces/1/items/PROJ-101/prescan": () => scannedItem({ label, label_reason: `why ${label}` }) });
    renderRoute(URL);
    const section = await screen.findByRole("region", { name: "Pre-scan" });
    expect(await within(section).findByText(`why ${label}`)).toBeInTheDocument();
    expect(within(section).getByText(/^Label:/)).toHaveTextContent(/^Label: \S/);
  });

  it("shows the line evidence of a likely present diff", async () => {
    const likely = unmappedDiff({
      project: "frameworks/base",
      mapping_source: "files",
      candidates: [],
      presence: "likely_present",
      presence_detail: {
        files: { "Power.java": "likely_present" },
        heuristic: {
          "Power.java": { added: { found: 2, total: 3, missing: ["if (lock == null) return;"] }, removed: { gone: 1, total: 1, remaining: [] } },
        },
      },
    });
    itemApi({ "GET /workspaces/1/items/PROJ-101/prescan": () => scannedItem({ label: "likely_present", diffs: [likely] }) });
    renderRoute(URL);
    const evidence = await screen.findByRole("list", { name: "Line evidence" });
    expect(evidence).toHaveTextContent("Power.java: 2 of 3 added lines found, 1 of 1 removed lines gone");
    expect(within(evidence).getByText("if (lock == null) return;").tagName).toBe("CODE");
  });

  it("lists the items this one depends on and is needed by", async () => {
    const other = { key: "PROJ-105", summary: "Boot props loader", filename: "0001-loader.patch", triage_label: "applies" as const, dropped: false };
    itemApi({
      "GET /workspaces/1/items/PROJ-101/prescan": () =>
        scannedItem({
          depends_on: [
            { filename: "0002-use.patch", path: "init/init.cpp", blob: "45c5620", state: "pending", other },
            { filename: "0002-use.patch", path: "init/oem.rc", blob: "1a2b3c4", state: "missing", other: null },
          ],
          needed_by: [{ filename: "0002-use.patch", path: "init/init.cpp", blob: "9f8e7d6", state: "in_target", other: { ...other, key: "PROJ-107", dropped: true } }],
        }),
    });
    renderRoute(URL);
    const dependsOn = await screen.findByRole("list", { name: "Depends on" });
    const [pending, missing] = within(dependsOn).getAllByRole("listitem");
    expect(within(pending).getByRole("link", { name: "PROJ-105" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-105");
    expect(pending).toHaveTextContent("init/init.cpp Not in target (0002-use.patch ← 0001-loader.patch, base 45c5620)");
    expect(missing).toHaveTextContent("init/oem.rc Base in no diff (0002-use.patch, base 1a2b3c4)");
    const neededBy = screen.getByRole("list", { name: "Needed by" });
    expect(neededBy).toHaveTextContent("PROJ-107");
    expect(neededBy).toHaveTextContent("Dropped");
    expect(neededBy).toHaveTextContent("Already in target");
    expect(screen.queryByText(/No dependencies found/)).not.toBeInTheDocument();
  });

  it("says when no dependencies were found", async () => {
    itemApi({ "GET /workspaces/1/items/PROJ-101/prescan": () => scannedItem() });
    renderRoute(URL);
    expect(await screen.findByText(/No dependencies found/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Depends on" })).not.toBeInTheDocument();
  });

  it("picks a project for an unmapped diff and shows the new results", async () => {
    const picked = scannedItem({
      label: "applies",
      label_reason: "system/core: applies as is",
      diffs: [unmappedDiff({ project: "system/core", mapping_source: "manual", presence: "not_present", apply: "applies", target_head: "b".repeat(40) })],
    });
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101/prescan": () => scannedItem(),
      "GET /workspaces/1/target-projects": () => ["frameworks/av", "hardware/interfaces", "system/core"],
      "PUT /workspaces/1/items/PROJ-101/diffs/3/project": () => picked,
    });
    renderRoute(URL);
    const section = await screen.findByRole("region", { name: "Pre-scan" });
    const select = await within(section).findByRole("combobox", { name: "Project for build.patch" });
    // Candidates first, then the rest of the tree.
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(4));
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Pick a project…",
      "system/core",
      "hardware/interfaces",
      "frameworks/av",
    ]);
    expect(within(section).getByRole("button", { name: "Use project" })).toBeDisabled();
    await userEvent.selectOptions(select, "system/core");
    await userEvent.click(within(section).getByRole("button", { name: "Use project" }));

    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ project: "system/core" });
    const row = within(section).getAllByRole("row")[1];
    await waitFor(() => expect(row).toHaveTextContent("system/core (picked by hand)"));
    expect(row).toHaveTextContent("Applies");
    expect(within(section).queryByRole("combobox", { name: "Project for build.patch" })).toBeNull();
    // The item's history refetches.
    await waitFor(() => expect(calls.filter((c) => c.method === "GET" && c.path === "/workspaces/1/items/PROJ-101")).toHaveLength(2));
  });

  it("narrows the project list with the search box", async () => {
    itemApi({
      "GET /workspaces/1/items/PROJ-101/prescan": () => scannedItem(),
      "GET /workspaces/1/target-projects": () => ["frameworks/av", "hardware/interfaces", "system/core"],
    });
    renderRoute(URL);
    const section = await screen.findByRole("region", { name: "Pre-scan" });
    const select = await within(section).findByRole("combobox", { name: "Project for build.patch" });
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(4));
    const search = within(section).getByRole("searchbox", { name: "Search projects for build.patch" });
    await userEvent.type(search, "HARD");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Pick a project…", "hardware/interfaces"]);
    // The current choice stays listed whatever the search.
    await userEvent.selectOptions(select, "hardware/interfaces");
    await userEvent.clear(search);
    await userEvent.type(search, "core");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Pick a project…", "system/core", "hardware/interfaces"]);
    expect(select).toHaveValue("hardware/interfaces");
  });

  it("changes a mapped project or goes back to the automatic mapping", async () => {
    const manual = scannedItem({ diffs: [unmappedDiff({ project: "system/core", mapping_source: "manual", candidates: [] })] });
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101/prescan": () => manual,
      "GET /workspaces/1/target-projects": () => ["hardware/interfaces", "system/core"],
      "PUT /workspaces/1/items/PROJ-101/diffs/3/project": () => scannedItem(),
    });
    renderRoute(URL);
    const section = await screen.findByRole("region", { name: "Pre-scan" });
    await userEvent.click(await within(section).findByRole("button", { name: "Change" }));
    expect(await within(section).findByRole("option", { name: "hardware/interfaces" })).toBeInTheDocument();
    await userEvent.click(within(section).getByRole("button", { name: "Cancel" }));
    expect(within(section).queryByRole("combobox")).toBeNull();

    await userEvent.click(within(section).getByRole("button", { name: "Map automatically" }));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ project: null });
    expect(await within(section).findByRole("combobox", { name: "Project for build.patch" })).toBeInTheDocument();
  });

  it("shows why picking failed", async () => {
    itemApi({
      "GET /workspaces/1/items/PROJ-101/prescan": () => scannedItem(),
      "GET /workspaces/1/target-projects": () => ["system/core"],
      "PUT /workspaces/1/items/PROJ-101/diffs/3/project": () => ({ status: 409, body: { detail: "A pre-scan is already running for this workspace" } }),
    });
    renderRoute(URL);
    const section = await screen.findByRole("region", { name: "Pre-scan" });
    await userEvent.selectOptions(await within(section).findByRole("combobox", { name: "Project for build.patch" }), "system/core");
    await userEvent.click(within(section).getByRole("button", { name: "Use project" }));
    expect(await within(section).findByText(/A pre-scan is already running/)).toBeInTheDocument();
  });

  it("re-runs the pre-scan for this item and refetches its results when done", async () => {
    const running: PrescanJob = {
      id: 4,
      workspace_id: 1,
      scope: "keys",
      issue_keys: ["PROJ-101"],
      status: "running",
      stage: "scan",
      done: 0,
      total: 1,
      errors: [],
      error: null,
      cancel_requested: false,
      started_at: "2026-10-26T10:00:00Z",
      finished_at: null,
    };
    let jobPolls = 0;
    let resultFetches = 0;
    const calls = itemApi({
      "POST /workspaces/1/prescan": () => ({ status: 202, body: running }),
      "GET /workspaces/1/prescan-jobs": () => {
        jobPolls += 1;
        return jobPolls === 1 ? [] : [{ ...running, status: "done", stage: null, done: 1, finished_at: "2026-10-26T10:00:05Z" }];
      },
      "GET /workspaces/1/items/PROJ-101/prescan": () => {
        resultFetches += 1;
        return { scanned: resultFetches > 1, label: "present", label_reason: "every file reverse-applies", stale: [], diffs: [], depends_on: [], needed_by: [] };
      },
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderRoute(URL);
      const section = await screen.findByRole("region", { name: "Pre-scan" });
      expect(await within(section).findByText("Not pre-scanned yet.")).toBeInTheDocument();
      await userEvent.click(within(section).getByRole("button", { name: "Pre-scan this item" }));
      expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/prescan")?.body).toEqual({ keys: ["PROJ-101"] });
      expect(await within(section).findByText(/Pre-scanning… 0 \/ 1/)).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(POLL_MS);
      expect(await within(section).findByText(/Pre-scan done: 1 item/)).toBeInTheDocument();
      expect(await within(section).findByText("every file reverse-applies")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

function itemPatch(overrides: Partial<ItemPatch> = {}): ItemPatch {
  return {
    id: 7,
    attachment_id: 12,
    jira_id: "12",
    filename: PATCH,
    project: "frameworks/av",
    source: "original",
    level: "plain",
    base_head: "b".repeat(40),
    base_patch_ids: [],
    review: "accepted",
    review_note: null,
    findings: [],
    author: "Jane Doe <jane.doe@example.com>",
    message: "camera: fix null deref on resume\n\nBug: PROJ-101",
    current: true,
    attempt_id: null,
    stale: [],
    bases: [],
    created_at: "2026-10-26T10:00:00Z",
    ...overrides,
  };
}

const CPP = "services/camera/libcameraservice/CameraService.cpp";

describe("ItemPage migration tab", () => {
  it("lists a plain patch and opens it in the diff viewer", async () => {
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101/patches": () => [itemPatch()],
      "GET /workspaces/1/patches/7": () => ({ ...cameraV2, format: "plain", commits: [] }),
    });
    const { router } = renderRoute(URL);
    await userEvent.click(await screen.findByRole("tab", { name: "Migration" }));
    expect(router.state.location.search).toBe("?tab=migration");

    const section = await screen.findByRole("region", { name: "Migration patches" });
    const rows = within(await within(section).findByRole("table", { name: "Patches" })).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent(`${PATCH}frameworks/avApplies as isJira diffbbbbbbbbbbbb`);
    expect(rows[0]).toHaveTextContent("current");
    const view = within(section).getByRole("region", { name: "Patch" });
    expect(within(view).getByRole("heading", { name: `${PATCH} · Applies as is` })).toBeInTheDocument();
    expect(within(view).getByText("Jane Doe <jane.doe@example.com>")).toBeInTheDocument();
    expect(await within(view).findByRole("region", { name: CPP })).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/workspaces/1/patches/7")).toBe(true);
  });

  it("opens the current 3-way patch first and switches to a superseded one", async () => {
    itemApi({
      "GET /workspaces/1/items/PROJ-101/patches": () => [
        itemPatch({ id: 9, level: "3way", base_head: "c".repeat(40), current: true }),
        itemPatch({ id: 7, current: false }),
      ],
      "GET /workspaces/1/patches/9": () => ({ ...cameraV2, format: "plain", commits: [] }),
      "GET /workspaces/1/patches/7": () => ({ ...cameraV1, format: "plain", commits: [] }),
    });
    renderRoute(`${URL}?tab=migration`);
    const section = await screen.findByRole("region", { name: "Migration patches" });
    const rows = within(await within(section).findByRole("table", { name: "Patches" })).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("3-way merge");
    expect(rows[0]).toHaveTextContent("current");
    expect(rows[1]).toHaveTextContent("superseded");
    expect(rows[0]).toHaveAttribute("aria-selected", "true");
    const view = within(section).getByRole("region", { name: "Patch" });
    expect(within(view).getByRole("heading", { name: `${PATCH} · 3-way merge` })).toBeInTheDocument();
    const sidebar = await within(view).findByRole("navigation", { name: "Changed files" });
    expect(within(sidebar).getAllByRole("listitem")).toHaveLength(2);

    await userEvent.click(within(rows[1]).getByRole("button", { name: PATCH }));
    expect(rows[1]).toHaveAttribute("aria-selected", "true");
    expect(within(view).getByRole("heading", { name: `${PATCH} · Applies as is` })).toBeInTheDocument();
    await waitFor(() => expect(within(view).getAllByRole("listitem")).toHaveLength(1));
  });

  it("explains why a conflicting item has no patch", async () => {
    itemApi({ "GET /workspaces/1/items/PROJ-101/patches": () => [] });
    renderRoute(`${URL}?tab=migration`);
    const section = await screen.findByRole("region", { name: "Migration patches" });
    expect(await within(section).findByText(/No migration patch\..*diffs that conflict or are already present have none/)).toBeInTheDocument();
    expect(within(section).queryByRole("table", { name: "Patches" })).toBeNull();
  });
});

function buildRun(overrides: Partial<BuildRun> = {}): BuildRun {
  return {
    id: 12,
    job_id: 4,
    attempt_id: null,
    keys: ["PROJ-101"],
    cluster_id: null,
    patch_ids: [7],
    tool: "mmm",
    modules: ["frameworks/av"],
    command: "source build/envsetup.sh && lunch aosp_x86_64-eng && mmm frameworks/av",
    status: "build_failed",
    summary: "CameraService.cpp:12: error: use of undeclared identifier",
    log_url: "/api/workspaces/1/build-runs/12/log",
    started_at: "2026-10-26T20:00:00Z",
    finished_at: "2026-10-26T20:10:00Z",
    ...overrides,
  };
}

describe("ItemPage builds", () => {
  it("lists build runs with status, modules and summary, and opens the full log", async () => {
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101/patches": () => [],
      "GET /workspaces/1/build-jobs": () => [],
      "GET /workspaces/1/items/PROJ-101/builds": () => [
        buildRun(),
        buildRun({ id: 11, keys: ["PROJ-100", "PROJ-101"], cluster_id: 5, status: "build_successful", summary: "nothing to build", tool: null, modules: [], log_url: "/api/workspaces/1/build-runs/11/log" }),
        buildRun({ id: 10, status: "apply_failed", summary: "project dirty: frameworks/av", tool: null, modules: [], log_url: null }),
      ],
      "GET /workspaces/1/build-runs/12/log": () => "lunch aosp_x86_64-eng\nmmm frameworks/av\nFAILED: CameraService.o",
    });
    renderRoute(`${URL}?tab=migration`);
    const section = await screen.findByRole("region", { name: "Builds" });
    const rows = within(await within(section).findByRole("table", { name: "Build runs" })).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText("Build failed")).toHaveClass("tag-error");
    expect(within(rows[0]).getByText("mmm frameworks/av")).toHaveAttribute("title", expect.stringContaining("lunch aosp_x86_64-eng"));
    expect(rows[0]).toHaveTextContent("CameraService.cpp:12: error: use of undeclared identifier");
    expect(within(rows[1]).getByText("Built")).toHaveClass("tag-ok");
    expect(rows[1]).toHaveTextContent("with PROJ-100");
    expect(rows[1]).toHaveTextContent("nothing to build");
    expect(within(rows[2]).getByText("Apply failed")).toBeInTheDocument();
    expect(within(rows[2]).queryByRole("button", { name: "Full log" })).toBeNull();

    await userEvent.click(within(rows[0]).getByRole("button", { name: "Full log" }));
    expect(await within(section).findByLabelText("Log of build 12")).toHaveTextContent("FAILED: CameraService.o");
    expect(calls.some((c) => c.path === "/workspaces/1/build-runs/12/log")).toBe(true);
    await userEvent.click(within(section).getByRole("button", { name: "Hide log" }));
    expect(within(section).queryByLabelText("Log of build 12")).toBeNull();
  });

  it("builds the item and refetches its runs when the build finishes", async () => {
    const running: BuildJob = {
      id: 4,
      workspace_id: 1,
      scope: "keys",
      issue_keys: ["PROJ-101"],
      cluster_id: null,
      skipped: [],
      status: "running",
      done: 0,
      total: 1,
      errors: [],
      error: null,
      cancel_requested: false,
      started_at: "2026-10-26T20:00:00Z",
      finished_at: null,
      unit_keys: ["PROJ-101"],
      log_tail: "mmm frameworks/av",
    };
    let jobs: BuildJob[] = [];
    let runs: BuildRun[] = [];
    const calls = itemApi({
      "GET /workspaces/1/items/PROJ-101/patches": () => [],
      "GET /workspaces/1/build-jobs": () => jobs,
      "GET /workspaces/1/items/PROJ-101/builds": () => runs,
      "POST /workspaces/1/build": () => {
        jobs = [running];
        return { status: 202, body: running };
      },
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderRoute(`${URL}?tab=migration`);
      const section = await screen.findByRole("region", { name: "Builds" });
      expect(await within(section).findByText("Not built yet.")).toBeInTheDocument();
      await userEvent.click(within(section).getByRole("button", { name: "Build" }));
      expect(calls.find((c) => c.method === "POST" && c.path === "/workspaces/1/build")?.body).toEqual({ keys: ["PROJ-101"] });
      expect(await within(section).findByText(/Building PROJ-101 \(0 \/ 1\)/)).toBeInTheDocument();
      jobs = [{ ...running, status: "done", done: 1, finished_at: "2026-10-26T20:10:00Z" }];
      runs = [buildRun({ status: "build_successful", summary: "" })];
      await vi.advanceTimersByTimeAsync(POLL_MS);
      expect(await within(section).findByText("Build done: 1 unit")).toBeInTheDocument();
      expect(await within(section).findByText("Built")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows why a build could not start", async () => {
    itemApi({
      "GET /workspaces/1/items/PROJ-101/patches": () => [],
      "GET /workspaces/1/build-jobs": () => [],
      "GET /workspaces/1/items/PROJ-101/builds": () => [],
      "POST /workspaces/1/build": () => ({ status: 409, body: { detail: "A build is already running for this workspace" } }),
    });
    renderRoute(`${URL}?tab=migration`);
    await userEvent.click(await screen.findByRole("button", { name: "Build" }));
    expect(await screen.findByText("A build is already running for this workspace")).toBeInTheDocument();
  });

  it("describes build events in the history", async () => {
    const events: HistoryEvent[] = [
      { id: 1, type: "build_started", payload: { run_id: 12, job_id: 4 }, created_at: "2026-10-26T20:00:00Z" },
      { id: 2, type: "build_finished", payload: { run_id: 12, status: "build_failed", summary: "FAILED: CameraService.o" }, created_at: "2026-10-26T20:10:00Z" },
      { id: 3, type: "tree_restored", payload: { run_id: 13 }, created_at: "2026-10-26T21:00:00Z" },
    ];
    itemApi({ "GET /workspaces/1/items/PROJ-101": () => ({ ...itemDetail(), history: events }) });
    renderRoute(URL);
    const history = await screen.findByRole("region", { name: "History" });
    expect(within(history).getByText("Build started (run 12)")).toBeInTheDocument();
    expect(within(history).getByText("Build: Build failed (run 12)")).toBeInTheDocument();
    expect(within(history).getByText("FAILED: CameraService.o")).toBeInTheDocument();
    expect(within(history).getByText("Target tree restored after interrupted build run 13")).toBeInTheDocument();
  });

  it("describes stacked pre-scans in the history, with the stacked patch", async () => {
    const patch = "diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-a\n+b\n";
    const events: HistoryEvent[] = [
      {
        id: 1,
        type: "stacked_prescan",
        payload: {
          job_id: 5,
          order: ["PROJ-100", "PROJ-110", "PROJ-101"],
          position: 3,
          base: ["PROJ-100"],
          left_out: ["PROJ-110"],
          status: "applied",
          reason: null,
          diffs: [{ attachment_id: 7, filename: "0001-fix.patch", project: "frameworks/av", status: "3way", error: null, patch }],
        },
        created_at: "2026-10-27T09:00:00Z",
        stale: ["frameworks/av HEAD moved"],
      },
    ];
    itemApi({ "GET /workspaces/1/items/PROJ-101": () => ({ ...itemDetail(), history: events }) });
    renderRoute(URL);
    const history = await screen.findByRole("region", { name: "History" });
    expect(within(history).getByText("Stacked pre-scan: stacked (3 of 3)")).toBeInTheDocument();
    expect(within(history).getByText("On top of: HEAD + PROJ-100")).toBeInTheDocument();
    expect(within(history).getByText("Left out before it: PROJ-110")).toBeInTheDocument();
    expect(within(history).getByText("0001-fix.patch (frameworks/av): applies 3-way")).toBeInTheDocument();
    expect(within(history).getByText("Stale: frameworks/av HEAD moved")).toBeInTheDocument();
    const download = within(history).getByRole("link", { name: "Download" });
    expect(download).toHaveAttribute("download", "03-0001-fix.patch");
    expect(decodeURIComponent(download.getAttribute("href") ?? "")).toContain("+b");
  });
});
