import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Decoration, Diff, Hunk, parseDiff, tokenize, type DiffType, type HunkTokens, type ViewType } from "react-diff-view";
import "react-diff-view/style/index.css";
import { refractor } from "refractor/core";
import c from "refractor/c";
import cpp from "refractor/cpp";
import java from "refractor/java";
import javascript from "refractor/javascript";
import kotlin from "refractor/kotlin";
import makefile from "refractor/makefile";
import markup from "refractor/markup";

import { api, type Attachment, type DiffWithHunks, type FileStatus } from "../api";

for (const syntax of [c, cpp, java, javascript, kotlin, makefile, markup]) refractor.register(syntax);
// react-diff-view expects refractor 3's API, which returned the node list instead of a root.
const highlighter = { highlight: (code: string, language: string) => refractor.highlight(code, language).children };

const VIEW_TYPE_KEY = "aosp-migrator.diffViewType";

const DIFF_TYPES: Record<FileStatus, DiffType> = {
  modified: "modify",
  added: "add",
  deleted: "delete",
  renamed: "rename",
  copied: "copy",
};

const EXTENSIONS: Record<string, string> = {
  java: "java",
  c: "c",
  h: "cpp",
  cc: "cpp",
  cpp: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  kt: "kotlin",
  kts: "kotlin",
  xml: "markup",
  // Blueprint is JSON-like with `//` comments; JavaScript highlighting fits it well enough.
  bp: "javascript",
  mk: "makefile",
};

export function languageFor(path: string): string | null {
  const name = path.split("/").pop() ?? "";
  if (name === "Makefile") return "makefile";
  const dot = name.lastIndexOf(".");
  return dot === -1 ? null : (EXTENSIONS[name.slice(dot + 1).toLowerCase()] ?? null);
}

function loadViewType(): ViewType {
  try {
    return localStorage.getItem(VIEW_TYPE_KEY) === "split" ? "split" : "unified";
  } catch {
    return "unified";
  }
}

function saveViewType(viewType: ViewType) {
  try {
    localStorage.setItem(VIEW_TYPE_KEY, viewType);
  } catch {
    // Storage blocked: the choice just is not remembered.
  }
}

/** One tab per diff attachment (same filename = versions of one diff), newest version shown first. */
export function DiffTabs({ workspaceId, attachments }: { workspaceId: number; attachments: Attachment[] }) {
  const groups = useMemo(() => {
    const byName = new Map<string, Attachment[]>();
    for (const a of attachments.filter((a) => a.kind === "diff")) byName.set(a.filename, [...(byName.get(a.filename) ?? []), a]);
    return [...byName.entries()].map(([filename, versions]) => ({
      filename,
      versions: [...versions].sort((x, y) => y.created.localeCompare(x.created) || Number(y.jira_id) - Number(x.jira_id)),
    }));
  }, [attachments]);
  const [tab, setTab] = useState(0);
  const [viewType, setViewType] = useState<ViewType>(loadViewType);

  if (groups.length === 0) return <p className="hint">No diff attachments.</p>;
  const group = groups[Math.min(tab, groups.length - 1)];

  function changeViewType(next: ViewType) {
    setViewType(next);
    saveViewType(next);
  }

  return (
    <div className="diff-tabs">
      <div className="tab-bar" role="tablist" aria-label="Diff attachments">
        {groups.map((g, i) => (
          <button key={g.filename} role="tab" aria-selected={g === group} className={g === group ? "tab active" : "tab"} onClick={() => setTab(i)}>
            {g.filename}
            {g.versions.length > 1 && <span className="tag">{g.versions.length} versions</span>}
          </button>
        ))}
        <span className="view-toggle" role="group" aria-label="Diff layout">
          <button aria-pressed={viewType === "unified"} onClick={() => changeViewType("unified")}>
            Unified
          </button>
          <button aria-pressed={viewType === "split"} onClick={() => changeViewType("split")}>
            Split
          </button>
        </span>
      </div>
      <VersionedDiff key={group.filename} workspaceId={workspaceId} versions={group.versions} viewType={viewType} />
    </div>
  );
}

