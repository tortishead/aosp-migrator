import { useQuery } from "@tanstack/react-query";

import { api, TRIAGE_LABELS } from "../api";
import { prescanSummaryKey, TRIAGE_TEXT, triageTone } from "./triage";

type Update = (change: (next: URLSearchParams) => void) => void;

interface Props {
  workspaceId: number;
  search: URLSearchParams;
  update: Update;
}

/** Items per pre-scan label; a click shows only that label, a second click clears it. */
export function TriageBar({ workspaceId, search, update }: Props) {
  const summary = useQuery({ queryKey: prescanSummaryKey(workspaceId), queryFn: () => api.prescanSummary(workspaceId) });
  const data = summary.data;
  if (!data) return null;
  // Each chip in the colour of its label's tag in the table; "not scanned" stays neutral.
  const counts: [string, string, number, string][] = TRIAGE_LABELS.filter((label) => data.labels[label] > 0).map((label) => [
    label,
    TRIAGE_TEXT[label],
    data.labels[label],
    `chip chip-${triageTone(label)}`,
  ]);
  // Nothing scanned yet: the bar would only say so.
  if (counts.length === 0) return null;
  if (data.unlabeled > 0) counts.push(["none", "not scanned", data.unlabeled, "chip"]);

  const triage = search.getAll("triage");
  const staleOnly = search.get("stale") === "true";

  function pick(label: string) {
    const active = triage.length === 1 && triage[0] === label;
    update((next) => {
      next.delete("triage");
      if (!active) next.set("triage", label);
    });
  }

  return (
    <div className="triage-bar" role="group" aria-label="Pre-scan counts">
      {counts.map(([label, text, n, className]) => (
        <button
          key={label}
          type="button"
          className={className}
          aria-pressed={triage.length === 1 && triage[0] === label}
          onClick={() => pick(label)}
        >
          <strong>{n}</strong> {text.toLowerCase()}
        </button>
      ))}
      {data.stale > 0 && (
        <button
          type="button"
          className="chip chip-warn"
          aria-pressed={staleOnly}
          onClick={() =>
            update((next) => {
              if (staleOnly) next.delete("stale");
              else next.set("stale", "true");
            })
          }
        >
          <strong>{data.stale}</strong> stale
        </button>
      )}
    </div>
  );
}
