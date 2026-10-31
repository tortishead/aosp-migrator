import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { CopilotUsage, CopilotUsageBucket } from "../api";
import { mockApi, renderRoute } from "../test-utils";
import { dailySeries, formatDuration, formatTokens } from "./CopilotPage";

function bucket(over: Partial<CopilotUsageBucket> = {}): CopilotUsageBucket {
  return { calls: 0, ok: 0, failed: 0, canceled: 0, reported: 0, premium_requests: 0, input_tokens: 0, output_tokens: 0, duration: 0, ...over };
}

const today = new Date().toISOString().slice(0, 10);

function usage(over: Partial<CopilotUsage> = {}): CopilotUsage {
  const b = bucket({ calls: 3, ok: 2, failed: 1, reported: 2, premium_requests: 2.5, input_tokens: 4000, output_tokens: 400, duration: 920 });
  return {
    workspace_id: null,
    days: 30,
    totals: { ...b, attempts: 2 },
    by_rung: [{ rung: "resolve", ...b }],
    by_day: [{ day: today, ...b }],
    by_workspace: [
      { workspace_id: 1, name: "A15", ...b },
      { workspace_id: 2, name: "A16", ...bucket({ calls: 1 }) },
    ],
    by_model: [{ model: "claude-sonnet-4.5", calls: 2, requests: 7, input_tokens: 4000, output_tokens: 400 }],
    by_requested_model: [{ model: null, ...b }],
    recent: [
      {
        attempt_id: 5,
        workspace_id: 1,
        workspace: "A15",
        keys: ["PROJ-1"],
        at: new Date().toISOString(),
        project: "core",
        rung: "port",
        round: 1,
        build_round: 0,
        model: null,
        served_models: ["claude-sonnet-4.5"],
        status: "ok",
        premium_requests: 1,
        input_tokens: 1834,
        output_tokens: 42,
        duration: 12.5,
      },
    ],
    ...over,
  };
}

describe("CopilotPage", () => {
  it("shows totals, breakdowns and recent calls", async () => {
    mockApi({ "GET /copilot/usage": () => usage(), "GET /workspaces": () => [{ id: 1, name: "A15" }] });
    renderRoute("/copilot");
    expect(await screen.findByText("Premium requests", { selector: ".stat-label" })).toBeInTheDocument();
    const tiles = screen.getAllByText(/./, { selector: ".stat-value" }).map((e) => e.textContent);
    expect(tiles).toEqual(["3", "2.5", "4,400", "67%", "15 min"]);
    expect(screen.getByText(/1 of 3 calls reported no usage/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "By workspace" })).toBeInTheDocument();
    const recent = screen.getByRole("heading", { name: "Recent calls" }).closest("section")!;
    expect(within(recent).getByRole("link", { name: "PROJ-1" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-1");
    expect(within(recent).getByText("claude-sonnet-4.5 (default)")).toBeInTheDocument();
  });

  it("shows the Copilot calls running now and refreshes the statistics when one ends", async () => {
    let live = [
      {
        workspace_id: 1,
        workspace: "A15",
        job_id: 7,
        attempt_id: 9,
        keys: ["PROJ-2"],
        key: "PROJ-2",
        project: "frameworks/base",
        call: "call-3",
        rung: "resolve",
        round: 1,
        build_round: 0,
        model: null,
        started_at: new Date().toISOString(),
        elapsed: 3,
        log: "thinking about the conflict",
      },
    ];
    const calls = mockApi({ "GET /copilot/usage": () => usage(), "GET /copilot/live": () => live, "GET /workspaces": () => [{ id: 1, name: "A15" }] });
    renderRoute("/copilot");
    const running = await screen.findByRole("region", { name: "Running now" });
    const call = within(running).getByRole("region", { name: "Copilot call running" });
    expect(call).toHaveTextContent("Copilot · A15");
    expect(call).toHaveTextContent("Resolve conflicts · fix round 1");
    expect(call).toHaveTextContent("PROJ-2 (frameworks/base)");
    expect(call).toHaveTextContent("default model");
    expect(within(call).getByLabelText("Copilot output")).toHaveTextContent("thinking about the conflict");
    expect(within(running).getByRole("link", { name: "PROJ-2" })).toHaveAttribute("href", "/workspaces/1/items/PROJ-2?tab=migration");
    const before = calls.filter((c) => c.path.startsWith("/copilot/usage")).length;
    live = [];
    await waitFor(() => expect(screen.queryByRole("region", { name: "Running now" })).not.toBeInTheDocument(), { timeout: 3000 });
    await waitFor(() => expect(calls.filter((c) => c.path.startsWith("/copilot/usage")).length).toBeGreaterThan(before));
  });

  it("filters by workspace and period", async () => {
    const calls = mockApi({ "GET /copilot/usage": () => usage(), "GET /workspaces": () => [{ id: 1, name: "A15" }] });
    renderRoute("/copilot");
    await screen.findByText("Copilot calls");
    expect(calls.some((c) => c.path === "/copilot/usage?days=30")).toBe(true);
    await userEvent.selectOptions(screen.getByLabelText("Workspace filter"), "1");
    await userEvent.click(screen.getByRole("button", { name: "All time" }));
    expect(calls.some((c) => c.path === "/copilot/usage?workspace_id=1")).toBe(true);
  });

  it("says when there were no calls", async () => {
    mockApi({ "GET /copilot/usage": () => usage({ totals: { ...bucket(), attempts: 0 }, by_day: [], recent: [] }) });
    renderRoute("/copilot");
    expect(await screen.findByText(/No Copilot calls in this period/)).toBeInTheDocument();
  });

  it("formats and fills the days", () => {
    expect(formatTokens(4200)).toBe("4,200");
    expect(formatTokens(1_250_000)).toBe("1.3M");
    expect(formatDuration(12.5)).toBe("12.5 s");
    expect(formatDuration(3900)).toBe("1 h 5 min");
    const series = dailySeries(usage({ days: 7, by_day: [{ day: "2026-10-30", ...bucket({ calls: 2 }) }] }), new Date("2026-10-31T12:00:00Z"));
    expect(series.map((d) => d.day)).toEqual(["2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31"]);
    expect(series[5].calls).toBe(2);
  });
});
