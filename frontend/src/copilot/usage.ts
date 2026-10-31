/** What the Copilot CLI's `--usage-output-file` says about one model it called. */
export interface ModelUsage {
  model: string;
  /** Model API calls: one per tool round. */
  requests: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** The models that served the call, from `modelMetrics`: with no model set, that is the CLI's default. Empty when the CLI wrote no usage. */
export function modelUsage(usage: Record<string, unknown> | null | undefined): ModelUsage[] {
  return Object.entries(record(usage?.modelMetrics)).map(([model, metrics]) => {
    const m = record(metrics);
    const tokens = record(m.usage);
    return { model, requests: num(record(m.requests).count), inputTokens: num(tokens.inputTokens), outputTokens: num(tokens.outputTokens) };
  });
}

/** Premium requests the call cost, when the CLI reported it. */
export function premiumRequests(usage: Record<string, unknown> | null | undefined): number | null {
  return num(usage?.totalPremiumRequestCost);
}

/** The model a call asked for, else the ones that answered it (the CLI's default), else "default". */
export function modelLabel(model: string | null | undefined, usage: Record<string, unknown> | null | undefined): string {
  if (model) return model;
  const served = modelUsage(usage).map((u) => u.model);
  return served.length ? `${served.join(", ")} (default)` : "default";
}
