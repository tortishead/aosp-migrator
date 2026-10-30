import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, fetchText, type ReworkAttempt } from "../api";
import { BUILD_TEXT, buildClass } from "../build/build";
import { ATTEMPT_RUNG_TEXT, ATTEMPT_TEXT, reworkAttemptsKey } from "./rework";

export function useReworkAttempts(workspaceId: number, itemKey: string) {
  return useQuery({ queryKey: reworkAttemptsKey(workspaceId, itemKey), queryFn: () => api.reworkAttempts(workspaceId, itemKey) });
}

function attemptClass(status: ReworkAttempt["status"]): string {
  return status === "patched" ? "tag tag-ok" : status === "failed" || status === "error" ? "tag tag-error" : status === "running" ? "tag" : "tag tag-warn";
}

/** Every rework attempt that included the item, newest first: outcome, rungs, Copilot calls, build rounds and their logs. */
export function ReworkAttempts({ workspaceId, itemKey }: { workspaceId: number; itemKey: string }) {
  const attempts = useReworkAttempts(workspaceId, itemKey);
  if (attempts.isPending) return <p>Loading…</p>;
  if (attempts.isError) return <p className="error">{attempts.error.message}</p>;
  if (attempts.data.length === 0) return <p className="hint">Not reworked yet.</p>;
  return (
    <ul className="timeline" aria-label="Rework attempts">
      {attempts.data.map((a) => (
        <Attempt key={a.id} itemKey={itemKey} attempt={a} />
      ))}
    </ul>
  );
}

function Attempt({ itemKey, attempt }: { itemKey: string; attempt: ReworkAttempt }) {
  const others = attempt.keys.filter((k) => k !== itemKey);
  const buildRounds = Math.max(0, ...attempt.calls.map((c) => c.build_round));
  return (
    <li aria-label={`Attempt ${attempt.id}`}>
      <div className="actions">
        <span className={attemptClass(attempt.status)}>{ATTEMPT_TEXT[attempt.status]}</span>
        <span className="nowrap">{new Date(attempt.started_at).toLocaleString()}</span>
        <span>rungs: {attempt.rungs.map((r) => ATTEMPT_RUNG_TEXT[r] ?? r).join(" → ") || "none"}</span>
        <span>
          {attempt.calls.length} Copilot {attempt.calls.length === 1 ? "call" : "calls"}
        </span>
        {buildRounds > 0 && (
          <span>
            {buildRounds} build-fix {buildRounds === 1 ? "round" : "rounds"}
          </span>
        )}
        {others.length > 0 && <span className="hint">with {others.join(", ")}</span>}
      </div>
      {attempt.reason && <p className={attempt.status === "patched" ? "hint" : "error"}>{attempt.reason}</p>}
      {attempt.notes.length > 0 && (
        <ul className="hint">
          {attempt.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      <details>
        <summary>Details</summary>
        {attempt.files.length > 0 && (
          <table className="table" aria-label="Files">
            <thead>
              <tr>
                <th>Project</th>
                <th>File</th>
                <th>Result</th>
                <th>Rung</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {attempt.files.map((f, i) => (
                <tr key={i}>
                  <td className="mono">{f.project}</td>
                  <td className="mono">{f.path}</td>
                  <td>{f.status}</td>
                  <td>{f.rung ? (ATTEMPT_RUNG_TEXT[f.rung] ?? f.rung) : "—"}</td>
                  <td>{[f.error, ...f.notes].filter(Boolean).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {attempt.calls.length > 0 && (
          <table className="table" aria-label="Copilot calls">
            <thead>
              <tr>
                <th>Rung</th>
                <th>Round</th>
                <th>Model</th>
                <th>Status</th>
                <th>Time</th>
                <th>Answer</th>
              </tr>
            </thead>
            <tbody>
              {attempt.calls.map((c, i) => (
                <tr key={i}>
                  <td>{ATTEMPT_RUNG_TEXT[c.rung] ?? c.rung}</td>
                  <td>
                    {c.round === 0 ? "first" : `fix ${c.round}`}
                    {c.build_round > 0 && ` (build fix ${c.build_round})`}
                  </td>
                  <td className="mono">{c.model ?? "default"}</td>
                  <td>{c.status}</td>
                  <td className="nowrap">{c.duration.toFixed(1)} s</td>
                  <td>{c.error ? <span className="error">{c.error}</span> : <pre className="copilot-answer">{c.answer}</pre>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {attempt.build_runs.length > 0 && (
          <section aria-label="Attempt builds">
            {attempt.build_runs.map((run) => (
              <AttemptBuild key={run.id} run={run} />
            ))}
          </section>
        )}
      </details>
    </li>
  );
}

function AttemptBuild({ run }: { run: ReworkAttempt["build_runs"][number] }) {
  const [open, setOpen] = useState(false);
  const log = useQuery({ queryKey: ["buildLog", "run", run.id, run.status], queryFn: () => fetchText(run.log_url!), enabled: open && !!run.log_url });
  return (
    <div>
      <p className="actions">
        <span className={buildClass(run.status)}>{BUILD_TEXT[run.status]}</span>
        {run.log_url && (
          <button className="link-button" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide log" : "Full log"}
          </button>
        )}
      </p>
      {run.summary && <pre className="build-summary">{run.summary}</pre>}
      {open && (log.isPending ? <p>Loading log…</p> : log.isError ? <p className="error">{log.error.message}</p> : <pre className="build-log">{log.data}</pre>)}
    </div>
  );
}
