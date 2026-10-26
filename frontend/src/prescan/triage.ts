import type { TriageLabel } from "../api";
import { itemsKey } from "../backlog/ImportPanel";

// Under the items key, so anything that refreshes the backlog refreshes the counts too.
export const prescanSummaryKey = (workspaceId: number) => [...itemsKey(workspaceId), "prescanSummary"];

export const TRIAGE_TEXT: Record<TriageLabel, string> = {
  error: "Error",
  unmapped: "Unmapped",
  unknown: "Unknown",
  conflicts: "Conflicts",
  partial: "Partially present",
  "3way": "3-way merge",
  applies: "Applies",
  likely_present: "Likely present",
  present: "Present",
};

export type TriageTone = "error" | "warn" | "info" | "ok";

export function triageTone(label: TriageLabel): TriageTone {
  if (label === "error" || label === "conflicts") return "error";
  if (label === "present") return "ok";
  if (label === "applies") return "info";
  return "warn";
}

export function triageClass(label: TriageLabel): string {
  const tone = triageTone(label);
  return tone === "info" ? "tag" : `tag tag-${tone}`;
}
