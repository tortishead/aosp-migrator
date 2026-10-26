import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HistoryEvent, ImportJob, ItemDetail, Note } from "../api";
import { diffHandlers, itemDetail } from "../item/fixtures";
import { mockApi, renderRoute } from "../test-utils";

const URL = "/workspaces/1/items/PROJ-101";

function itemApi(handlers: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({ "GET /workspaces/1/items/PROJ-101": () => itemDetail(), ...diffHandlers, ...handlers });
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

    const description = screen.getByRole("region", { name: "Description" });
    expect(within(description).getByRole("table")).toBeInTheDocument();
    expect(within(description).getByRole("cell", { name: "Pixel 8" })).toBeInTheDocument();
    expect(description.querySelector("script")).toBeNull();
    expect((window as { pwned?: boolean }).pwned).toBeUndefined();

    const comments = screen.getByRole("region", { name: "Comments" });
    expect(within(comments).getByText("Raj Patel")).toBeInTheDocument();
    expect(within(comments).getByText("AP2A").tagName).toBe("B");

    // Diffs get their own tabs; everything else is listed with size and a Jira link.
    expect(await screen.findByRole("region", { name: "services/camera/libcameraservice/CameraService.cpp" })).toBeInTheDocument();
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
