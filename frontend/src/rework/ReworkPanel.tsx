import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link } from "react-router";

import { api, type ReworkJob, type ReworkRequest } from "../api";
import { itemsKey, POLL_MS } from "../backlog/ImportPanel";
import { reworkAttemptsKey, reworkJobsKey } from "./rework";

/** Start a rework; whoever shows the latest job picks it up and polls it. */
export function useStartRework(workspaceId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: ReworkRequest) => api.startRework(workspaceId, request),
    onSuccess: (job) => queryClient.setQueryData(reworkJobsKey(workspaceId), [job]),
  });
}

/** The workspace's latest rework job, polled while it runs. Patches, attempts, backlog and history are refetched as units finish. */
export function useLatestRework(workspaceId: number) {
  const queryClient = useQueryClient();
  const jobs = useQuery({
    queryKey: reworkJobsKey(workspaceId),
    queryFn: () => api.reworkJobs(workspaceId, 1),
    refetchInterval: (query) => (query.state.data?.[0]?.status === "running" ? POLL_MS : false),
  });
  const job = jobs.data?.[0];
  const progress = job ? `${job.id}:${job.status}:${job.done}:${job.unit_keys?.join(",")}` : "";
  const seen = useRef(progress);
  useEffect(() => {
    if (progress === seen.current) return;
    seen.current = progress;
    void queryClient.invalidateQueries({ queryKey: reworkAttemptsKey(workspaceId) });
    void queryClient.invalidateQueries({ queryKey: ["itemPatches", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: ["itemBuilds", workspaceId] });
    void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
    // Item pages, for the rework events in their history.
    void queryClient.invalidateQueries({ queryKey: ["item", workspaceId] });
  }, [progress, queryClient, workspaceId]);
  return job;
}

/** The latest rework job: progress, Copilot calls, cancel, items not reworked and unit errors. */
export function ReworkPanel({ workspaceId }: { workspaceId: number }) {
  const queryClient = useQueryClient();
  const job = useLatestRework(workspaceId);
  const cancel = useMutation({
    mutationFn: api.cancelRework,
    onSuccess: (job) => queryClient.setQueryData(reworkJobsKey(workspaceId), [job]),
  });
  if (!job) return <p className="hint">No rework yet.</p>;
  const running = job.status === "running";
  const items = (n: number) => `${n} ${n === 1 ? "item" : "items"}`;

  return (
    <section className="import-panel" aria-label="Rework">
      <div className="actions">
        {running && (
          <button onClick={() => cancel.mutate(job.id)} disabled={job.cancel_requested || cancel.isPending}>
            Cancel rework
          </button>
        )}
        <ReworkStatusLine job={job} />
      </div>
      {cancel.isError && <p className="error">{cancel.error.message}</p>}
      {job.log_tail && (
        <details className="import-errors build-tail">
          <summary>{job.phase === "building" ? "Live build log" : "Build log tail"}</summary>
          <pre aria-label="Rework build log tail">{job.log_tail}</pre>
        </details>
      )}
      {job.skipped.length > 0 && (
        <details className="import-errors">
          <summary>{items(job.skipped.length)} not reworked</summary>
          <ul>
            {job.skipped.map((s) => (
              <li key={s.key}>
                <Link to={`/workspaces/${workspaceId}/items/${s.key}?tab=migration`}>{s.key}</Link>: {s.reason ?? "not reworked"}
              </li>
            ))}
          </ul>
        </details>
      )}
      {job.errors.length > 0 && (
        <details className="import-errors">
          <summary>
            {job.errors.length} {job.errors.length === 1 ? "unit" : "units"} failed unexpectedly
          </summary>
          <ul>
            {job.errors.map((e, i) => (
              <li key={i}>
                {e.keys.join(", ")}: {e.message}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export function reworkStatusText(job: ReworkJob): string {
  const units = (n: number) => `${n} ${n === 1 ? "unit" : "units"}`;
  const calls = `${job.calls} Copilot ${job.calls === 1 ? "call" : "calls"}`;
  switch (job.status) {
    case "running":
      return (
        `Reworking ${job.unit_keys?.join(", ") ?? "…"} (${job.done} / ${job.total}, ${calls})` +
        (job.phase ? `: ${phaseText(job)}` : "") +
        (job.cancel_requested ? " (canceling)" : "")
      );
    case "done":
      return job.total === 0 ? "Rework: nothing to rework" : `Rework done: ${units(job.done)}, ${calls}`;
    case "canceled":
      return `Rework canceled after ${job.done} of ${units(job.total)}`;
    case "failed":
      return `Rework failed: ${job.error}`;
  }
}

function phaseText(job: ReworkJob): string {
  switch (job.phase) {
    case "building":
      return job.builds > 1 ? `building again (after build-fix round ${job.builds - 1})` : "building";
    case "fixing":
      return `fixing the build with Copilot (round ${job.builds})`;
    case "saving":
      return "saving the patch";
    default:
      return "making the patch";
  }
}

function ReworkStatusLine({ job }: { job: ReworkJob }) {
  const when = job.finished_at ? `Finished ${new Date(job.finished_at).toLocaleString()}` : undefined;
  return (
    <span className={job.status === "failed" ? "job-status error" : "job-status"} role="status" title={when}>
      {job.status === "running" && <progress value={job.done} max={job.total} aria-label="Rework progress" />}
      {reworkStatusText(job)}
    </span>
  );
}
