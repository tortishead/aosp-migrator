import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { createQueryClient } from "../queryClient";
import { mockApi } from "../test-utils";
import { DiffTabs, languageFor } from "./DiffViewer";
import { cameraAttachments, diffHandlers, PATCH } from "./fixtures";

function renderTabs() {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <DiffTabs workspaceId={1} attachments={cameraAttachments} />
    </QueryClientProvider>,
  );
}

function file(path: string): HTMLElement {
  return screen.getByRole("region", { name: path });
}

/** Text of each code cell in a file's diff table. */
function code(region: HTMLElement): string[] {
  return [...region.querySelectorAll(".diff-code")].map((cell) => cell.textContent ?? "");
}

const CPP = "services/camera/libcameraservice/CameraService.cpp";
const H = "services/camera/libcameraservice/CameraService.h";

describe("DiffTabs", () => {
  beforeEach(() => localStorage.clear());

  it("renders the current version of a patch in unified mode", async () => {
    mockApi(diffHandlers);
    const { container } = renderTabs();
    // One tab per filename, versions grouped.
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual([`${PATCH}2 versions`, "icons.patch"]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");

    const cpp = await screen.findByRole("region", { name: CPP });
    // The format-patch mail header sits above the files.
    const commit = screen.getByRole("region", { name: "Commit" });
    expect(within(commit).getByRole("heading", { name: "camera: fix null deref on resume" })).toBeInTheDocument();
    expect(within(commit).getByText("Jane Doe <jane.doe@example.com>")).toBeInTheDocument();
    expect(within(commit).getByText("9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a")).toBeInTheDocument();
    expect(within(commit).getByText(/Log the missing client/)).toBeInTheDocument();
    expect(code(cpp)).toContain("        return NO_INIT;");
    expect(within(cpp).getByText("@@ -1200,6 +1200,10 @@ status_t CameraService::onResume(int cameraId) {")).toBeInTheDocument();
    expect(container.querySelectorAll("table.diff-unified")).toHaveLength(2);
    expect(container.querySelector("table.diff-split")).toBeNull();
    // Inserted and deleted lines are marked, with syntax tokens.
    expect(code(file(H))).toEqual([
      "    virtual status_t    onSuspend(int cameraId);",
      "    virtual void        onResume(int cameraId);",
      "    virtual status_t    onResume(int cameraId);",
      " ", // react-diff-view keeps empty lines one space high
      "    // Debug",
    ]);
    expect(file(H).querySelectorAll(".diff-code-insert")).toHaveLength(1);
    expect(file(H).querySelectorAll(".diff-code-delete")).toHaveLength(1);
    expect(file(CPP).querySelector(".token.keyword")).not.toBeNull();

    // File sidebar with counts.
    const sidebar = screen.getByRole("navigation", { name: "Changed files" });
    expect(within(sidebar).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "CameraService.cpp+4 −0",
      "CameraService.h+1 −1",
    ]);
  });

  it("switches to split mode and remembers it", async () => {
    mockApi(diffHandlers);
    const { container, unmount } = renderTabs();
    await screen.findByRole("region", { name: CPP });
    await userEvent.click(screen.getByRole("button", { name: "Split" }));
    expect(container.querySelectorAll("table.diff-split")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Split" })).toHaveAttribute("aria-pressed", "true");
    // Both sides in one row: old line left, new line right.
    const row = file(H).querySelector(".diff-code-delete")!.closest("tr")!;
    expect(row.querySelector(".diff-code-insert")).not.toBeNull();
    unmount();

    const again = renderTabs();
    await screen.findByRole("region", { name: CPP });
    expect(again.container.querySelector("table.diff-unified")).toBeNull();
    expect(again.container.querySelectorAll("table.diff-split")).toHaveLength(2);
  });

  it("picks an older version", async () => {
    const calls = mockApi(diffHandlers);
    renderTabs();
    await screen.findByRole("region", { name: H });
    const version = screen.getByRole("combobox", { name: "Version" });
    expect(within(version).getAllByRole("option").map((o) => o.textContent)).toEqual([
      expect.stringMatching(/^v2 · .* \(current\)$/),
      expect.stringMatching(/^v1 · /),
    ]);
    await userEvent.selectOptions(version, "11");
    await waitFor(() => expect(screen.queryByRole("region", { name: H })).toBeNull());
    expect(code(file(CPP))).not.toContain("        return NO_INIT;");
    expect(code(file(CPP))).toContain("    if (client == nullptr) {");
    expect(calls.map((c) => c.path)).toContain("/workspaces/1/diffs/11");
  });

  it("collapses files and shows binary files without content", async () => {
    mockApi(diffHandlers);
    renderTabs();
    const cpp = await screen.findByRole("region", { name: CPP });
    await userEvent.click(within(cpp).getByRole("button", { expanded: true }));
    expect(code(cpp)).toEqual([]);
    // Clicking the file in the sidebar opens it again.
    await userEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "CameraService.cpp" }));
    expect(code(cpp)).toContain("        return NO_INIT;");

    await userEvent.click(screen.getByRole("tab", { name: "icons.patch" }));
    const png = await screen.findByRole("region", { name: "res/drawable/oem_battery.png" });
    expect(within(png).getByText("Binary file not shown.")).toBeInTheDocument();
    expect(within(png).getByText("added")).toBeInTheDocument();
  });
});

describe("languageFor", () => {
  it("maps AOSP file types", () => {
    expect(["A.java", "x.cpp", "x.h", "x.c", "K.kt", "res/values/strings.xml", "Android.bp", "Android.mk", "Makefile", "README"].map(languageFor)).toEqual(
      ["java", "cpp", "cpp", "c", "kotlin", "markup", "javascript", "makefile", "makefile", null],
    );
  });
});
