import type { Item } from "../api";
import { TRIAGE_TEXT, triageClass } from "./triage";

/** Backlog pre-scan column: the triage label (the apply result whenever the diffs were apply-checked), a stale and a dependency marker. */
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
      {item.dependencies.length > 0 && (
        <span className="tag tag-warn" title={`Needs: ${item.dependencies.join("; ")}`}>
          dependency
        </span>
      )}
    </>
  );
}
