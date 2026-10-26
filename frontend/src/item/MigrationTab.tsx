import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type ItemPatch } from "../api";
import { DiffBody, useViewType, ViewToggle } from "./DiffViewer";

export const itemPatchesKey = (workspaceId: number, key: string) => ["itemPatches", workspaceId, key];

const LEVEL_TEXT: Record<ItemPatch["level"], string> = { plain: "Applies as is", "3way": "3-way merge" };
const SOURCE_TEXT: Record<ItemPatch["source"], string> = { original: "Jira diff", ai: "AI" };

/** The item's migration patches, newest first; the selected one opens in the diff viewer. */
export function MigrationTab({ workspaceId, itemKey }: { workspaceId: number; itemKey: string }) {
  const patches = useQuery({ queryKey: itemPatchesKey(workspaceId, itemKey), queryFn: () => api.itemPatches(workspaceId, itemKey) });
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [viewType, setViewType] = useViewType();

  if (patches.isPending) return <p>Loading…</p>;
  if (patches.isError) return <p className="error">{patches.error.message}</p>;
  const list = patches.data;
  if (list.length === 0)
    return (
      <p className="hint">
        No migration patch. The pre-scan makes one for each diff that applies, as is or with a 3-way merge; diffs that conflict or are
        already present have none.
      </p>
    );
  const selected = list.find((p) => p.id === selectedId) ?? list.find((p) => p.current) ?? list[0];

  return (
    <>
      <table className="table patches" aria-label="Patches">
        <thead>
          <tr>
            <th>Diff</th>
            <th>Project</th>
            <th>Level</th>
            <th>Source</th>
            <th>Base HEAD</th>
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
          <ViewToggle viewType={viewType} onChange={setViewType} />
        </div>
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
        <DiffBody
          key={selected.id}
          id={`patch-${selected.id}`}
          queryKey={["patch", workspaceId, selected.id]}
          load={() => api.patch(workspaceId, selected.id)}
          viewType={viewType}
        />
      </section>
    </>
  );
}
