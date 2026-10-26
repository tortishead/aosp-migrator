import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, fetchText, type BuildRun } from "../api";
import { BUILD_TEXT, buildClass, itemBuildsKey } from "./build";

/** Every build run that included the item, newest first, each with its full log on demand. */
export function BuildRuns({ workspaceId, itemKey }: { workspaceId: number; itemKey: string }) {
  const runs = useQuery({ queryKey: itemBuildsKey(workspaceId, itemKey), queryFn: () => api.itemBuilds(workspaceId, itemKey) });
  const [openLog, setOpenLog] = useState<number | null>(null);

  if (runs.isPending) return <p>Loading…</p>;
  if (runs.isError) return <p className="error">{runs.error.message}</p>;
  if (runs.data.length === 0) return <p className="hint">Not built yet.</p>;

  return (
    <table className="table build-runs" aria-label="Build runs">
      <thead>
        <tr>
          <th>Started</th>
          <th>Status</th>
          <th>Modules</th>
          <th>Summary</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {runs.data.map((run) => (
          <RunRow
            key={run.id}
            workspaceId={workspaceId}
            itemKey={itemKey}
            run={run}
            logOpen={openLog === run.id}
            toggleLog={() => setOpenLog(openLog === run.id ? null : run.id)}
          />
        ))}
      </tbody>
    </table>
  );
}

interface RowProps {
  workspaceId: number;
  itemKey: string;
  run: BuildRun;
  logOpen: boolean;
  toggleLog: () => void;
}

function RunRow({ workspaceId, itemKey, run, logOpen, toggleLog }: RowProps) {
  const others = run.keys.filter((k) => k !== itemKey);
  return (
    <>
      <tr>
        <td className="nowrap">{new Date(run.started_at).toLocaleString()}</td>
        <td className="nowrap">
          <span className={buildClass(run.status)}>{BUILD_TEXT[run.status]}</span>
          {others.length > 0 && <div className="hint">with {others.join(", ")}</div>}
        </td>
        <td className="mono" title={run.command ?? undefined}>
          {run.tool ? `${run.tool} ${run.modules.join(" ")}` : "—"}
        </td>
        <td>{run.summary && <pre className="build-summary">{run.summary}</pre>}</td>
        <td className="nowrap">
          {run.log_url && (
            <button className="link-button" aria-expanded={logOpen} onClick={toggleLog}>
              {logOpen ? "Hide log" : "Full log"}
            </button>
          )}
        </td>
      </tr>
      {logOpen && run.log_url && (
        <tr>
          <td colSpan={5}>
            <FullLog workspaceId={workspaceId} run={run} />
          </td>
        </tr>
      )}
    </>
  );
}

function FullLog({ workspaceId, run }: { workspaceId: number; run: BuildRun }) {
  const log = useQuery({
    queryKey: ["buildLog", workspaceId, run.id, run.status],
    queryFn: () => fetchText(run.log_url!),
  });
  if (log.isPending) return <p>Loading log…</p>;
  if (log.isError) return <p className="error">{log.error.message}</p>;
  return (
    <pre className="build-log" aria-label={`Log of build ${run.id}`}>
      {log.data}
    </pre>
  );
}
