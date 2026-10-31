import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

// Ports away from the dev defaults (8000, 5173, 8090) so a running `make dev` does not interfere.
export const FAKE_JIRA_PORT = 8791;
export const APP_PORT = 8792;
// Serves the pre-scan scenario tickets that fit the `repo` tree it builds at PRESCAN_TREE.
export const PRESCAN_JIRA_PORT = 8793;
export const PRESCAN_TREE = ".prescan/aosp";
// The same scenario set up for builds (fake envsetup.sh, one compile failure, one 3-way patch) in a tree of its own.
export const BUILD_JIRA_PORT = 8794;
export const BUILD_TREE = ".build/aosp";
// The build scenario plus a chain of conflicting items, for rework with the fake Copilot.
export const REWORK_JIRA_PORT = 8795;
export const REWORK_TREE = ".rework/aosp";
// The fake Copilot CLI answers from <scenarios>/<ticket key>.json (the tests write them) and logs every call.
export const FAKE_COPILOT = path.resolve(import.meta.dirname, "../backend/tests/fake_copilot/copilot");
export const COPILOT_SCENARIOS = path.resolve(import.meta.dirname, ".data/copilot/scenarios");
export const COPILOT_LOG = path.resolve(import.meta.dirname, ".data/copilot/calls.jsonl");

// Every run starts from an empty data dir. Jira settings from the environment would override the UI.
const clean = "env -u AOSP_MIGRATOR_JIRA_BASE_URL -u AOSP_MIGRATOR_JIRA_PAT -u AOSP_MIGRATOR_DATA_DIR -u AOSP_MIGRATOR_COPILOT_BINARY";

export default defineConfig({
  testDir: "tests",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? "line" : [["line"], ["html", { open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${APP_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `cd ../backend && PYTHONPATH=tests uv run python -m fake_jira --port ${FAKE_JIRA_PORT}`,
      url: `http://127.0.0.1:${FAKE_JIRA_PORT}/rest/api/2/myself`, // 401 without a PAT counts as up
      reuseExistingServer: false,
    },
    {
      command: `rm -rf .prescan && cd ../backend && PYTHONPATH=tests uv run python -m fake_jira --port ${PRESCAN_JIRA_PORT} --prescan-tree ../e2e/${PRESCAN_TREE}`,
      url: `http://127.0.0.1:${PRESCAN_JIRA_PORT}/rest/api/2/myself`,
      reuseExistingServer: false,
    },
    {
      command: `rm -rf .build && cd ../backend && PYTHONPATH=tests uv run python -m fake_jira --port ${BUILD_JIRA_PORT} --prescan-tree ../e2e/${BUILD_TREE} --build`,
      url: `http://127.0.0.1:${BUILD_JIRA_PORT}/rest/api/2/myself`,
      reuseExistingServer: false,
    },
    {
      command: `rm -rf .rework && cd ../backend && PYTHONPATH=tests uv run python -m fake_jira --port ${REWORK_JIRA_PORT} --prescan-tree ../e2e/${REWORK_TREE} --rework`,
      url: `http://127.0.0.1:${REWORK_JIRA_PORT}/rest/api/2/myself`,
      reuseExistingServer: false,
    },
    {
      // Serves the prebuilt bundle in backend/aosp_migrator/static: run `pnpm build` in frontend/ first (`make test-e2e` does).
      command: `rm -rf .data && mkdir -p .data/tree ${COPILOT_SCENARIOS} && cd ../backend && ${clean} FAKE_COPILOT_SCENARIOS=${COPILOT_SCENARIOS} FAKE_COPILOT_LOG=${COPILOT_LOG} uv run aosp-migrator serve --port ${APP_PORT} --data-dir ../e2e/.data/app`,
      url: `http://127.0.0.1:${APP_PORT}/api/health`,
      reuseExistingServer: false,
    },
  ],
});
