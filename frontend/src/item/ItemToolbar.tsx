import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { api, type ItemDetail, type LocalState } from "../api";
import { facetsKey, itemsKey, POLL_MS } from "../backlog/ImportPanel";
import { STATE_LABELS } from "../backlog/params";
import { useLatestBuild, useStartBuild } from "../build/BuildPanel";
import { itemKey } from "../pages/ItemPage";
import { useLatestPrescan, useStartPrescan } from "../prescan/PrescanPanel";
import { confirmReset, RESET_TITLE, useResetItems } from "../backlog/reset";
import { ReworkDialog } from "../rework/ReworkDialog";
import { useLatestRework } from "../rework/ReworkPanel";

/** Where an action's progress shows, so starting one brings its section into view. */
export type ToolbarTab = "migration";

interface Props {
  workspaceId: number;
  detail: ItemDetail;
  onShow: (tab: ToolbarTab) => void;
}

/**
 * Everything one can do to the item, in one bar under its title: its local state; the
 * migration steps in their order (pre-scan, build, rework); and the Jira side (reimport,
 * open). Job progress stays in the sections below; starting a job switches to the tab showing it.
 */
export function ItemToolbar({ workspaceId, detail, onShow }: Props) {
  const key = detail.issue.key;
  const [reworkOpen, setReworkOpen] = useState(false);

  const prescanJob = useLatestPrescan(workspaceId);
  const prescan = useStartPrescan(workspaceId);
  const buildJob = useLatestBuild(workspaceId);
  const build = useStartBuild(workspaceId);
  const reworkJob = useLatestRework(workspaceId);
  const reimport = useReimport(workspaceId, key);
  const changeState = useChangeState(workspaceId, key);
  const reset = useResetItems(workspaceId);

  // One job of each kind at a time per workspace: the backend refuses a second.
  const prescanning = prescanJob?.status === "running" || prescan.isPending;
  const building = buildJob?.status === "running" || build.isPending;
  const reworking = reworkJob?.status === "running";

  const errors = [changeState, reset, prescan, build, reimport.start].flatMap((m) => (m.isError ? [m.error.message] : []));

  return (
    <>
      <div className="item-toolbar" role="group" aria-label="Item actions">
        <ToolbarGroup label="Triage">
          <label className="toolbar-field">
            State
            <select
              aria-label="State"
              value={detail.local_state}
              disabled={changeState.isPending}
              onChange={(e) => changeState.mutate(e.target.value as LocalState)}
            >
              {(Object.entries(STATE_LABELS) as [LocalState, string][]).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <button
            title={RESET_TITLE}
            disabled={reset.isPending || prescanning || building || reworking}
            onClick={() => confirmReset([key]) && reset.mutate([key])}
          >
            Reset
          </button>
        </ToolbarGroup>
        <ToolbarGroup label="Migration">
          <button
            title="Check whether the diffs are already in the target tree and whether they apply"
            disabled={prescanning}
            onClick={() => prescan.mutate([key], { onSuccess: () => onShow("migration") })}
          >
            {prescanJob?.status === "running" ? "Pre-scanning…" : "Pre-scan"}
          </button>
          <button
            title="Apply the item's patches to the target tree and build the touched modules"
            disabled={building}
            onClick={() => build.mutate({ keys: [key] }, { onSuccess: () => onShow("migration") })}
          >
            {buildJob?.status === "running" ? "Building…" : "Build"}
          </button>
          <button title="Rewrite conflicting patches with Copilot" disabled={reworking} onClick={() => setReworkOpen(true)}>
            Rework…
          </button>
        </ToolbarGroup>
        <ToolbarGroup label="Jira" end>
          <button onClick={() => reimport.start.mutate()} disabled={reimport.running}>
            Reimport from Jira
          </button>
          {detail.issue.browse_url && (
            <a className="toolbar-link" href={detail.issue.browse_url} target="_blank" rel="noopener noreferrer">
              Open in Jira ↗
            </a>
          )}
        </ToolbarGroup>
      </div>
      {(reimport.message || errors.length > 0) && (
        <div className="item-toolbar-status">
          {reimport.message}
          {errors.map((message) => (
            <span key={message} className="error">
              {message}
            </span>
          ))}
        </div>
      )}
      {reworkOpen && (
        <ReworkDialog
          workspaceId={workspaceId}
          keys={[key]}
          onClose={() => setReworkOpen(false)}
          onStarted={() => onShow("migration")}
        />
      )}
    </>
  );
}

function ToolbarGroup({ label, end, children }: { label: string; end?: boolean; children: ReactNode }) {
  return (
    <div className={end ? "toolbar-group toolbar-end" : "toolbar-group"} role="group" aria-label={label}>
      {children}
    </div>
  );
}

function useRefreshItem(workspaceId: number, key: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: itemKey(workspaceId, key) });
    void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
  };
}

function useChangeState(workspaceId: number, key: string) {
  const refresh = useRefreshItem(workspaceId, key);
  return useMutation({
    mutationFn: (state: LocalState) => api.changeState(workspaceId, [key], state),
    onSuccess: refresh,
  });
}

/** Reimport just this item, polling its job and reloading the item when it finishes. */
function useReimport(workspaceId: number, key: string) {
  const queryClient = useQueryClient();
  const refresh = useRefreshItem(workspaceId, key);
  const [jobId, setJobId] = useState<number | null>(null);
  const start = useMutation({ mutationFn: () => api.startImport(workspaceId, [key]), onSuccess: (job) => setJobId(job.id) });
  const job = useQuery({
    queryKey: ["importJob", jobId],
    queryFn: () => api.importJob(jobId!),
    enabled: jobId !== null,
    refetchInterval: (query) => (query.state.data?.status === "running" ? POLL_MS : false),
  });
  const data = job.data;
  const status = data?.status;
  const running = start.isPending || status === "running";

  const seen = useRef(status);
  useEffect(() => {
    if (status === seen.current) return;
    seen.current = status;
    if (status && status !== "running") {
      refresh();
      void queryClient.invalidateQueries({ queryKey: facetsKey(workspaceId) });
    }
  }, [status]);

  let message: ReactNode = null;
  if (running) message = <span role="status">Reimporting…</span>;
  else if (status === "done" && data!.errors.length === 0) message = <span role="status">Reimported.</span>;
  else if (status === "done")
    message = (
      <span role="status" className="error">
        Reimport failed: {data!.errors.map((e) => e.message).join("; ")}
      </span>
    );
  else if (status === "failed" || status === "canceled")
    message = (
      <span role="status" className="error">
        Reimport {status}
        {data!.error && `: ${data!.error}`}
      </span>
    );

  return { start, running, message };
}
