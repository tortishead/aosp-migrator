import { useQueries, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, type Attachment, type DiffWithHunks } from "../api";
import { FileDiff } from "../item/DiffViewer";

type DiffFile = DiffWithHunks["files"][number];

interface Props {
  workspaceId: number;
  patchId: number;
  /** The item's attachments; its current diffs are the originals. */
  attachments: Attachment[];
}

/** Per file of a rework patch: what the Jira diff did to it next to what the proposed patch does. */
export function PatchCompare({ workspaceId, patchId, attachments }: Props) {
  const originals = attachments.filter((a) => a.kind === "diff" && a.is_current && a.local_url);
  const patch = useQuery({ queryKey: ["patch", workspaceId, patchId], queryFn: () => api.patch(workspaceId, patchId), staleTime: Infinity });
  const diffs = useQueries({
    queries: originals.map((a) => ({
      queryKey: ["diff", workspaceId, a.jira_id],
      queryFn: () => api.diff(workspaceId, a.jira_id),
      staleTime: Infinity,
    })),
  });
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  if (patch.isPending || diffs.some((d) => d.isPending)) return <p>Loading diff…</p>;
  if (patch.isError) return <p className="error">{patch.error.message}</p>;
  const failed = diffs.find((d) => d.isError);
  if (failed?.error) return <p className="error">{failed.error.message}</p>;
  const originalFiles = diffs.flatMap((d) => d.data?.files ?? []);
  const files = patch.data.files;
  if (files.length === 0) return <p className="hint">No file changes found.</p>;

  function original(file: DiffFile): DiffFile | undefined {
    const paths = [file.path, file.old_path].filter(Boolean);
    return originalFiles.find((o) => paths.includes(o.path) || (o.old_path !== null && paths.includes(o.old_path)));
  }

  function toggle(path: string) {
    setCollapsed((current) => {
      const next = new Set(current);
      if (!next.delete(path)) next.add(path);
      return next;
    });
  }

  return (
    <div className="patch-compare">
      <div className="patch-compare-head">
        <h4>Jira diff</h4>
        <h4>Proposed patch</h4>
      </div>
      {files.map((file, i) => {
        const before = original(file);
        const folded = collapsed.has(file.path);
        return (
          <div key={file.path} className="patch-compare-row" role="group" aria-label={`Compare ${file.path}`}>
            {before ? (
              <FileDiff id={`compare-${patchId}-o${i}`} file={before} viewType="unified" collapsed={folded} toggle={() => toggle(file.path)} />
            ) : (
              <p className="hint diff-note">Not in the Jira diff.</p>
            )}
            <FileDiff id={`compare-${patchId}-p${i}`} file={file} viewType="unified" collapsed={folded} toggle={() => toggle(file.path)} />
          </div>
        );
      })}
    </div>
  );
}
