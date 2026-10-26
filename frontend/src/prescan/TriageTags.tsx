import type { Item } from "../api";
import { TRIAGE_TEXT, triageClass } from "./triage";

/** Backlog pre-scan column: the triage label (the apply result whenever the diffs were apply-checked) and a stale marker. */
export function TriageTags({ item }: { item: Item }) {
  return (
    <>
      {item.triage_label && (
        <span className={triageClass(item.triage_label)} title={item.triage_reason ?? undefined}>
          {TRIAGE_TEXT[item.triage_label]}
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
