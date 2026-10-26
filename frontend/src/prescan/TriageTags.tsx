import type { Item, TriageLabel } from "../api";
import { TRIAGE_TEXT, triageClass } from "./triage";

// Labels saying the diffs are (partly) in the target already; an apply result says more.
const PRESENT_LABELS = new Set<TriageLabel>(["partial", "likely_present", "present"]);

/** Backlog pre-scan column: one verdict tag and a stale marker.
 *
 * The verdict is the triage label, or the apply result in place of a present label. Never both.
 */
export function TriageTags({ item }: { item: Item }) {
  const { triage_label: label, apply } = item;
  const shown = label && apply && PRESENT_LABELS.has(label) ? apply : label;
  return (
    <>
      {shown && (
        <span className={triageClass(shown)} title={item.triage_reason ?? undefined}>
          {TRIAGE_TEXT[shown]}
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
