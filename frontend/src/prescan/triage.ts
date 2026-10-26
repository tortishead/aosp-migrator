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

// Labels saying the diffs are (partly) in the target already; an apply result says more.
export const PRESENT_LABELS: TriageLabel[] = ["partial", "likely_present", "present"];

export function triageClass(label: TriageLabel): string {
  if (label === "error" || label === "conflicts") return "tag tag-error";
  if (label === "applies" || label === "present") return "tag";
  return "tag tag-warn";
}
