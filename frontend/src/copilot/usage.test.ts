import { describe, expect, it } from "vitest";

import { aiCredits, modelLabel, modelUsage, premiumRequests } from "./usage";

const usage = {
  totalPremiumRequestCost: 1,
  totalUserRequests: 1,
  modelMetrics: { "claude-sonnet-4.5": { requests: { count: 3, cost: 1 }, usage: { inputTokens: 1834, outputTokens: 42 } } },
};

describe("Copilot usage", () => {
  it("reads the models that served the call", () => {
    expect(modelUsage(usage)).toEqual([{ model: "claude-sonnet-4.5", requests: 3, inputTokens: 1834, outputTokens: 42 }]);
    expect(premiumRequests(usage)).toBe(1);
  });

  it("copes with no or odd usage", () => {
    expect(modelUsage(null)).toEqual([]);
    expect(modelUsage({ modelMetrics: { x: "nope" } })).toEqual([{ model: "x", requests: null, inputTokens: null, outputTokens: null }]);
    expect(premiumRequests({})).toBeNull();
  });

  it("reads the AI credits, in nano AIU", () => {
    expect(aiCredits({ totalNanoAiu: 2_500_000_000 })).toBe(2.5);
    expect(aiCredits({ copilotUsage: { totalNanoAiu: 1_000_000_000 } })).toBe(1);
    expect(aiCredits({ modelMetrics: { a: { totalNanoAiu: 1_000_000_000 }, b: { totalNanoAiu: 500_000_000 } } })).toBe(1.5);
    expect(aiCredits(usage)).toBeNull();
    expect(aiCredits(null)).toBeNull();
  });

  it("labels a call by its model, else the CLI default that answered", () => {
    expect(modelLabel("gpt-5", usage)).toBe("gpt-5");
    expect(modelLabel(null, usage)).toBe("claude-sonnet-4.5 (default)");
    expect(modelLabel(null, null)).toBe("default");
  });
});
