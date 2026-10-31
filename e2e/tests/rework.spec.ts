import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { COPILOT_LOG, COPILOT_SCENARIOS, FAKE_COPILOT, REWORK_JIRA_PORT, REWORK_TREE } from "../playwright.config";

// Built by the rework fake Jira web server in playwright.config.ts; scenario in backend/tests/fake_jira/rework.py.
const TARGET_TREE = path.resolve(import.meta.dirname, "..", REWORK_TREE);
const CORE = "system/core";
const PROPS = "init/oem_props.cpp";
// What a 3-way merge of PROJ-111 leaves in the target's file, and Copilot's resolution.
const REGION = "<<<<<<< ours\nint mode = 0;\nint level = 5;\n||||||| base\nint mode = 0;\nint level = 0;\n=======\nint mode = 1;\nint level = 0;\n>>>>>>> theirs\n";
const RESOLVED = "int mode = 1;\nint level = 5;\n";

const git = (...args: string[]) => execFileSync("git", args, { cwd: path.join(TARGET_TREE, CORE), encoding: "utf8" });

/** The fake Copilot's answer for calls about `key`. */
function copilotAnswers(key: string, reply: string) {
  writeFileSync(path.join(COPILOT_SCENARIOS, `${key}.json`), JSON.stringify({ reply, edits: [{ path: PROPS, old: REGION, new: RESOLVED }] }));
}

function copilotPrompts(): string[] {
  return readFileSync(COPILOT_LOG, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => (JSON.parse(line) as { prompt: string }).prompt);
}

async function openMigration(page: Page, key: string) {
  const back = page.getByRole("link", { name: "← Backlog" });
  if (await back.isVisible()) await back.click();
  await page.locator("table.backlog tbody tr").getByRole("link", { name: key, exact: true }).click();
  await page.getByRole("tab", { name: /Migration/ }).click();
}

