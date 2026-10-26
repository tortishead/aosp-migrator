import { defineConfig, devices } from "@playwright/test";

// Ports away from the dev defaults (8000, 5173, 8090) so a running `make dev` does not interfere.
export const FAKE_JIRA_PORT = 8791;
export const APP_PORT = 8792;
// Serves the pre-scan scenario tickets that fit the `repo` tree it builds at PRESCAN_TREE.
export const PRESCAN_JIRA_PORT = 8793;
export const PRESCAN_TREE = ".prescan/aosp";

// Every run starts from an empty data dir. Jira settings from the environment would override the UI.
const clean = "env -u AOSP_MIGRATOR_JIRA_BASE_URL -u AOSP_MIGRATOR_JIRA_PAT -u AOSP_MIGRATOR_DATA_DIR";

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
      // Serves the prebuilt bundle in backend/aosp_migrator/static: run `pnpm build` in frontend/ first (`make test-e2e` does).
      command: `rm -rf .data && mkdir -p .data/tree && cd ../backend && ${clean} uv run aosp-migrator serve --port ${APP_PORT} --data-dir ../e2e/.data/app`,
      url: `http://127.0.0.1:${APP_PORT}/api/health`,
      reuseExistingServer: false,
    },
  ],
});
