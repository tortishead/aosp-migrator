import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link } from "react-router";

import { api, type BuildJob } from "../api";
import { itemsKey, POLL_MS } from "../backlog/ImportPanel";
import { buildJobsKey, itemBuildsKey } from "./build";

/** Start a build (items, or a cluster); whoever shows the latest job picks it up and polls it. */
export function useStartBuild(workspaceId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (what: { keys: string[] } | { cluster_id: number }) => api.startBuild(workspaceId, what),
    onSuccess: (job) => queryClient.setQueryData(buildJobsKey(workspaceId), [job]),
  });
}

/** The workspace's latest build job, polled while it runs. Build results, runs and history are refetched as units finish. */
export function useLatestBuild(workspaceId: number) {
  const queryClient = useQueryClient();
  const jobs = useQuery({
    queryKey: buildJobsKey(workspaceId),
    queryFn: () => api.buildJobs(workspaceId, 1),
    refetchInterval: (query) => (query.state.data?.[0]?.status === "running" ? POLL_MS : false),
  });
  const job = jobs.data?.[0];
  const progress = job ? `${job.id}:${job.status}:${job.done}:${job.unit_keys?.join(",")}` : "";
  const seen = useRef(progress);
  useEffect(() => {
    if (progress === seen.current) return;
    seen.current = progress;
    void queryClient.invalidateQueries({ queryKey: itemBuildsKey(workspaceId) });
    void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
    // Item pages, for the build events in their history.
    void queryClient.invalidateQueries({ queryKey: ["item", workspaceId] });
  }, [progress, queryClient, workspaceId]);
  return job;
}

/** The latest build job: progress, the building unit's log tail, cancel, skipped items and unit errors. Nothing before the first build. */
export function BuildPanel({ workspaceId }: { workspaceId: number }) {
  const queryClient = useQueryClient();
  const job = useLatestBuild(workspaceId);
  const cancel = useMutation({
    mutationFn: api.cancelBuild,
    onSuccess: (job) => queryClient.setQueryData(buildJobsKey(workspaceId), [job]),
  });
  if (!job) return null;
  const running = job.status === "running";
  const items = (n: number) => `${n} ${n === 1 ? "item" : "items"}`;

  return (
    <section className="import-panel" aria-label="Build">
      <div className="actions">
        {running && (
          <button onClick={() => cancel.mutate(job.id)} disabled={job.cancel_requested || cancel.isPending}>
            Cancel build
          </button>
        )}
        <BuildStatus job={job} />
      </div>
      {cancel.isError && <p className="error">{cancel.error.message}</p>}
      {job.log_tail && (
        <details className="import-errors build-tail">
          <summary>{running ? "Live log" : "Log tail"}</summary>
          <pre aria-label="Build log tail">{job.log_tail}</pre>
        </details>
      )}
      {job.skipped.length > 0 && (
        <details className="import-errors">
          <summary>{items(job.skipped.length)} not built</summary>
          <ul>
            {job.skipped.map((s) => (
              <li key={s.key}>
                <Link to={`/workspaces/${workspaceId}/items/${s.key}`}>{s.key}</Link>: {s.reason}
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

export function buildStatusText(job: BuildJob): string {
  const units = (n: number) => `${n} ${n === 1 ? "unit" : "units"}`;
  switch (job.status) {
    case "running":
      return `Building ${job.unit_keys?.join(", ") ?? "…"} (${job.done} / ${job.total})` + (job.cancel_requested ? " (canceling)" : "");
    case "done":
      return job.total === 0 ? "Build: nothing eligible" : `Build done: ${units(job.done)}`;
    case "canceled":
      return `Build canceled after ${job.done} of ${units(job.total)}`;
    case "failed":
      return `Build failed: ${job.error}`;
  }
}

function BuildStatus({ job }: { job: BuildJob }) {
  const when = job.finished_at ? `Finished ${new Date(job.finished_at).toLocaleString()}` : undefined;
  return (
    <span className={job.status === "failed" ? "job-status error" : "job-status"} role="status" title={when}>
      {job.status === "running" && <progress value={job.done} max={job.total} aria-label="Build progress" />}
      {buildStatusText(job)}
    </span>
  );
}
