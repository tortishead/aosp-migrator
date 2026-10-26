import type { BuildRun } from "../api";
import { itemsKey } from "../backlog/ImportPanel";

export const buildJobsKey = (workspaceId: number) => ["buildJobs", workspaceId];
// Under the items key, so anything that refreshes the backlog refreshes the counts too.
export const buildSummaryKey = (workspaceId: number) => [...itemsKey(workspaceId), "buildSummary"];
export const itemBuildsKey = (workspaceId: number, key?: string) =>
  key === undefined ? ["itemBuilds", workspaceId] : ["itemBuilds", workspaceId, key];

export const BUILD_TEXT: Record<BuildRun["status"], string> = {
  apply_failed: "Apply failed",
  build_failed: "Build failed",
  build_successful: "Built",
  running: "Building",
  canceled: "Canceled",
  interrupted: "Interrupted",
  error: "Error",
};

export function buildTone(status: BuildRun["status"]): "error" | "warn" | "ok" | "info" {
  if (status === "build_successful") return "ok";
  if (status === "apply_failed" || status === "build_failed" || status === "error") return "error";
  if (status === "running") return "info";
  return "warn";
}

export function buildClass(status: BuildRun["status"]): string {
  const tone = buildTone(status);
  return tone === "info" ? "tag" : `tag tag-${tone}`;
}
