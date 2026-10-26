import { useQuery } from "@tanstack/react-query";

import { api, type ApplyResult, type DiffScan, type Presence } from "../api";
import { itemPrescanKey, PrescanStatus, useLatestPrescan, useStartPrescan } from "./PrescanPanel";

export const PRESENCE_LABELS: Record<Presence, string> = {
  present: "Already present",
  likely_present: "Likely present",
  not_present: "Not present",
  partial: "Partially present",
  unknown: "Unknown",
};

export const APPLY_LABELS: Record<ApplyResult, string> = {
  applies: "Applies",
  "3way": "Applies with 3-way merge",
  conflicts: "Conflicts",
};

const MAPPING_LABELS: Record<NonNullable<DiffScan["mapping_source"]>, string> = {
  hint: "path in description",
  files: "files match",
  copilot: "Copilot",
  manual: "picked by hand",
};

function presenceClass(presence: Presence | null): string {
  if (presence === "present") return "tag";
  if (presence === "likely_present" || presence === "partial" || presence === "unknown") return "tag tag-warn";
  return "tag tag-error";
}

function applyClass(apply: ApplyResult): string {
  return { applies: "tag", "3way": "tag tag-warn", conflicts: "tag tag-error" }[apply];
}

/** Read-only pre-scan results for one item, with a re-run button. */
export function PrescanSection({ workspaceId, itemKey }: { workspaceId: number; itemKey: string }) {
  const result = useQuery({ queryKey: itemPrescanKey(workspaceId, itemKey), queryFn: () => api.itemPrescan(workspaceId, itemKey) });
  const job = useLatestPrescan(workspaceId);
  const start = useStartPrescan(workspaceId);
  const running = job?.status === "running";
  // Only this item's own re-runs, or a scan still going, are news here.
  const showJob = job && (running || (job.scope === "keys" && job.issue_keys.includes(itemKey)));
  const prescan = result.data;

  return (
    <section aria-label="Pre-scan">
      <h2>Pre-scan</h2>
      <p className="actions">
        <button onClick={() => start.mutate([itemKey])} disabled={running || start.isPending}>
          Pre-scan this item
        </button>
        {showJob && <PrescanStatus job={job} />}
        {start.isError && <span className="error">{start.error.message}</span>}
      </p>
      {result.isError && <p className="error">{result.error.message}</p>}
      {prescan && !prescan.scanned && <p className="hint">Not pre-scanned yet.</p>}
      {prescan?.scanned && (
        <>
          <p>
            Item: <span className={presenceClass(prescan.presence)}>{PRESENCE_LABELS[prescan.presence ?? "unknown"]}</span>
            {prescan.stale.map((reason) => (
              <span key={reason} className="tag tag-warn" title="Results are out of date; pre-scan again">
                Stale: {reason}
              </span>
            ))}
          </p>
          <table className="table prescan-diffs">
            <thead>
              <tr>
                <th>Diff</th>
                <th>Target project</th>
                <th>Already present?</th>
                <th>Applies?</th>
              </tr>
            </thead>
            <tbody>
              {prescan.diffs.map((d) => (
                <DiffRow key={d.attachment_id} diff={d} />
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

function DiffRow({ diff }: { diff: DiffScan }) {
  if (diff.scanned_at === null) {
    return (
      <tr>
        <td className="mono">{diff.filename}</td>
        <td colSpan={3} className="hint">
          Not scanned (new version since the last pre-scan)
        </td>
      </tr>
    );
  }
  const detail = diff.presence_detail;
  const applied = diff.apply_detail;
  return (
    <tr>
      <td className="mono">{diff.filename}</td>
      <td>
        {diff.project ? (
          <>
            <span className="mono">{diff.project}</span>{" "}
            <span className="hint">({MAPPING_LABELS[diff.mapping_source ?? "files"]})</span>
          </>
        ) : (
          <>
            <span className="tag tag-warn">Unmapped</span>{" "}
            {diff.candidates.length > 0 ? (
              <span className="hint">
                candidates: <span className="mono">{diff.candidates.join(", ")}</span>
              </span>
            ) : (
              <span className="hint">no project has these files</span>
            )}
          </>
        )}
      </td>
      <td>
        {diff.presence && <span className={presenceClass(diff.presence)}>{PRESENCE_LABELS[diff.presence]}</span>}
        {detail?.reason && <span className="hint"> {detail.reason}</span>}
        {detail?.files && (
          <ul className="prescan-files">
            {Object.entries(detail.files).map(([path, status]) => (
              <li key={path}>
                <span className="mono">{path}</span>: {PRESENCE_LABELS[status]}
              </li>
            ))}
          </ul>
        )}
        {detail?.history && (
          <ul className="prescan-files">
            {detail.history.map((h, i) => (
              <li key={i}>
                Commit “{h.subject}”:{" "}
                {h.sha ? (
                  <>
                    in history as <span className="mono">{h.sha.slice(0, 12)}</span> (by {h.match?.replace("_", "-")})
                  </>
                ) : (
                  "not found in history"
                )}
              </li>
            ))}
          </ul>
        )}
      </td>
      <td>
        {diff.apply && <span className={applyClass(diff.apply)}>{APPLY_LABELS[diff.apply]}</span>}
        {applied && diff.apply !== "applies" && (
          <ul className="prescan-files">
            {Object.entries(applied.files).map(([path, status]) => (
              <li key={path}>
                <span className="mono">{path}</span>: {APPLY_LABELS[status]}
                {applied.errors?.[path] && <span className="hint"> ({applied.errors[path]})</span>}
              </li>
            ))}
          </ul>
        )}
      </td>
    </tr>
  );
}
