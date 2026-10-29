import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { api, type ApplyResult, type Dependency, type DependencyState, type DiffScan, type Presence } from "../api";
import { itemsKey } from "../backlog/ImportPanel";
import { itemKey } from "../pages/ItemPage";
import { itemPrescanKey, PrescanStatus, useLatestPrescan, useStartPrescan } from "./PrescanPanel";
import { TRIAGE_TEXT, triageClass } from "./triage";

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

const DEPENDENCY_LABELS: Record<DependencyState, string> = {
  pending: "Not in target",
  in_target: "Already in target",
  missing: "Base in no diff",
};

const DEPENDENCY_CLASSES: Record<DependencyState, string> = { pending: "tag tag-warn", in_target: "tag", missing: "tag tag-error" };

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
            {prescan.label && (
              <>
                Label: <span className={triageClass(prescan.label)}>{TRIAGE_TEXT[prescan.label]}</span>{" "}
                {prescan.label_reason && <span className="hint">{prescan.label_reason}</span>}
              </>
            )}
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
                <DiffRow key={d.attachment_id} workspaceId={workspaceId} itemKey={itemKey} diff={d} running={running} />
              ))}
            </tbody>
          </table>
          <Dependencies workspaceId={workspaceId} title="Depends on" deps={prescan.depends_on} />
          <Dependencies workspaceId={workspaceId} title="Needed by" deps={prescan.needed_by} />
        </>
      )}
    </section>
  );
}

/** Files written on top of another item's diff (or on a base no diff has), found by blob ids. */
function Dependencies({ workspaceId, title, deps }: { workspaceId: number; title: string; deps: Dependency[] }) {
  if (deps.length === 0) return null;
  return (
    <>
      <h3>{title}</h3>
      <ul className="prescan-files" aria-label={title}>
        {deps.map((d) => (
          <li key={`${d.filename}:${d.path}:${d.other?.filename ?? ""}`}>
            {d.other ? (
              <>
                <Link to={`/workspaces/${workspaceId}/items/${d.other.key}`}>{d.other.key}</Link> {d.other.summary}{" "}
                {d.other.triage_label && <span className={triageClass(d.other.triage_label)}>{TRIAGE_TEXT[d.other.triage_label]}</span>}
                {d.other.dropped && <span className="tag tag-error">Dropped</span>}
                {": "}
              </>
            ) : null}
            <span className="mono">{d.path}</span> <span className={DEPENDENCY_CLASSES[d.state]}>{DEPENDENCY_LABELS[d.state]}</span>{" "}
            <span className="hint">
              ({d.other ? `${d.filename} ← ${d.other.filename}` : d.filename}, base <span className="mono">{d.blob.slice(0, 12)}</span>)
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

interface DiffRowProps {
  workspaceId: number;
  itemKey: string;
  diff: DiffScan;
  // No picking while a pre-scan runs: the backend refuses it.
  running: boolean;
}

function DiffRow({ workspaceId, itemKey, diff, running }: DiffRowProps) {
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
        <ProjectPicker workspaceId={workspaceId} itemKey={itemKey} diff={diff} disabled={running} />
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
        {detail?.heuristic && (
          <ul className="prescan-files" aria-label="Line evidence">
            {Object.entries(detail.heuristic).map(([path, h]) => (
              <li key={path}>
                <span className="mono">{path}</span>: {h.added.found} of {h.added.total} added lines found, {h.removed.gone} of{" "}
                {h.removed.total} removed lines gone
                {h.added.missing.length > 0 && <Lines title="missing" lines={h.added.missing} />}
                {h.removed.remaining.length > 0 && <Lines title="still there" lines={h.removed.remaining} />}
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

function Lines({ title, lines }: { title: string; lines: string[] }) {
  return (
    <div className="prescan-lines">
      <span className="hint">{title}:</span>
      {lines.map((line, i) => (
        <code key={i}>{line}</code>
      ))}
    </div>
  );
}

/** Pick the diff's project by hand (always offered while unmapped), or go back to the automatic mapping. */
function ProjectPicker({ workspaceId, itemKey: key, diff, disabled }: { workspaceId: number; itemKey: string; diff: DiffScan; disabled: boolean }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [choice, setChoice] = useState("");
  const [search, setSearch] = useState("");
  const shown = open || diff.project === null;
  const projects = useQuery({
    queryKey: ["targetProjects", workspaceId],
    queryFn: () => api.targetProjects(workspaceId),
    enabled: shown,
  });
  const pick = useMutation({
    mutationFn: (project: string | null) => api.pickProject(workspaceId, key, diff.attachment_id, project),
    onSuccess: (result) => {
      queryClient.setQueryData(itemPrescanKey(workspaceId, key), result);
      // Backlog label and the item's history changed too.
      void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
      void queryClient.invalidateQueries({ queryKey: itemKey(workspaceId, key) });
      setOpen(false);
      setChoice("");
      setSearch("");
    },
  });
  // The current choice stays listed so the select never shows a hidden value.
  const needle = search.trim().toLowerCase();
  const matches = (p: string) => !needle || p === choice || p.toLowerCase().includes(needle);
  const candidates = diff.candidates.filter(matches);
  const others = (projects.data ?? []).filter((p) => !diff.candidates.includes(p) && matches(p));

  return (
    <div className="project-picker">
      {shown ? (
        <>
          <input
            type="search"
            aria-label={`Search projects for ${diff.filename}`}
            placeholder="Search projects"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            disabled={disabled || pick.isPending}
          />
          <select
            aria-label={`Project for ${diff.filename}`}
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
            disabled={disabled || pick.isPending}
          >
            <option value="">Pick a project…</option>
            {candidates.length > 0 && (
              <optgroup label="Candidates">
                {candidates.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </optgroup>
            )}
            {others.length > 0 && (
              <optgroup label="All projects">
                {others.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </optgroup>
            )}
          </select>{" "}
          <button onClick={() => pick.mutate(choice)} disabled={!choice || disabled || pick.isPending}>
            Use project
          </button>
          {open && (
            <button
              onClick={() => {
                setOpen(false);
                setSearch("");
              }}
            >
              Cancel
            </button>
          )}
        </>
      ) : (
        <>
          <button onClick={() => setOpen(true)} disabled={disabled}>
            Change
          </button>
          {diff.mapping_source === "manual" && (
            <button onClick={() => pick.mutate(null)} disabled={disabled || pick.isPending}>
              Map automatically
            </button>
          )}
        </>
      )}
      {pick.isPending && <span className="hint"> Pre-scanning…</span>}
      {projects.isError && <span className="error"> {projects.error.message}</span>}
      {pick.isError && <span className="error"> {pick.error.message}</span>}
    </div>
  );
}
