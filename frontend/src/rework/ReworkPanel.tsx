import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Link } from "react-router";

import { api, type ReworkJob, type ReworkPhase, type ReworkRequest } from "../api";
import { itemsKey, POLL_MS } from "../backlog/ImportPanel";
import { clock, LiveCall, useTick } from "./LiveCall";
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
      {running && <RunningRework job={job} />}
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
      {job.status === "running" && <span className="spinner" aria-hidden="true" />}
      {reworkStatusText(job)}
    </span>
  );
}

const STEPS: { phase: ReworkPhase; label: string }[] = [
  { phase: "patching", label: "Make the patch" },
  { phase: "building", label: "Build" },
  { phase: "fixing", label: "Fix the build" },
  { phase: "saving", label: "Save" },
];

// How far into a unit each phase is, for the bar; a build-fix round goes back to building, a little further on.
const PHASE_SHARE: Record<ReworkPhase, number> = { patching: 0.15, building: 0.5, fixing: 0.7, saving: 0.92 };

/** Units done plus how far the current one is, as a share of the job. */
export function reworkProgress(job: ReworkJob): number {
  if (!job.total) return 0;
  const rounds = Math.max(0, job.builds - 1);
  const share = job.phase ? Math.min(0.95, PHASE_SHARE[job.phase] + (job.phase === "building" ? rounds * 0.05 : 0)) : 0;
  return Math.min(1, (job.done + share) / job.total);
}

/** While the job runs: an animated bar, the unit's steps, a ticking job timer and the Copilot call running now. */
function RunningRework({ job }: { job: ReworkJob }) {
  const now = useTick(true);
  const progress = reworkProgress(job);
  const current = STEPS.findIndex((s) => s.phase === job.phase);
  const elapsed = (now - new Date(job.started_at).getTime()) / 1000;
  return (
    <div className="rework-live">
      <div
        className="rework-bar running"
        role="progressbar"
        aria-label="Rework progress"
        aria-valuemin={0}
        aria-valuemax={job.total}
        aria-valuenow={job.done}
        aria-valuetext={`${job.done} of ${job.total} units done`}
      >
        <div className="rework-bar-fill" style={{ width: `${Math.max(3, progress * 100)}%` }} />
      </div>
      <div className="actions">
        <ol className="rework-steps" aria-label="Rework steps">
          {STEPS.map((step, i) => (
            <li key={step.phase} className={i === current ? "active" : current > i ? "done" : undefined} aria-current={i === current ? "step" : undefined}>
              {step.label}
              {step.phase === "building" && job.builds > 1 && ` (${job.builds})`}
            </li>
          ))}
        </ol>
        <span className="rework-elapsed" aria-label="Rework time">
          {clock(elapsed)}
        </span>
      </div>
      {job.copilot_call && <LiveCall call={job.copilot_call} />}
    </div>
  );
}