test("rework a chain of two conflicting items, accept, build stacked, reject and rework again", async ({ page }) => {
  test.setTimeout(120_000);
  // Jira against the rework fake Jira; Copilot is the fake CLI.
  await page.goto("/settings");
  await page.getByLabel("Base URL").fill(`http://127.0.0.1:${REWORK_JIRA_PORT}`);
  await page.getByLabel("Personal access token").fill("fake-jira-pat");
  const jira = page.getByRole("form", { name: "Jira" });
  await jira.getByRole("button", { name: "Save" }).click();
  await expect(jira.getByText("Saved.")).toBeVisible();
  const copilot = page.getByRole("form", { name: "Copilot" });
  await copilot.getByLabel("Copilot CLI").fill(FAKE_COPILOT);
  await copilot.getByRole("button", { name: "Save" }).click();
  await expect(copilot.getByText("Saved.")).toBeVisible();
  await copilot.getByRole("button", { name: "Test Copilot" }).click();
  await expect(copilot).toContainText("Copilot CLI 1.0.105-fake answered with its default model: OK");

  // Workspace with a lunch target, import, pre-scan.
  await page.goto("/");
  await page.getByRole("link", { name: "New workspace" }).click();
  await page.getByLabel("Name").fill("Rework");
  await page.getByLabel("JQL").fill("project = PROJ");
  await page.getByLabel("Target tree path").fill(TARGET_TREE);
  await page.getByLabel("lunch target").fill("aosp_x86_64-eng");
  await page.getByRole("button", { name: "Create" }).click();
  await page.getByRole("link", { name: "Rework", exact: true }).click();
  await page.getByRole("button", { name: "Import from Jira" }).click();
  await expect(page.getByRole("button", { name: "Import from Jira" })).toHaveAttribute("title", "Import done: 25 tickets", { timeout: 30_000 });
  await page.getByRole("button", { name: "Pre-scan backlog" }).click();
  await expect(page.getByRole("button", { name: "Pre-scan backlog" })).toHaveAttribute("title", /^Pre-scan done: 25 items/, { timeout: 60_000 });
  const rows = page.locator("table.backlog tbody tr");
  for (const key of ["PROJ-111", "PROJ-112"]) await expect(rows.filter({ hasText: key })).toContainText("Conflicts");

  const head = git("rev-parse", "HEAD");
  expect(git("status", "--porcelain")).toBe("");

  // Rework PROJ-111 with the items that need it: PROJ-112 on top, Copilot only for PROJ-111's conflict.
  copilotAnswers("PROJ-111", `${PROPS}: mode 1, kept level 5`);
  await page.getByRole("checkbox", { name: "Select PROJ-111" }).check();
  await page.getByRole("group", { name: "Bulk actions" }).getByRole("button", { name: "Rework…" }).click();
  const dialog = page.getByRole("dialog", { name: "Rework" });
  await dialog.getByLabel("Also rework the items that need these").check();
  const units = dialog.getByRole("region", { name: "Units" }).getByRole("listitem");
  await expect(units).toHaveText(["PROJ-111", "PROJ-112dependent"]);
  await expect(dialog.getByRole("checkbox", { name: "Resolve conflicts" })).toBeChecked();
  await expect(dialog.getByRole("status", { name: "Estimate" })).toHaveText("2 units, 2 items; Copilot calls likely 1, at most 10");
  await dialog.getByRole("button", { name: "Start rework" }).click();
  const jobs = page.getByRole("button", { name: /^Job status/ });
  await expect(jobs).toHaveAttribute("title", /Rework done: 2 units, 1 Copilot call$/, { timeout: 60_000 });
  const counts = page.getByRole("group", { name: "Rework counts" });
  await expect(counts.getByRole("button")).toHaveText(["2 proposed"]);

  // PROJ-112's patch sits on PROJ-111's: accept is blocked until the base is accepted.
  await rows.getByRole("link", { name: "PROJ-112", exact: true }).click();
  await page.getByRole("tab", { name: /Migration/ }).click();
  let review = page.getByRole("region", { name: "Review", exact: true });
  await expect(page.getByRole("table", { name: "Patches", exact: true })).toContainText("Stacked");
  await expect(review.getByLabel("Base chain")).toContainText("PROJ-111 (system/core) proposed");
  await expect(review.getByRole("button", { name: "Accept" })).toBeDisabled();
  await expect(review).toContainText("Accept the base patch first: PROJ-111 (system/core, proposed)");

  // PROJ-111: Copilot's resolution, its explanation and the attempt; accept it.
  await review.getByRole("link", { name: "PROJ-111" }).click();
  await page.getByRole("tab", { name: /Migration/ }).click();
  await expect(page.getByRole("region", { name: "Patch", exact: true })).toContainText("int mode = 1;");
  review = page.getByRole("region", { name: "Review", exact: true });
  await expect(review.getByRole("region", { name: "Copilot's explanation" })).toContainText(`${PROPS}: mode 1, kept level 5`);
  const attempts = page.getByRole("list", { name: "Rework attempts" }).getByRole("listitem");
  await expect(attempts).toHaveCount(1);
  await expect(attempts.first()).toContainText("Patched");
  await expect(attempts.first()).toContainText("rungs: stack → resolve");
  await expect(attempts.first()).toContainText("1 Copilot call");
  await review.getByRole("button", { name: "Accept" }).click();
  await expect(review).toContainText("Review: accepted");

  // Then PROJ-112.
  await openMigration(page, "PROJ-112");
  review = page.getByRole("region", { name: "Review", exact: true });
  await expect(review.getByLabel("Base chain")).toContainText("PROJ-111 (system/core) accepted");
  await review.getByRole("button", { name: "Accept" }).click();
  await expect(review).toContainText("Review: accepted");

  // Stacked build of both: their accepted patches, green.
  await page.getByRole("link", { name: "← Backlog" }).click();
  await expect(counts.getByRole("button")).toHaveText(["2 accepted"]);
  for (const key of ["PROJ-111", "PROJ-112"]) await page.getByRole("checkbox", { name: `Select ${key}` }).check();
  await page.getByRole("group", { name: "Bulk actions" }).getByRole("button", { name: "Stacked build…" }).click();
  const stacked = page.getByRole("dialog", { name: "Stacked build" });
  await expect(stacked.getByRole("list", { name: "Stack order" }).getByRole("listitem")).toHaveText([/PROJ-111/, /PROJ-112/]);
  await stacked.getByRole("button", { name: "Start stacked build" }).click();
  await expect(jobs).toHaveAttribute("title", /Stacked build done: 1 unit/, { timeout: 60_000 });
  await expect(page.getByRole("group", { name: "Build counts" }).getByRole("button")).toHaveText(["2 built", "23 not built"]);
  await openMigration(page, "PROJ-112");
  const runs = page.getByRole("region", { name: "Builds" }).getByRole("table", { name: "Build runs" });
  // Newest first: the stacked run, then the rework's own build of PROJ-112's patch.
  await expect(runs.getByRole("row")).toHaveCount(3);
  const stackedRun = runs.getByRole("row").nth(1);
  await expect(stackedRun.getByRole("cell").nth(1)).toHaveText("Builtwith PROJ-111");
  await stackedRun.getByRole("button", { name: "Full log" }).click();
  await expect(runs.getByLabel(/^Log of build/)).toContainText("#### build completed successfully ####");

  // Restored: same HEAD, nothing changed or left behind.
  expect(git("rev-parse", "HEAD")).toBe(head);
  expect(git("status", "--porcelain", "--ignored")).toBe("");

  // Reject PROJ-111's patch with a note; the next rework gets the note, and PROJ-112's patch goes stale.
  await openMigration(page, "PROJ-111");
  review = page.getByRole("region", { name: "Review", exact: true });
  await review.getByLabel("Reject note").fill("say why level stays 5");
  await review.getByRole("button", { name: "Reject" }).click();
  await expect(review).toContainText("Review: rejected");
  await expect(review).toContainText("Note: say why level stays 5");
  copilotAnswers("PROJ-111", `${PROPS}: mode 1; level stays 5, the target set it on purpose`);
  const calls = copilotPrompts().length;
  await page.getByRole("region", { name: "Migration patches" }).getByRole("button", { name: "Rework…" }).click();
  await expect(units).toHaveText(["PROJ-111"]);
  await dialog.getByRole("button", { name: "Start rework" }).click();
  await expect(page.getByRole("region", { name: "Migration patches" }).getByRole("status")).toHaveText("Rework done: 1 unit, 1 Copilot call", {
    timeout: 60_000,
  });
  const prompts = copilotPrompts().slice(calls);
  expect(prompts).toHaveLength(1);
  expect(prompts[0]).toContain("The reviewer rejected the last patch: say why level stays 5\n");
  await expect(attempts).toHaveCount(2);
  await expect(attempts.first()).toContainText("Patched");
  review = page.getByRole("region", { name: "Review", exact: true });
  await expect(review).toContainText("Review: proposed");
  await expect(review.getByRole("region", { name: "Copilot's explanation" })).toContainText("level stays 5, the target set it on purpose");
  await expect(page.getByRole("table", { name: "Patches", exact: true }).getByRole("row").filter({ hasText: "rejected" })).toContainText("superseded");

  await openMigration(page, "PROJ-112");
  review = page.getByRole("region", { name: "Review", exact: true });
  await expect(review.getByLabel("Stale reason")).toContainText("base PROJ-111 changed");

  expect(git("rev-parse", "HEAD")).toBe(head);
  expect(git("status", "--porcelain", "--ignored")).toBe("");
});
