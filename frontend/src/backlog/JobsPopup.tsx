import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { BuildPanel, buildStatusText, useLatestBuild } from "../build/BuildPanel";
import { PrescanPanel, prescanStatusText, useLatestPrescan } from "../prescan/PrescanPanel";
import { ImportPanel, importStatusText, useLatestImport } from "./ImportPanel";

/** "Job status" button: the latest import, pre-scan and build in its tooltip, their full panels in a popup. */
export function JobsPopup({ workspaceId }: { workspaceId: number }) {
  const [open, setOpen] = useState(false);
  const importJob = useLatestImport(workspaceId);
  const prescanJob = useLatestPrescan(workspaceId);
  const buildJob = useLatestBuild(workspaceId);
  const jobs = [importJob, prescanJob, buildJob];
  const lines = [
    importJob && importStatusText(importJob),
    prescanJob && prescanStatusText(prescanJob),
    buildJob && buildStatusText(buildJob),
  ].filter(Boolean);
  const running = jobs.some((job) => job?.status === "running");
  const failed = jobs.some((job) => job && (job.status === "failed" || job.errors.length > 0));

  useEffect(() => {
    if (!open) return;
    const close = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [open]);

  return (
    <>
      <button
        type="button"
        className="jobs-button"
        onClick={() => setOpen(true)}
        title={lines.join("\n") || "No jobs yet"}
        aria-label={running ? "Job status running" : failed ? "Job status with errors" : "Job status"}
      >
        Job status
        {running && <span className="jobs-dot jobs-dot-running" />}
        {!running && failed && <span className="jobs-dot jobs-dot-error" />}
      </button>
      {open &&
        createPortal(
          <div className="popup-backdrop" onClick={() => setOpen(false)}>
            <div className="popup" role="dialog" aria-modal="true" aria-label="Job status" onClick={(e) => e.stopPropagation()}>
              <div className="popup-header">
                <h2>Job status</h2>
                <button type="button" className="link-button" onClick={() => setOpen(false)}>
                  Close
                </button>
              </div>
              <h3>Import</h3>
              <ImportPanel workspaceId={workspaceId} />
              <h3>Pre-scan</h3>
              <PrescanPanel workspaceId={workspaceId} />
              <h3>Build</h3>
              {buildJob ? <BuildPanel workspaceId={workspaceId} /> : <p className="hint">No build yet.</p>}
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
