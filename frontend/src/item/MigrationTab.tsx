import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Attachment, type ItemPatch } from "../api";
import { PatchCompare } from "../rework/PatchCompare";
import { ReviewBox } from "../rework/ReviewBox";
import { REVIEW_TEXT, reviewClass } from "../rework/rework";
import { useReworkAttempts } from "../rework/ReworkAttempts";
import { DiffBody, useViewType, ViewToggle } from "./DiffViewer";

export const itemPatchesKey = (workspaceId: number, key: string) => ["itemPatches", workspaceId, key];

const LEVEL_TEXT: Record<ItemPatch["level"], string> = {
  plain: "Applies as is",
  "3way": "3-way merge",
  stacked: "Stacked on prerequisites",
  source3way: "3-way on source base",
  resolve: "Conflicts resolved",
  port: "Rejected hunks ported",
  reimpl: "Re-implemented",
};
const SOURCE_TEXT: Record<ItemPatch["source"], string> = { original: "Jira diff", ai: "AI" };

const REWORK_LEVELS: ItemPatch["level"][] = ["stacked", "source3way", "resolve", "port", "reimpl"];

/** Made by a rework, so reviewed; pre-scan patches are not. */
export function isRework(patch: ItemPatch): boolean {
  return REWORK_LEVELS.includes(patch.level);
}

interface Props {
  workspaceId: number;
  itemKey: string;
  /** The item's attachments: a rework patch is shown next to its current Jira diffs. */
  attachments: Attachment[];
}

/** The item's migration patches, newest first; the selected one opens in the diff viewer, a rework patch with its review. */
export function MigrationTab({ workspaceId, itemKey, attachments }: Props) {
  const patches = useQuery({ queryKey: itemPatchesKey(workspaceId, itemKey), queryFn: () => api.itemPatches(workspaceId, itemKey) });
  const attempts = useReworkAttempts(workspaceId, itemKey);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [viewType, setViewType] = useViewType();
  const [compare, setCompare] = useState(false);

  if (patches.isPending) return <p>Loading…</p>;
  if (patches.isError) return <p className="error">{patches.error.message}</p>;
  const list = patches.data;
  if (list.length === 0)
    return (
      <p className="hint">
        No migration patch. The pre-scan makes one for each diff that applies, as is or with a 3-way merge; diffs that conflict or are
        already present have none. Rework makes one for a diff that conflicts.
      </p>
    );
  const selected = list.find((p) => p.id === selectedId) ?? list.find((p) => p.current) ?? list[0];
  const reworked = isRework(selected);
  const attempt = attempts.data?.find((a) => a.id === selected.attempt_id);

  return (
    <>
      <table className="table patches" aria-label="Patches">
        <thead>
          <tr>
            <th>Diff</th>
            <th>Project</th>
            <th>Level</th>
            <th>Source</th>
            <th>Base</th>
            <th>Review</th>
            <th>Made</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {list.map((p) => (
            <tr key={p.id} aria-selected={p === selected} className={p === selected ? "selected" : undefined}>
              <td>
                <button className="link-button" aria-pressed={p === selected} onClick={() => setSelectedId(p.id)}>
                  {p.filename ?? `Patch ${p.id}`}
                </button>
              </td>
              <td className="mono">{p.project}</td>
              <td>{LEVEL_TEXT[p.level]}</td>
              <td>{SOURCE_TEXT[p.source]}</td>
              <td className="mono" title={p.base_head ?? undefined}>
                {p.base_head ? p.base_head.slice(0, 12) : "—"}
                {p.bases.length > 0 && <div>+ {p.bases.map((b) => b.key).join(" + ")}</div>}
              </td>
              <td className="nowrap">
                {isRework(p) && <span className={reviewClass(p.review)}>{REVIEW_TEXT[p.review]}</span>}
                {p.stale.length > 0 && (
                  <span className="tag tag-warn" title={`Out of date: ${p.stale.join("; ")}`}>
                    stale
                  </span>
                )}
              </td>
              <td className="nowrap">{new Date(p.created_at).toLocaleString()}</td>
              <td>{p.current ? <span className="tag tag-ok">current</span> : <span className="hint">superseded</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <section aria-label="Patch" className="patch-view">
        <div className="diff-meta">
          <h3>
            {selected.filename ?? `Patch ${selected.id}`} · {LEVEL_TEXT[selected.level]}
          </h3>
          {reworked && (
            <label>
              <input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} /> Next to the Jira diff
            </label>
          )}
          {!(reworked && compare) && <ViewToggle viewType={viewType} onChange={setViewType} />}
        </div>
        {reworked && <ReviewBox key={selected.id} workspaceId={workspaceId} patch={selected} attempt={attempt} />}
        {(selected.author || selected.message) && (
          <section className="commit-header" aria-label="Commit">
            {selected.author && (
              <dl>
                <dt>From</dt>
                <dd>{selected.author}</dd>
              </dl>
            )}
            {selected.message && <pre>{selected.message}</pre>}
          </section>
        )}
        {reworked && compare ? (
          <PatchCompare key={selected.id} workspaceId={workspaceId} patchId={selected.id} attachments={attachments} />
        ) : (
          <DiffBody
            key={selected.id}
            id={`patch-${selected.id}`}
            queryKey={["patch", workspaceId, selected.id]}
            load={() => api.patch(workspaceId, selected.id)}
            viewType={viewType}
          />
        )}
      </section>
    </>
  );
}
