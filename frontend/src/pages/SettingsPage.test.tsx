import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Settings } from "../api";
import { mockApi, renderRoute } from "../test-utils";

function settings(overrides: Partial<Settings["jira"]> = {}): Settings {
  return {
    jira: {
      base_url: { source: "file", value: "https://jira.example.com" },
      pat: { source: "file", is_set: true },
      ...overrides,
    },
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
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ jira: { base_url: "https://jira.other.com" } });
  });

  it("sends a newly typed PAT and clears the field after saving", async () => {
    const calls = mockApi({ "GET /settings": () => settings(), "PUT /settings": () => settings() });
    renderRoute("/settings");
    const pat = await screen.findByLabelText("Personal access token");
    await userEvent.type(pat, "new-token");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
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
});
