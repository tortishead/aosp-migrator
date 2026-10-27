import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link } from "react-router";

import { api, type ImportJob } from "../api";

export const POLL_MS = 1000;

export const importJobsKey = (workspaceId: number) => ["importJobs", workspaceId];
export const itemsKey = (workspaceId: number) => ["items", workspaceId];
export const facetsKey = (workspaceId: number) => ["facets", workspaceId];

/** Start an import (all, or the given keys); the panel then picks up and polls the new job. */
export function useStartImport(workspaceId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (keys?: string[]) => api.startImport(workspaceId, keys),
    onSuccess: (job) => queryClient.setQueryData(importJobsKey(workspaceId), [job]),
  });
}

/** The workspace's latest import job, polled while it runs. Items land one by one, so the backlog is refreshed as it moves. */
export function useLatestImport(workspaceId: number) {
  const queryClient = useQueryClient();
  const jobs = useQuery({
    queryKey: importJobsKey(workspaceId),
    queryFn: () => api.importJobs(workspaceId, 1),
    refetchInterval: (query) => (query.state.data?.[0]?.status === "running" ? POLL_MS : false),
  });
  const job = jobs.data?.[0];
  const progress = job ? `${job.id}:${job.status}:${job.done}` : "";
  const seen = useRef(progress);
  useEffect(() => {
    if (progress === seen.current) return;
    seen.current = progress;
    void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
    void queryClient.invalidateQueries({ queryKey: facetsKey(workspaceId) });
  }, [progress, queryClient, workspaceId]);
  return job;
}

/** Starts a full import; the latest job's status is the tooltip. */
export function ImportButton({ workspaceId }: { workspaceId: number }) {
  const job = useLatestImport(workspaceId);
  const start = useStartImport(workspaceId);
  return (
    <>
      <button
        onClick={() => start.mutate(undefined)}
        disabled={job?.status === "running" || start.isPending}
        title={job ? importStatusText(job) : "No import yet"}
      >
        Import from Jira
      </button>
      {start.isError && <span className="error">{start.error.message}</span>}
    </>
  );
}

/** The latest import job: status, cancel and failed tickets. */
export function ImportPanel({ workspaceId }: { workspaceId: number }) {
  const queryClient = useQueryClient();
  const job = useLatestImport(workspaceId);
  const cancel = useMutation({
    mutationFn: api.cancelImport,
    onSuccess: (job) => queryClient.setQueryData(importJobsKey(workspaceId), [job]),
  });
  if (!job) return <p className="hint">No import yet.</p>;
  const running = job.status === "running";

  return (
    <section className="import-panel" aria-label="Import">
      <div className="actions">
        <JobSummary job={job} />
        {running && (
          <button onClick={() => cancel.mutate(job.id)} disabled={job.cancel_requested || cancel.isPending}>
            Cancel import
          </button>
        )}
      </div>
      {cancel.isError && <p className="error">{cancel.error.message}</p>}
      {job.errors.length > 0 && (
        <details className="import-errors">
          <summary>
            {job.errors.length} {job.errors.length === 1 ? "ticket" : "tickets"} failed
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

export function importStatusText(job: ImportJob): string {
  const what = job.scope === "all" ? "Import" : "Reimport";
  switch (job.status) {
    case "running":
      return (
        `${what}ing… ${job.done} / ${job.total ?? "?"}` +
        (job.rate_limit_wait !== null ? ` (Jira rate limit, resuming in ${job.rate_limit_wait}s)` : "") +
        (job.cancel_requested ? " (canceling)" : "")
      );
    case "done":
      return `${what} done: ${job.done} ${job.done === 1 ? "ticket" : "tickets"}`;
    case "canceled":
      return `${what} canceled after ${job.done} of ${job.total ?? "?"}`;
    case "failed":
      return `${what} failed: ${job.error}`;
  }
}

function JobSummary({ job }: { job: ImportJob }) {
  const when = job.finished_at ? `Finished ${new Date(job.finished_at).toLocaleString()}` : undefined;
  return (
    <span className={job.status === "failed" ? "job-status error" : "job-status"} role="status" title={when}>
      {job.status === "running" && <progress value={job.done} max={job.total ?? undefined} aria-label="Import progress" />}
      {importStatusText(job)}
    </span>
  );
}
