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

export function ImportPanel({ workspaceId }: { workspaceId: number }) {
  const queryClient = useQueryClient();
  const jobs = useQuery({
    queryKey: importJobsKey(workspaceId),
    queryFn: () => api.importJobs(workspaceId, 1),
    refetchInterval: (query) => (query.state.data?.[0]?.status === "running" ? POLL_MS : false),
  });
  const start = useStartImport(workspaceId);
  const cancel = useMutation({
    mutationFn: api.cancelImport,
    onSuccess: (job) => queryClient.setQueryData(importJobsKey(workspaceId), [job]),
  });
  const job = jobs.data?.[0];
  const running = job?.status === "running";

  // Items land one by one while a job runs: refresh the backlog whenever progress moves.
  const progress = job ? `${job.id}:${job.status}:${job.done}` : "";
  const seen = useRef(progress);
  useEffect(() => {
    if (progress === seen.current) return;
    seen.current = progress;
    void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
    void queryClient.invalidateQueries({ queryKey: facetsKey(workspaceId) });
  }, [progress, queryClient, workspaceId]);

  return (
    <section className="import-panel" aria-label="Import">
      <div className="actions">
        <button onClick={() => start.mutate(undefined)} disabled={running || start.isPending}>
          Import from Jira
        </button>
        {running && (
          <button onClick={() => cancel.mutate(job.id)} disabled={job.cancel_requested || cancel.isPending}>
            Cancel import
          </button>
        )}
        {job && <JobSummary job={job} />}
      </div>
      {start.isError && <p className="error">{start.error.message}</p>}
      {cancel.isError && <p className="error">{cancel.error.message}</p>}
      {job && job.errors.length > 0 && (
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

function JobSummary({ job }: { job: ImportJob }) {
  const what = job.scope === "all" ? "Import" : "Reimport";
  // The finish time only in the tooltip: it would double the line in the backlog header.
  const when = job.finished_at ? `Finished ${new Date(job.finished_at).toLocaleString()}` : undefined;
  switch (job.status) {
    case "running":
      return (
        <span className="job-status" role="status">
          <progress value={job.done} max={job.total ?? undefined} aria-label="Import progress" />
          {what}ing… {job.done} / {job.total ?? "?"}
          {job.rate_limit_wait !== null && ` (Jira rate limit, resuming in ${job.rate_limit_wait}s)`}
          {job.cancel_requested && " (canceling)"}
        </span>
      );
    case "done":
      return (
        <span className="job-status" role="status" title={when}>
          {what} done: {job.done} {job.done === 1 ? "ticket" : "tickets"}
        </span>
      );
    case "canceled":
      return (
        <span className="job-status" role="status" title={when}>
          {what} canceled after {job.done} of {job.total ?? "?"}
        </span>
      );
    case "failed":
      return (
        <span className="job-status error" role="status">
          {what} failed: {job.error}
        </span>
      );
  }
}
