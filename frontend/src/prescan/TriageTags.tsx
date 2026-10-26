import type { Item, TriageLabel } from "../api";
import { TRIAGE_TEXT, triageClass } from "./triage";

// Labels saying the diffs are (partly) in the target already; an apply result says more.
const PRESENT_LABELS = new Set<TriageLabel>(["partial", "likely_present", "present"]);

/** Backlog pre-scan column: the triage label, the apply result when it says more, and a stale marker. */
export function TriageTags({ item }: { item: Item }) {
  const { triage_label: label, apply } = item;
  return (
    <>
      {label && !(apply && PRESENT_LABELS.has(label)) && (
        <span className={triageClass(label)} title={item.triage_reason ?? undefined}>
          {TRIAGE_TEXT[label]}
        </span>
      )}
      {apply && apply !== label && (
        <span className={triageClass(apply)} title="Apply check of the diffs not already in the target">
          {TRIAGE_TEXT[apply]}
        </span>
      )}
      {item.stale.length > 0 && (
        <span className="tag tag-warn" title={`Out of date: ${item.stale.join("; ")}`}>
          stale
        </span>
      )}
    </>
  );
}
