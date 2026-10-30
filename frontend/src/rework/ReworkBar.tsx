import { useQuery } from "@tanstack/react-query";

import { api, REWORK_STATUSES } from "../api";
import { REWORK_TEXT, reworkSummaryKey, reworkTone } from "./rework";

type Update = (change: (next: URLSearchParams) => void) => void;

interface Props {
  workspaceId: number;
  search: URLSearchParams;
  update: Update;
}

/** Items per rework status; a click shows only that status, a second click clears it. */
export function ReworkBar({ workspaceId, search, update }: Props) {
  const summary = useQuery({ queryKey: reworkSummaryKey(workspaceId), queryFn: () => api.reworkSummary(workspaceId) });
  const data = summary.data;
  if (!data) return null;
  const counts = REWORK_STATUSES.filter((status) => data.statuses[status] > 0);
  // Nothing reworked yet: the bar would only say so.
  if (counts.length === 0) return null;
  const rework = search.getAll("rework");

  function pick(status: string) {
    const active = rework.length === 1 && rework[0] === status;
    update((next) => {
      next.delete("rework");
      if (!active) next.set("rework", status);
    });
  }

  return (
    <div className="triage-bar" role="group" aria-label="Rework counts">
      {counts.map((status) => (
        <button
          key={status}
          type="button"
          className={`chip chip-${reworkTone(status)}`}
          aria-pressed={rework.length === 1 && rework[0] === status}
          onClick={() => pick(status)}
        >
          <strong>{data.statuses[status]}</strong> {REWORK_TEXT[status].toLowerCase()}
        </button>
      ))}
    </div>
  );
}
