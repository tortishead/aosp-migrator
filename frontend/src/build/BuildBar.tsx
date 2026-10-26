import { useQuery } from "@tanstack/react-query";

import { api, BUILD_STATUSES } from "../api";
import { BUILD_TEXT, buildSummaryKey, buildTone } from "./build";

type Update = (change: (next: URLSearchParams) => void) => void;

interface Props {
  workspaceId: number;
  search: URLSearchParams;
  update: Update;
}

/** Items per build status; a click shows only that status, a second click clears it. */
export function BuildBar({ workspaceId, search, update }: Props) {
  const summary = useQuery({ queryKey: buildSummaryKey(workspaceId), queryFn: () => api.buildSummary(workspaceId) });
  const data = summary.data;
  if (!data) return null;
  const counts: [string, string, number, string][] = BUILD_STATUSES.filter((status) => data.statuses[status] > 0).map((status) => [
    status,
    BUILD_TEXT[status],
    data.statuses[status],
    `chip chip-${buildTone(status)}`,
  ]);
  // Nothing built yet: the bar would only say so.
  if (counts.length === 0) return null;
  if (data.unbuilt > 0) counts.push(["none", "not built", data.unbuilt, "chip"]);

  const build = search.getAll("build");

  function pick(status: string) {
    const active = build.length === 1 && build[0] === status;
    update((next) => {
      next.delete("build");
      if (!active) next.set("build", status);
    });
  }

  return (
    <div className="triage-bar" role="group" aria-label="Build counts">
      {counts.map(([status, text, n, className]) => (
        <button
          key={status}
          type="button"
          className={className}
          aria-pressed={build.length === 1 && build[0] === status}
          onClick={() => pick(status)}
        >
          <strong>{n}</strong> {text.toLowerCase()}
        </button>
      ))}
    </div>
  );
}
