import type { Review, ReworkAttempt, ReworkStatus, Rung } from "../api";
import { itemsKey } from "../backlog/ImportPanel";

export const reworkJobsKey = (workspaceId: number) => ["reworkJobs", workspaceId];
// Under the items key, so anything that refreshes the backlog refreshes the counts too.
export const reworkSummaryKey = (workspaceId: number) => [...itemsKey(workspaceId), "reworkSummary"];
export const reworkAttemptsKey = (workspaceId: number, key?: string) =>
  key === undefined ? ["reworkAttempts", workspaceId] : ["reworkAttempts", workspaceId, key];

export const REWORK_TEXT: Record<ReworkStatus, string> = {
  rework_failed: "Rework failed",
  proposed: "Proposed",
  rejected: "Rejected",
  accepted: "Accepted",
};

export function reworkTone(status: ReworkStatus): "error" | "warn" | "ok" | "info" {
  if (status === "accepted") return "ok";
  if (status === "rework_failed") return "error";
  if (status === "rejected") return "warn";
  return "info";
}

/** A proposed patch that passed the rework's build is light green; one left unbuilt stays neutral. */
export function reworkClass(status: ReworkStatus, built = false): string {
  if (status === "proposed" && built) return "tag tag-built";
  const tone = reworkTone(status);
  return tone === "info" ? "tag" : `tag tag-${tone}`;
}

export const REVIEW_TEXT: Record<Review, string> = { proposed: "proposed", accepted: "accepted", rejected: "rejected" };

export function reviewClass(review: Review): string {
  return review === "accepted" ? "tag tag-ok" : review === "rejected" ? "tag tag-warn" : "tag";
}

export const RUNG_TEXT: Record<Rung, { name: string; hint: string }> = {
  resolve: { name: "Resolve conflicts", hint: "Copilot resolves the conflict markers a 3-way merge left" },
  port: { name: "Port rejected hunks", hint: "Copilot ports the hunks that found no base into the current files" },
  reimpl: { name: "Re-implement", hint: "Copilot re-implements the change from intent: file moved, rewritten or gone" },
};

/** Rungs as recorded in an attempt, the Copilot-free ones too. */
export const ATTEMPT_RUNG_TEXT: Record<string, string> = {
  stack: "stack",
  source3way: "source base",
  resolve: "resolve",
  port: "port",
  reimpl: "re-implement",
};

export const ATTEMPT_TEXT: Record<ReworkAttempt["status"], string> = {
  running: "Running",
  patched: "Patched",
  unchanged: "Nothing new",
  failed: "Failed",
  canceled: "Canceled",
  error: "Error",
};
