import path from "node:path";

import { expect, test } from "@playwright/test";

import { PRESCAN_JIRA_PORT, PRESCAN_TREE } from "../playwright.config";

// Built by the pre-scan fake Jira web server in playwright.config.ts; outcomes per ticket in backend/tests/fake_jira/prescan.py.
const TARGET_TREE = path.resolve(import.meta.dirname, "..", PRESCAN_TREE);

test("pre-scan the backlog, pick a project by hand, close present items", async ({ page }) => {
  // Jira settings against the pre-scan fake Jira.
  await page.goto("/settings");
  await page.getByLabel("Base URL").fill(`http://127.0.0.1:${PRESCAN_JIRA_PORT}`);
  await page.getByLabel("Personal access token").fill("fake-jira-pat");
  const jira = page.getByRole("form", { name: "Jira" });
  await jira.getByRole("button", { name: "Save" }).click();
  await expect(jira.getByText("Saved.")).toBeVisible();

  // Workspace on the `repo` tree, import.
  await page.goto("/");
  await page.getByRole("link", { name: "New workspace" }).click();
  await page.getByLabel("Name").fill("Pre-scan");
  await page.getByLabel("JQL").fill("project = PROJ");
  await page.getByLabel("Target tree path").fill(TARGET_TREE);
  await page.getByRole("button", { name: "Create" }).click();
  await page.getByRole("link", { name: "Pre-scan", exact: true }).click();
  await page.getByRole("button", { name: "Import from Jira" }).click();
  await expect(page.getByRole("button", { name: "Import from Jira" })).toHaveAttribute("title", "Import done: 25 tickets", { timeout: 30_000 });

  // Pre-scan, then the count bar.
  await page.getByRole("button", { name: "Pre-scan backlog" }).click();
  await expect(page.getByRole("button", { name: "Pre-scan backlog" })).toHaveAttribute("title", /^Pre-scan done: 25 items/, { timeout: 60_000 });
  const counts = page.getByRole("group", { name: "Pre-scan counts" });
  await expect(counts.getByRole("button")).toHaveText(["1 unmapped", "1 conflicts", "6 applies", "2 present", "15 not scanned"]);
  const rows = page.locator("table.backlog tbody tr");
  await counts.getByRole("button", { name: "1 unmapped" }).click();
  await expect(page).toHaveURL(/triage=unmapped/);
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("Unmapped");

  // Item page: pick the project; the item is scanned again right away.
  await rows.getByRole("link", { name: "PROJ-110" }).click();
  await expect(page).toHaveURL(/\/items\/PROJ-110$/);
  const section = page.getByRole("region", { name: "Pre-scan" });
  await expect(section).toContainText("Label: Unmapped");
  const picker = section.getByRole("combobox", { name: "Project for 0001-Build-with-OEM-flag.patch" });
  await expect(picker.getByRole("option", { name: "system/core" })).toBeAttached();
  await picker.selectOption("system/core");
  await section.getByRole("button", { name: "Use project" }).click();
  await expect(section).toContainText("Label: Applies");
  await expect(section.getByRole("row").nth(1)).toContainText("system/core (picked by hand)");
  await expect(page.getByRole("region", { name: "History" })).toContainText("Target project for 0001-Build-with-OEM-flag.patch: system/core");

  // A full re-run keeps the pick.
  await page.getByRole("link", { name: "← Backlog" }).click();
  await page.getByRole("button", { name: "Pre-scan backlog" }).click();
  await expect(page.getByRole("button", { name: "Pre-scan backlog" })).toHaveAttribute("title", /^Pre-scan done: 25 items/, { timeout: 60_000 });
  await expect(counts.getByRole("button")).toHaveText(["1 conflicts", "7 applies", "2 present", "15 not scanned"]);

  // Close present items: count bar filter, select the page, bulk state change.
  await counts.getByRole("button", { name: "2 present" }).click();
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("PROJ-103");
  await expect(rows.nth(1)).toContainText("PROJ-109");
  await page.getByRole("checkbox", { name: "Select all on this page" }).click();
  const bulk = page.getByRole("group", { name: "Bulk actions" });
  await bulk.getByRole("combobox", { name: "New state" }).selectOption({ label: "Closed" });
  await bulk.getByRole("button", { name: "Apply" }).click();
  await expect(rows.nth(0)).toContainText("Closed");
  await expect(rows.nth(1)).toContainText("Closed");
  await rows.getByRole("link", { name: "PROJ-109" }).click();
  await expect(page.getByRole("combobox", { name: "State" })).toHaveValue("closed");
  await expect(page.getByRole("region", { name: "History" })).toContainText("State: New → Closed");
});
