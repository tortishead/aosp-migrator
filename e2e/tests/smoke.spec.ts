import path from "node:path";

import { expect, test } from "@playwright/test";

import { FAKE_JIRA_PORT } from "../playwright.config";

// Created fresh by the app web server command in playwright.config.ts.
const TARGET_TREE = path.resolve(import.meta.dirname, "../.data/tree");

test("import from fake Jira, triage an item, reimport keeps local state", async ({ page }) => {
  // Workspace.
  // Other specs may have made workspaces already.
  await page.goto("/");
  await page.getByRole("link", { name: "New workspace" }).click();
  await page.getByLabel("Name").fill("Android 15 OEM");
  await page.getByLabel("JQL").fill("project = PROJ AND labels = oem-patch");
  await page.getByLabel("Target tree path").fill(TARGET_TREE);
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("link", { name: "Android 15 OEM", exact: true })).toBeVisible();

  // Jira settings against the fake Jira.
  await page.getByRole("link", { name: "Settings" }).click();
  await page.getByLabel("Base URL").fill(`http://127.0.0.1:${FAKE_JIRA_PORT}`);
  await page.getByLabel("Personal access token").fill("fake-jira-pat");
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByText(/^Connected as /)).toBeVisible();
  const jira = page.getByRole("form", { name: "Jira" });
  await jira.getByRole("button", { name: "Save" }).click();
  await expect(jira.getByText("Saved.")).toBeVisible();

  // Import.
  await page.getByRole("link", { name: "Workspaces", exact: true }).click();
  await page.getByRole("link", { name: "Android 15 OEM", exact: true }).click();
  await expect(page.getByText("No items yet")).toBeVisible();
  await page.getByRole("button", { name: "Import from Jira" }).click();
  await expect(page.getByText("Import done: 25 tickets")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("1–25 of 25")).toBeVisible();

  // Filter: label facet, then free text over diff paths.
  const rows = page.locator("table.backlog tbody tr");
  await page.locator("summary", { hasText: "Labels" }).click();
  // Checkboxes follow the URL, which updates after the click, so `check()` would see no change yet.
  const camera = page.getByRole("group", { name: "Labels" }).getByLabel("camera");
  await camera.click();
  await expect(camera).toBeChecked();
  await expect(page).toHaveURL(/label=camera/);
  await expect(rows).toHaveCount(1);
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(rows).toHaveCount(25);
  await page.getByRole("searchbox", { name: "Search" }).fill("CameraService.h");
  await expect(page).toHaveURL(/q=CameraService\.h/);
  await expect(rows).toHaveCount(1);

  // Item detail.
  await rows.getByRole("link", { name: "PROJ-101" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Camera HAL crash on resume");
  const description = page.getByRole("region", { name: "Description" });
  await expect(description.getByRole("cell", { name: "Pixel 8" })).toBeVisible();
  await expect(description.locator("pre")).toContainText("client->notifyResume();");

  // Diff viewer: current version, both layouts, on the Patches tab.
  await page.getByRole("tab", { name: /^Patches/ }).click();
  await expect(page).toHaveURL(/tab=patches/);
  const diffs = page.getByRole("region", { name: "Diffs" });
  await expect(diffs.getByRole("tab", { name: /0001-camera-fix-null-deref\.patch/ })).toBeVisible();
  await expect(diffs.getByRole("navigation", { name: "Changed files" })).toContainText("CameraService.h");
  const file = diffs.getByRole("region", { name: "services/camera/libcameraservice/CameraService.cpp" });
  await expect(file.locator("table.diff-unified")).toBeVisible();
  await diffs.getByRole("button", { name: "Split" }).click();
  await expect(file.locator("table.diff-split")).toBeVisible();
  await diffs.getByRole("button", { name: "Unified" }).click();

  // Note and state.
  await page.getByRole("tab", { name: "Overview" }).click();
  const notes = page.getByRole("region", { name: "Notes" });
  await notes.getByLabel("New note").fill("Fixed upstream in Android 15, nothing to migrate.");
  await notes.getByRole("button", { name: "Add note" }).click();
  await expect(notes.getByText("Fixed upstream in Android 15, nothing to migrate.")).toBeVisible();
  await page.getByRole("combobox", { name: "State" }).selectOption({ label: "Not relevant" });
  const history = page.getByRole("region", { name: "History" });
  await expect(history).toContainText("State: New → Not relevant");

  // Reimport keeps state and notes.
  await page.getByRole("button", { name: "Reimport from Jira" }).click();
  await expect(page.getByText("Reimported.")).toBeVisible({ timeout: 30_000 });
  await page.reload();
  await expect(page.getByRole("combobox", { name: "State" })).toHaveValue("not_relevant");
  await expect(notes.getByText("Fixed upstream in Android 15, nothing to migrate.")).toBeVisible();

  // Backlog shows the state.
  await page.getByRole("link", { name: "← Backlog" }).click();
  await expect(rows).toHaveCount(25);
  await expect(rows.filter({ hasText: "PROJ-101" })).toContainText("Not relevant");

  // Cluster two related items; default order is oldest diff first (PROJ-102), then reorder.
  await page.getByRole("checkbox", { name: "Select PROJ-101" }).click();
  await page.getByRole("checkbox", { name: "Select PROJ-102" }).click();
  await page.getByRole("button", { name: "New cluster from selected" }).click();
  await page.getByRole("status").getByRole("link", { name: /^PROJ-102 / }).click();
  const members = page.getByRole("region", { name: "Members" });
  await expect(members.locator("tbody tr").first()).toContainText("PROJ-102");
  await expect(members.getByText("not relevant", { exact: true })).toBeVisible();
  await members.getByRole("button", { name: "Move PROJ-101 up" }).click();
  await expect(members.locator("tbody tr").first()).toContainText("PROJ-101");
  await expect(page.getByRole("region", { name: "History" })).toContainText("Reordered: PROJ-101, PROJ-102");

  // The item page shows the cluster and its siblings.
  await members.getByRole("link", { name: "PROJ-102" }).click();
  const cluster = page.getByRole("region", { name: "Cluster" });
  await expect(cluster.getByRole("link", { name: /^PROJ-102 / })).toBeVisible();
  await expect(cluster.getByRole("list", { name: "Cluster items" }).locator("li")).toHaveText([/^PROJ-101 /, /^PROJ-102 /]);
});
