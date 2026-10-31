import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Settings, Source } from "../api";
import { mockApi, renderRoute } from "../test-utils";

function settings(overrides: Partial<Settings["jira"]> = {}, domains: { source: Source; value: string[] } = { source: "unset", value: ["google.com", "android.com"] }): Settings {
  return {
    jira: {
      base_url: { source: "file", value: "https://jira.example.com" },
      pat: { source: "file", is_set: true },
      ...overrides,
    },
    prescan: { upstream_domains: domains },
    copilot: {
      binary: { source: "unset", value: "copilot" },
      model_resolve: { source: "file", value: "gpt-5-mini" },
      model_port: { source: "unset", value: null },
      model_reimpl: { source: "unset", value: null },
      timeout: { source: "unset", value: 900 },
    },
    rework: {
      item_cap: { source: "file", value: 3 },
      job_cap: { source: "env", value: 50 },
      build_rounds: { source: "unset", value: 2 },
    },
    build: { dependents_cap: { source: "unset", value: 20 } },
  };
}

describe("SettingsPage", () => {
  it("shows stored values without revealing the PAT", async () => {
    mockApi({ "GET /settings": () => settings() });
    renderRoute("/settings");
    expect(await screen.findByLabelText("Base URL")).toHaveValue("https://jira.example.com");
    const pat = screen.getByLabelText("Personal access token");
    expect(pat).toHaveValue("");
    expect(pat).toHaveAttribute("placeholder", "Set (leave empty to keep)");
  });

  it("does not send the PAT when left empty", async () => {
    const calls = mockApi({ "GET /settings": () => settings(), "PUT /settings": () => settings() });
    renderRoute("/settings");
    const baseUrl = await screen.findByLabelText("Base URL");
    await userEvent.clear(baseUrl);
    await userEvent.type(baseUrl, "https://jira.other.com");
    await userEvent.click(within(screen.getByRole("form", { name: "Jira" })).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ jira: { base_url: "https://jira.other.com" } });
  });

  it("sends a newly typed PAT and clears the field after saving", async () => {
    const calls = mockApi({ "GET /settings": () => settings(), "PUT /settings": () => settings() });
    renderRoute("/settings");
    const pat = await screen.findByLabelText("Personal access token");
    await userEvent.type(pat, "new-token");
    await userEvent.click(within(screen.getByRole("form", { name: "Jira" })).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      jira: { base_url: "https://jira.example.com", pat: "new-token" },
    });
    expect(pat).toHaveValue("");
  });

  it("locks fields that come from environment variables", async () => {
    mockApi({
      "GET /settings": () =>
        settings({ base_url: { source: "env", value: "https://env.example.com" }, pat: { source: "env", is_set: true } }),
    });
    renderRoute("/settings");
    expect(await screen.findByLabelText("Base URL")).toBeDisabled();
    expect(screen.getByLabelText("Personal access token")).toBeDisabled();
    expect(screen.getByText("Set by AOSP_MIGRATOR_JIRA_PAT.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear token" })).not.toBeInTheDocument();
  });

  it("shows test connection success and failure", async () => {
    let ok = true;
    const calls = mockApi({
      "GET /settings": () => settings(),
      "POST /settings/jira/test": () => (ok ? { ok: true, user: "Jane Doe" } : { ok: false, error: "Authentication failed (HTTP 401)" }),
    });
    renderRoute("/settings");
    const button = await screen.findByRole("button", { name: "Test connection" });
    await userEvent.click(button);
    expect(await screen.findByText("Connected as Jane Doe.")).toBeInTheDocument();
    expect(calls.at(-1)?.body).toEqual({ base_url: "https://jira.example.com" });

    ok = false;
    await userEvent.click(button);
    expect(await screen.findByText("Authentication failed (HTTP 401)")).toBeInTheDocument();
  });

  it("clears a stored PAT", async () => {
    const calls = mockApi({
      "GET /settings": () => settings(),
      "PUT /settings": () => settings({ pat: { source: "unset", is_set: false } }),
    });
    renderRoute("/settings");
    await userEvent.click(await screen.findByRole("button", { name: "Clear token" }));
    expect(await screen.findByLabelText("Personal access token")).toHaveAttribute("placeholder", "Not set");
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ jira: { pat: "" } });
  });

  it("shows the default upstream domains as a placeholder and saves new ones", async () => {
    const calls = mockApi({
      "GET /settings": () => settings(),
      "PUT /settings": () => settings({}, { source: "file", value: ["google.com", "oem.example.com"] }),
    });
    renderRoute("/settings");
    const domains = await screen.findByLabelText("Upstream author domains");
    expect(domains).toHaveValue("");
    expect(domains).toHaveAttribute("placeholder", "google.com, android.com");
    await userEvent.type(domains, "google.com, OEM.example.com");
    await userEvent.click(within(screen.getByRole("form", { name: "Pre-scan" })).getByRole("button", { name: "Save" }));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ prescan: { upstream_domains: "google.com, OEM.example.com" } });
    expect(await screen.findByDisplayValue("google.com, oem.example.com")).toBeInTheDocument();
  });

  it("shows a domain error and locks domains set by the environment", async () => {
    mockApi({
      "GET /settings": () => settings(),
      "PUT /settings": () => ({ status: 422, body: { detail: "Not an email domain: nope" } }),
    });
    const { unmount } = renderRoute("/settings");
    await userEvent.type(await screen.findByLabelText("Upstream author domains"), "nope");
    await userEvent.click(within(screen.getByRole("form", { name: "Pre-scan" })).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Not an email domain: nope")).toBeInTheDocument();
    unmount();

    mockApi({ "GET /settings": () => settings({}, { source: "env", value: ["corp.example.com"] }) });
    renderRoute("/settings");
    expect(await screen.findByLabelText("Upstream author domains")).toBeDisabled();
    expect(screen.getByLabelText("Upstream author domains")).toHaveValue("corp.example.com");
    expect(screen.getByText("Set by AOSP_MIGRATOR_UPSTREAM_DOMAINS.")).toBeInTheDocument();
  });

  it("shows Copilot defaults as placeholders and sends only what changed", async () => {
    const calls = mockApi({ "GET /settings": () => settings(), "PUT /settings": () => settings() });
    renderRoute("/settings");
    const form = within(await screen.findByRole("form", { name: "Copilot" }));
    expect(form.getByLabelText("Copilot CLI")).toHaveValue("");
    expect(form.getByLabelText("Copilot CLI")).toHaveAttribute("placeholder", "copilot");
    expect(form.getByLabelText("Model for resolving conflicts")).toHaveValue("gpt-5-mini");
    expect(form.getByLabelText("Model for porting rejected hunks")).toHaveAttribute("placeholder", "CLI default");
    expect(form.getByLabelText("Timeout per call (s)")).toHaveAttribute("placeholder", "900");
    expect(form.getByLabelText("Copilot calls per item")).toHaveValue(3);
    expect(form.getByLabelText("Copilot calls per rework job")).toBeDisabled();

    await userEvent.type(form.getByLabelText("Model for porting rejected hunks"), "gpt-5");
    await userEvent.type(form.getByLabelText("Build-fix rounds"), "0");
    await userEvent.clear(form.getByLabelText("Copilot calls per item"));
    await userEvent.click(form.getByRole("button", { name: "Save" }));
    expect(await form.findByText("Saved.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      copilot: { model_port: "gpt-5" },
      rework: { item_cap: 0, build_rounds: 0 },
    });
  });

  it("saves the dependents cap, empty restoring the default", async () => {
    const calls = mockApi({ "GET /settings": () => settings(), "PUT /settings": () => settings() });
    renderRoute("/settings");
    const form = within(await screen.findByRole("form", { name: "Build" }));
    const cap = form.getByLabelText("Dependent modules per build");
    expect(cap).toHaveValue(null);
    expect(cap).toHaveAttribute("placeholder", "20");
    await userEvent.type(cap, "5");
    await userEvent.click(form.getByRole("button", { name: "Save" }));
    expect(await form.findByText("Saved.")).toBeInTheDocument();
    await userEvent.clear(cap);
    await userEvent.click(form.getByRole("button", { name: "Save" }));
    expect(calls.filter((c) => c.method === "PUT").map((c) => c.body)).toEqual([{ build: { dependents_cap: 5 } }, { build: { dependents_cap: -1 } }]);
  });

  it("tests Copilot with the unsaved binary and model", async () => {
    const calls = mockApi({
      "GET /settings": () => settings(),
      "POST /settings/copilot/test": () => ({ ok: true, version: "1.0.105", model: "gpt-5-mini", reply: "OK", usage: {} }),
    });
    renderRoute("/settings");
    const form = within(await screen.findByRole("form", { name: "Copilot" }));
    await userEvent.type(form.getByLabelText("Copilot CLI"), "/opt/copilot");
    await userEvent.click(form.getByRole("button", { name: "Test Copilot" }));
    const result = within(await form.findByRole("status", { name: "Copilot works" }));
    expect(result.getByText("1.0.105")).toBeInTheDocument();
    expect(result.getByText("gpt-5-mini")).toBeInTheDocument();
    expect(result.getByText("OK")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ binary: "/opt/copilot", model: "gpt-5-mini" });
  });

  it("warns when the CLI answered without a token in the environment", async () => {
    const message = "COPILOT_GITHUB_TOKEN is not set in the backend's environment (nor GH_TOKEN or GITHUB_TOKEN).";
    mockApi({
      "GET /settings": () => settings(),
      "POST /settings/copilot/test": () => ({ ok: true, version: "1.0.105", model: null, reply: "OK", usage: {}, token: { status: "missing", variable: null, login: null, message } }),
    });
    renderRoute("/settings");
    const form = within(await screen.findByRole("form", { name: "Copilot" }));
    await userEvent.click(form.getByRole("button", { name: "Test Copilot" }));
    const result = within(await form.findByRole("status", { name: "Copilot works, but check the token" }));
    expect(result.getByText("CLI default")).toBeInTheDocument();
    expect(result.getByText(message)).toBeInTheDocument();
    expect(result.getByText("Token · Missing")).toHaveClass("warning");
  });

  it("shows that the token is not valid", async () => {
    const message = "COPILOT_GITHUB_TOKEN is not valid: GitHub rejected it (HTTP 401, bad credentials).";
    mockApi({
      "GET /settings": () => settings(),
      "POST /settings/copilot/test": () => ({ ok: false, error: message, token: { status: "invalid", variable: "COPILOT_GITHUB_TOKEN", login: null, message } }),
    });
    renderRoute("/settings");
    const form = within(await screen.findByRole("form", { name: "Copilot" }));
    await userEvent.click(form.getByRole("button", { name: "Test Copilot" }));
    const result = within(await form.findByRole("status", { name: "Copilot test failed" }));
    // The token message is the error: shown once.
    expect(result.getAllByText(message)).toHaveLength(1);
  });

  it("shows why the Copilot test failed", async () => {
    mockApi({
      "GET /settings": () => settings(),
      "POST /settings/copilot/test": () => ({ ok: false, error: "Copilot CLI does not run: not found" }),
    });
    renderRoute("/settings");
    const form = within(await screen.findByRole("form", { name: "Copilot" }));
    await userEvent.click(form.getByRole("button", { name: "Test Copilot" }));
    const result = within(await form.findByRole("status", { name: "Copilot test failed" }));
    expect(result.getByText("Copilot CLI does not run: not found")).toBeInTheDocument();
  });

  it("filters settings by the search box and hides sections without a match", async () => {
    mockApi({ "GET /settings": () => settings() });
    renderRoute("/settings");
    await screen.findByLabelText("Base URL");
    await userEvent.type(screen.getByLabelText("Search settings"), "timeout");
    expect(screen.getByLabelText("Timeout per call (s)")).toBeVisible();
    expect(screen.queryByRole("form", { name: "Jira" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Jira/ })).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("Search settings"));
    await userEvent.type(screen.getByLabelText("Search settings"), "zzz");
    expect(screen.getByText("No settings found for “zzz”.")).toBeInTheDocument();
  });
});
