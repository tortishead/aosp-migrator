import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link } from "react-router";

import { api, type PrescanJob } from "../api";
import { itemsKey, POLL_MS } from "../backlog/ImportPanel";

export const prescanJobsKey = (workspaceId: number) => ["prescanJobs", workspaceId];
export const itemPrescanKey = (workspaceId: number, key?: string) =>
  key === undefined ? ["itemPrescan", workspaceId] : ["itemPrescan", workspaceId, key];

/**
 * Start a pre-scan (all, or the given keys; `stacked`: the keys stacked in order); whoever shows the latest job picks it up and polls it.
 */
export function useStartPrescan(workspaceId: number, stacked = false) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (keys?: string[]) => api.startPrescan(workspaceId, keys, stacked),
    onSuccess: (job) => queryClient.setQueryData(prescanJobsKey(workspaceId), [job]),
  });
}

/** The workspace's latest pre-scan job, polled while it runs. Results and backlog labels are refetched as it moves. */
export function useLatestPrescan(workspaceId: number) {
  const queryClient = useQueryClient();
  const jobs = useQuery({
    queryKey: prescanJobsKey(workspaceId),
    queryFn: () => api.prescanJobs(workspaceId, 1),
    refetchInterval: (query) => (query.state.data?.[0]?.status === "running" ? POLL_MS : false),
  });
  const job = jobs.data?.[0];
  const progress = job ? `${job.id}:${job.status}:${job.stage}:${job.done}` : "";
  const seen = useRef(progress);
  useEffect(() => {
    if (progress === seen.current) return;
    seen.current = progress;
    void queryClient.invalidateQueries({ queryKey: itemPrescanKey(workspaceId) });
    void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
    // A stacked pre-scan's results are in the items' history.
    void queryClient.invalidateQueries({ queryKey: ["item", workspaceId] });
  }, [progress, queryClient, workspaceId]);
  return job;
}

/** Starts a pre-scan of the whole backlog; the latest job's status is the tooltip. */
export function PrescanButton({ workspaceId }: { workspaceId: number }) {
  const job = useLatestPrescan(workspaceId);
  const start = useStartPrescan(workspaceId);
  return (
    <>
      <button
        onClick={() => start.mutate(undefined)}
        disabled={job?.status === "running" || start.isPending}
        title={job ? prescanStatusText(job) : "No pre-scan yet"}
      >
        Pre-scan backlog
      </button>
      {start.isError && <span className="error">{start.error.message}</span>}
    </>
  );
}

/** The latest pre-scan job: status, cancel and failed items. */
export function PrescanPanel({ workspaceId }: { workspaceId: number }) {
  const queryClient = useQueryClient();
  const job = useLatestPrescan(workspaceId);
  const cancel = useMutation({
    mutationFn: api.cancelPrescan,
    onSuccess: (job) => queryClient.setQueryData(prescanJobsKey(workspaceId), [job]),
  });
  if (!job) return <p className="hint">No pre-scan yet.</p>;
  const running = job.status === "running";

  return (
    <section className="import-panel" aria-label="Pre-scan">
      <div className="actions">
        <PrescanStatus job={job} />
        {running && (
          <button onClick={() => cancel.mutate(job.id)} disabled={job.cancel_requested || cancel.isPending}>
            Cancel pre-scan
          </button>
        )}
      </div>
      {cancel.isError && <p className="error">{cancel.error.message}</p>}
      {job.errors.length > 0 && (
        <details className="import-errors">
          <summary>
            {job.errors.length} {job.errors.length === 1 ? "item" : "items"} failed to pre-scan
          </summary>
          <ul>
            {job.errors.map((e, i) => (
              <li key={i}>
                <Link to={`/workspaces/${workspaceId}/items/${e.key}`}>{e.key}</Link>: {e.message}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export function prescanStatusText(job: PrescanJob): string {
  const items = (n: number) => `${n} ${n === 1 ? "item" : "items"}`;
  const name = job.scope === "stacked" ? "Stacked pre-scan" : "Pre-scan";
  switch (job.status) {
    case "running":
      return (
        (job.stage === "projects"
          ? `${name}: finding target projects…`
          : `${job.scope === "stacked" ? "Stacking" : "Pre-scanning"}… ${job.done} / ${job.total ?? "?"}`) +
        (job.cancel_requested ? " (canceling)" : "")
      );
    case "done":
      return `${name} done: ${items(job.done)}` + (job.scope === "stacked" ? ", results in their history" : "");
    case "canceled":
      return `${name} canceled after ${job.done} of ${job.total ?? "?"}`;
    case "failed":
      return `${name} failed: ${job.error}`;
  }
}

export function PrescanStatus({ job }: { job: PrescanJob }) {
  // The finish time only in the tooltip: it would double the line.
  const when = job.finished_at ? `Finished ${new Date(job.finished_at).toLocaleString()}` : undefined;
  return (
    <span className={job.status === "failed" ? "job-status error" : "job-status"} role="status" title={when}>
      {job.status === "running" && <progress value={job.done} max={job.total ?? undefined} aria-label="Pre-scan progress" />}
      {prescanStatusText(job)}
    </span>
  );
}
