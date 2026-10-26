import { execFileSync } from "node:child_process";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { BUILD_JIRA_PORT, BUILD_TREE } from "../playwright.config";

// Built by the build fake Jira web server in playwright.config.ts; scenario in backend/tests/fake_jira/build.py.
const TARGET_TREE = path.resolve(import.meta.dirname, "..", BUILD_TREE);
const PROJECTS = ["system/core", "packages/apps/Settings"];

const git = (project: string, ...args: string[]) => execFileSync("git", args, { cwd: path.join(TARGET_TREE, project), encoding: "utf8" });

test("build a 3-way item and a failing one, the tree stays clean", async ({ page }) => {
  // Jira settings against the build fake Jira.
  await page.goto("/settings");
  await page.getByLabel("Base URL").fill(`http://127.0.0.1:${BUILD_JIRA_PORT}`);
  await page.getByLabel("Personal access token").fill("fake-jira-pat");
  const jira = page.getByRole("form", { name: "Jira" });
  await jira.getByRole("button", { name: "Save" }).click();
  await expect(jira.getByText("Saved.")).toBeVisible();

  // Workspace with a lunch target; envsetup.sh is the default one under the `repo` root.
  await page.goto("/");
  await page.getByRole("link", { name: "New workspace" }).click();
  await page.getByLabel("Name").fill("Build");
  await page.getByLabel("JQL").fill("project = PROJ");
  await page.getByLabel("Target tree path").fill(TARGET_TREE);
  await page.getByLabel("lunch target").fill("aosp_x86_64-eng");
  await page.getByRole("button", { name: "Create" }).click();
  await page.getByRole("link", { name: "Build", exact: true }).click();
  await page.getByRole("button", { name: "Import from Jira" }).click();
  await expect(page.getByText("Import done: 25 tickets")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Pre-scan backlog" }).click();
  await expect(page.getByText(/^Pre-scan done: 25 items/)).toBeVisible({ timeout: 60_000 });

  const heads = PROJECTS.map((p) => git(p, "rev-parse", "HEAD"));
  for (const p of PROJECTS) expect(git(p, "status", "--porcelain")).toBe("");

  // Build selected: PROJ-105 needs a 3-way merge and builds, PROJ-102 fails to compile, PROJ-103 is present.
  for (const key of ["PROJ-102", "PROJ-103", "PROJ-105"]) await page.getByRole("checkbox", { name: `Select ${key}` }).check();
  await page.getByRole("group", { name: "Bulk actions" }).getByRole("button", { name: "Build selected" }).click();
  const panel = page.getByRole("region", { name: "Build" });
  await expect(panel.getByRole("status")).toHaveText("Build done: 2 units", { timeout: 60_000 });
  await panel.getByText("1 item not built").click();
  await expect(panel).toContainText("PROJ-103: pre-scan: present");

  // Count bar, filter, column.
  const counts = page.getByRole("group", { name: "Build counts" });
  await expect(counts.getByRole("button")).toHaveText(["1 build failed", "1 built", "23 not built"]);
  const rows = page.locator("table.backlog tbody tr");
  await counts.getByRole("button", { name: "1 build failed" }).click();
  await expect(page).toHaveURL(/build=build_failed/);
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("PROJ-102");
  await expect(rows).toContainText("Build failed");

  // The failing item: summary and full log.
  await rows.getByRole("link", { name: "PROJ-102" }).click();
  await page.getByRole("tab", { name: /Migration/ }).click();
  const builds = page.getByRole("region", { name: "Builds" });
  const runs = builds.getByRole("table", { name: "Build runs" });
  await expect(runs.getByRole("row")).toHaveCount(2);
  await expect(runs.getByRole("row").nth(1)).toContainText("Build failed");
  await expect(runs.getByRole("row").nth(1)).toContainText("mmm packages/apps/Settings");
  await expect(runs.getByRole("row").nth(1)).toContainText("error: FAIL_BUILD marker");
  await runs.getByRole("button", { name: "Full log" }).click();
  await expect(runs.getByLabel(/^Log of build/)).toContainText("ninja: build stopped");

  // The 3-way item: the merged patch, the successful run and its log.
  await page.getByRole("link", { name: "← Backlog" }).click();
  await counts.getByRole("button", { name: "1 built" }).click();
  await rows.getByRole("link", { name: "PROJ-105" }).click();
  await page.getByRole("tab", { name: /Migration/ }).click();
  const patches = page.getByRole("region", { name: "Migration patches" });
  await expect(patches.getByRole("table", { name: "Patches" })).toContainText("3-way merge");
  await expect(patches.getByRole("heading", { level: 3 })).toContainText("3-way merge");
  await expect(patches.getByRole("region", { name: "Patch" })).toContainText("LoadOemBootProps");
  await expect(runs.getByRole("row").nth(1)).toContainText("Built");
  await expect(runs.getByRole("row").nth(1)).toContainText("mmm system/core");
  await runs.getByRole("button", { name: "Full log" }).click();
  await expect(runs.getByLabel(/^Log of build/)).toContainText("#### build completed successfully ####");
  await page.getByRole("tab", { name: /Overview/ }).click();
  const history = page.getByRole("region", { name: "History" });
  await expect(history).toContainText(/Build started \(run \d+\)/);
  await expect(history).toContainText(/Build: Built \(run \d+\)/);

  // Restored: same HEADs, nothing changed or left behind.
  expect(PROJECTS.map((p) => git(p, "rev-parse", "HEAD"))).toEqual(heads);
  for (const p of PROJECTS) expect(git(p, "status", "--porcelain", "--ignored")).toBe("");
});
