import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link } from "react-router";

import { api, type PrescanJob } from "../api";
import { POLL_MS } from "../backlog/ImportPanel";

export const prescanJobsKey = (workspaceId: number) => ["prescanJobs", workspaceId];
export const itemPrescanKey = (workspaceId: number, key?: string) =>
  key === undefined ? ["itemPrescan", workspaceId] : ["itemPrescan", workspaceId, key];

/** Start a pre-scan (all, or the given keys); whoever shows the latest job picks it up and polls it. */
export function useStartPrescan(workspaceId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (keys?: string[]) => api.startPrescan(workspaceId, keys),
    onSuccess: (job) => queryClient.setQueryData(prescanJobsKey(workspaceId), [job]),
  });
}

/** The workspace's latest pre-scan job, polled while it runs. Results are refetched as it moves. */
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
  }, [progress, queryClient, workspaceId]);
  return job;
}

export function PrescanPanel({ workspaceId }: { workspaceId: number }) {
  const queryClient = useQueryClient();
  const job = useLatestPrescan(workspaceId);
  const start = useStartPrescan(workspaceId);
  const cancel = useMutation({
    mutationFn: api.cancelPrescan,
    onSuccess: (job) => queryClient.setQueryData(prescanJobsKey(workspaceId), [job]),
  });
  const running = job?.status === "running";

  return (
    <section className="import-panel" aria-label="Pre-scan">
      <div className="actions">
        <button onClick={() => start.mutate(undefined)} disabled={running || start.isPending}>
          Pre-scan backlog
        </button>
        {running && (
          <button onClick={() => cancel.mutate(job.id)} disabled={job.cancel_requested || cancel.isPending}>
            Cancel pre-scan
          </button>
        )}
        {job && <PrescanStatus job={job} />}
      </div>
      {start.isError && <p className="error">{start.error.message}</p>}
      {cancel.isError && <p className="error">{cancel.error.message}</p>}
      {job && job.errors.length > 0 && (
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

export function PrescanStatus({ job }: { job: PrescanJob }) {
  const when = job.finished_at ? ` on ${new Date(job.finished_at).toLocaleString()}` : "";
  const items = (n: number) => `${n} ${n === 1 ? "item" : "items"}`;
  switch (job.status) {
    case "running":
      return (
        <span className="job-status" role="status">
          <progress value={job.done} max={job.total ?? undefined} aria-label="Pre-scan progress" />
          {job.stage === "projects"
            ? "Pre-scan: finding target projects…"
            : job.stage === "graph"
              ? "Pre-scan: building dependency graph…"
              : `Pre-scanning… ${job.done} / ${job.total ?? "?"}`}
          {job.cancel_requested && " (canceling)"}
        </span>
      );
    case "done":
      return (
        <span className="job-status" role="status">
          Pre-scan done: {items(job.done)}
          {when}
        </span>
      );
    case "canceled":
      return (
        <span className="job-status" role="status">
          Pre-scan canceled after {job.done} of {job.total ?? "?"}
          {when}
        </span>
      );
    case "failed":
      return (
        <span className="job-status error" role="status">
          Pre-scan failed: {job.error}
        </span>
      );
  }
}