function VersionedDiff({ workspaceId, versions, viewType }: { workspaceId: number; versions: Attachment[]; viewType: ViewType }) {
  const [selected, setSelected] = useState((versions.find((v) => v.is_current) ?? versions[0]).jira_id);
  const attachment = versions.find((v) => v.jira_id === selected) ?? versions[0];
  return (
    <>
      <div className="diff-meta">
        {versions.length > 1 && (
          <select aria-label="Version" value={attachment.jira_id} onChange={(e) => setSelected(e.target.value)}>
            {versions.map((v, i) => (
              <option key={v.jira_id} value={v.jira_id}>
                v{versions.length - i} · {new Date(v.created).toLocaleString()}
                {v.is_current ? " (current)" : ""}
              </option>
            ))}
          </select>
        )}
        {attachment.local_url && (
          <a href={attachment.local_url} download={attachment.filename}>
            Download
          </a>
        )}
      </div>
      {attachment.local_url ? (
        <DiffBody workspaceId={workspaceId} attachment={attachment} viewType={viewType} />
      ) : (
        <p className="hint">Not downloaded.</p>
      )}
    </>
  );
}

function DiffBody({ workspaceId, attachment, viewType }: { workspaceId: number; attachment: Attachment; viewType: ViewType }) {
  const diff = useQuery({
    queryKey: ["diff", workspaceId, attachment.jira_id],
    queryFn: () => api.diff(workspaceId, attachment.jira_id),
    staleTime: Infinity,
  });
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());

  if (diff.isPending) return <p>Loading diff…</p>;
  if (diff.isError) return <p className="error">{diff.error.message}</p>;
  const files = diff.data.files;
  if (files.length === 0) return <p className="hint">No file changes found in this attachment.</p>;
  const anchor = (i: number) => `diff-${attachment.jira_id}-${i}`;

  function toggle(i: number, open?: boolean) {
    setCollapsed((current) => {
      const next = new Set(current);
      if (open ?? next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  }

  return (
    <div className="diff-layout">
      <nav className="diff-files" aria-label="Changed files">
        <ul>
          {files.map((f, i) => (
            <li key={i}>
              <button
                className="link-button"
                title={f.path}
                onClick={() => {
                  toggle(i, true);
                  document.getElementById(anchor(i))?.scrollIntoView({ block: "start" });
                }}
              >
                {f.path.split("/").pop()}
              </button>
              <Counts file={f} />
            </li>
          ))}
        </ul>
      </nav>
      <div className="diff-main">
        {files.map((f, i) => (
          <FileDiff key={i} id={anchor(i)} file={f} viewType={viewType} collapsed={collapsed.has(i)} toggle={() => toggle(i)} />
        ))}
      </div>
    </div>
  );
}

function Counts({ file }: { file: { added: number; removed: number } }) {
  return (
    <span className="diff-counts">
      <span className="added">+{file.added}</span> <span className="removed">−{file.removed}</span>
    </span>
  );
}

interface FileDiffProps {
  id: string;
  file: DiffWithHunks["files"][number];
  viewType: ViewType;
  collapsed: boolean;
  toggle: () => void;
}

function FileDiff({ id, file, viewType, collapsed, toggle }: FileDiffProps) {
  const hunks = useMemo(() => {
    if (file.hunks.length === 0) return [];
    // Hunk bodies only: paths and status come from the server's parser, which knows more formats.
    // Some tools strip the space of empty context lines; put it back.
    const body = file.hunks.map((h) => h.split("\n").map((line) => (line === "" ? " " : line)).join("\n")).join("\n");
    return parseDiff(`diff --git a/f b/f\n--- a/f\n+++ b/f\n${body}\n`)[0]?.hunks ?? [];
  }, [file.hunks]);
  const tokens = useMemo((): HunkTokens | null => {
    const language = languageFor(file.path);
    if (!language || hunks.length === 0) return null;
    try {
      return tokenize(hunks, { highlight: true, refractor: highlighter, language });
    } catch {
      return null;
    }
  }, [hunks, file.path]);

  return (
    <section className="diff-file" id={id} aria-label={file.path}>
      <header className="diff-file-header">
        <button className="link-button" aria-expanded={!collapsed} onClick={toggle}>
          {collapsed ? "▸" : "▾"}
        </button>
        <span className="mono">
          {file.old_path ? `${file.old_path} → ${file.path}` : file.path}
        </span>
        {file.status !== "modified" && <span className="tag">{file.status}</span>}
        <Counts file={file} />
      </header>
      {!collapsed &&
        (file.binary ? (
          <p className="hint diff-note">Binary file not shown.</p>
        ) : hunks.length === 0 ? (
          <p className="hint diff-note">No content changes.</p>
        ) : (
          <Diff viewType={viewType} diffType={DIFF_TYPES[file.status]} hunks={hunks} tokens={tokens}>
            {(hunks) =>
              hunks.flatMap((hunk, i) => [
                <Decoration key={`d${i}`}>
                  <span className="mono hunk-header">{hunk.content}</span>
                </Decoration>,
                <Hunk key={`h${i}`} hunk={hunk} />,
              ])
            }
          </Diff>
        ))}
    </section>
  );
}
