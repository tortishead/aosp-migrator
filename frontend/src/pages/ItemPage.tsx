import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { api, type Attachment, type Issue, type IssueLink, type ItemDetail, type LocalState } from "../api";
import { facetsKey, itemsKey, POLL_MS } from "../backlog/ImportPanel";
import { STATE_LABELS } from "../backlog/params";
import { confirmingDissolve, memberWarning, useRefreshClusters } from "../clusters/shared";
import { DiffTabs } from "../item/DiffViewer";
import { HistoryTimeline } from "../item/History";
import { JiraHtml } from "../item/JiraHtml";
import { PrescanSection } from "../prescan/PrescanSection";

type Tab = "overview" | "patches";

// Diffs and their pre-scan results live in their own tab, apart from the ticket itself.
const TABS: Record<Tab, string> = { overview: "Overview", patches: "Patches" };

export const itemKey = (workspaceId: number, key: string) => ["item", workspaceId, key];

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

export function ItemPage() {
  const workspaceId = Number(useParams().workspaceId);
  const key = useParams().itemKey ?? "";
  const item = useQuery({ queryKey: itemKey(workspaceId, key), queryFn: () => api.item(workspaceId, key) });
  const [search, setSearch] = useSearchParams();
  const tab: Tab = search.get("tab") === "patches" ? "patches" : "overview";

  function selectTab(next: Tab) {
    setSearch(
      (current) => {
        const params = new URLSearchParams(current);
        if (next === "overview") params.delete("tab");
        else params.set("tab", next);
        return params;
      },
      { replace: true },
    );
  }

  if (!item.data) {
    return (
      <>
        <h1>{key}</h1>
        {item.isError ? <p className="error">{item.error.message}</p> : <p>Loading…</p>}
      </>
    );
  }
  const detail = item.data;
  const issue = detail.issue;
  const diffCount = new Set(issue.attachments.filter((a) => a.kind === "diff").map((a) => a.filename)).size;

  return (
    <article className="item">
      <p>
        <Link to={`/workspaces/${workspaceId}`}>← Backlog</Link>
      </p>
      <div className="page-header">
        <h1>
          {issue.browse_url ? (
            <a href={issue.browse_url} target="_blank" rel="noopener noreferrer" title="Open in Jira">
              {issue.key}
            </a>
          ) : (
            issue.key
          )}{" "}
          {issue.summary}
        </h1>
      </div>
      <IssueFields issue={issue} />
      {detail.dropped && <p className="tag tag-warn">Dropped: no longer matched by the workspace JQL</p>}
      {detail.last_import_error && <p className="error">Last import failed: {detail.last_import_error}</p>}
      <ItemActions workspaceId={workspaceId} detail={detail} />
      <ClusterBox workspaceId={workspaceId} detail={detail} />

      <div className="tab-bar page-tabs" role="tablist" aria-label="Item sections">
        {(Object.entries(TABS) as [Tab, string][]).map(([value, label]) => (
          <button
            key={value}
            role="tab"
            id={`tab-${value}`}
            aria-selected={tab === value}
            aria-controls={`panel-${value}`}
            className={tab === value ? "tab active" : "tab"}
            onClick={() => selectTab(value)}
          >
            {label}
            {value === "patches" && <span className="count">{diffCount}</span>}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "overview" ? (
          <>
            <section aria-label="Description">
              <h2>Description</h2>
              <JiraHtml html={issue.description_html} baseUrl={detail.jira_base_url} />
            </section>
            <OtherAttachments attachments={issue.attachments} />
            <Comments issue={issue} baseUrl={detail.jira_base_url} />
            <LinkedTickets workspaceId={workspaceId} detail={detail} />
            <Notes workspaceId={workspaceId} itemKey={key} detail={detail} />
            <section aria-label="History">
              <h2>History</h2>
              <HistoryTimeline events={detail.history} />
            </section>
          </>
        ) : (
          <>
            <PrescanSection workspaceId={workspaceId} itemKey={key} />
            <section aria-label="Diffs">
              <h2>Diffs</h2>
              <DiffTabs workspaceId={workspaceId} attachments={issue.attachments} />
            </section>
          </>
        )}
      </div>
    </article>
  );
}

function IssueFields({ issue }: { issue: Issue }) {
  const fields: [string, string][] = [
    ["Jira status", issue.status],
    ["Labels", issue.labels.join(", ")],
    ["Components", issue.components.join(", ")],
    ["Fix versions", issue.fix_versions.join(", ")],
    ["Updated in Jira", new Date(issue.jira_updated).toLocaleString()],
    ["Imported", new Date(issue.fetched_at).toLocaleString()],
  ];
  return (
    <dl className="issue-fields">
      {fields.map(([name, value]) => (
        <div key={name}>
          <dt>{name}</dt>
          <dd>{value || "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

function ClusterBox({ workspaceId, detail }: { workspaceId: number; detail: ItemDetail }) {
  const refresh = useRefreshClusters(workspaceId);
  const key = detail.issue.key;
  const cluster = detail.cluster;
  const remove = useMutation({
    mutationFn: () => confirmingDissolve((confirm) => api.removeClusterMember(workspaceId, cluster!.id, key, confirm)),
    onSuccess: (result) => result !== null && refresh(),
  });

  return (
    <section aria-label="Cluster" className="cluster-box">
      {cluster === null ? (
        <p className="hint">
          Not in a cluster. Group items from the backlog selection, or with “Add to my cluster” on a linked backlog item.
        </p>
      ) : (
        <>
          <p className="actions">
            <span>
              Cluster <Link to={`/workspaces/${workspaceId}/clusters/${cluster.id}`}>{cluster.name}</Link>
            </span>
            <button onClick={() => remove.mutate()} disabled={remove.isPending}>
              Remove from cluster
            </button>
            {remove.isError && <span className="error">{remove.error.message}</span>}
          </p>
          <ol className="cluster-siblings" aria-label="Cluster items">
            {cluster.members.map((m) => {
              const warning = memberWarning(m);
              return (
                <li key={m.key}>
                  {m.key === key ? <strong>{m.key}</strong> : <Link to={`/workspaces/${workspaceId}/items/${m.key}`}>{m.key}</Link>}{" "}
                  {m.summary}
                  {warning && <span className="tag tag-warn">{warning}</span>}
                </li>
              );
            })}
          </ol>
        </>
      )}
    </section>
  );
}

function AddToMyCluster({ workspaceId, detail, link }: { workspaceId: number; detail: ItemDetail; link: IssueLink }) {
  const refresh = useRefreshClusters(workspaceId);
  const mine = detail.cluster;
  const add = useMutation({
    mutationFn: () =>
      confirmingDissolve((confirm) =>
        mine
          ? api.addClusterMembers(workspaceId, mine.id, [link.issue.key], confirm)
          : api.createCluster(workspaceId, [detail.issue.key, link.issue.key], confirm),
      ),
    onSuccess: (result) => result !== null && refresh(),
  });
  if (mine && link.cluster?.id === mine.id) return <span className="hint">In this cluster</span>;
  return (
    <>
      <button onClick={() => add.mutate()} disabled={add.isPending}>
        Add to my cluster
      </button>
      {add.isError && <span className="error">{add.error.message}</span>}
    </>
  );
}

function ItemActions({ workspaceId, detail }: { workspaceId: number; detail: ItemDetail }) {
  const queryClient = useQueryClient();
  const key = detail.issue.key;
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: itemKey(workspaceId, key) });
    void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
  };

  const changeState = useMutation({
    mutationFn: (state: LocalState) => api.changeState(workspaceId, [key], state),
    onSuccess: refresh,
  });

  const [jobId, setJobId] = useState<number | null>(null);
  const reimport = useMutation({ mutationFn: () => api.startImport(workspaceId, [key]), onSuccess: (job) => setJobId(job.id) });
  const job = useQuery({
    queryKey: ["importJob", jobId],
    queryFn: () => api.importJob(jobId!),
    enabled: jobId !== null,
    refetchInterval: (query) => (query.state.data?.status === "running" ? POLL_MS : false),
  });
  const status = job.data?.status;
  const running = reimport.isPending || status === "running";
  // Reload the item once the reimport finishes.
  const seen = useRef(status);
  useEffect(() => {
    if (status === seen.current) return;
    seen.current = status;
    if (status && status !== "running") {
      refresh();
      void queryClient.invalidateQueries({ queryKey: facetsKey(workspaceId) });
    }
  }, [status]);

  return (
    <div className="actions item-actions">
      <label>
        State{" "}
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
      <button onClick={() => reimport.mutate()} disabled={running}>
        Reimport from Jira
      </button>
      {running && <span role="status">Reimporting…</span>}
      {status === "done" && job.data!.errors.length === 0 && <span role="status">Reimported.</span>}
      {status === "done" && job.data!.errors.length > 0 && (
        <span role="status" className="error">
          Reimport failed: {job.data!.errors.map((e) => e.message).join("; ")}
        </span>
      )}
      {(status === "failed" || status === "canceled") && (
        <span role="status" className="error">
          Reimport {status}
          {job.data!.error && `: ${job.data!.error}`}
        </span>
      )}
      {changeState.isError && <span className="error">{changeState.error.message}</span>}
      {reimport.isError && <span className="error">{reimport.error.message}</span>}
    </div>
  );
}

// `of` names the linked ticket a section belongs to, so section labels stay unique on the page.
function OtherAttachments({ attachments, of }: { attachments: Attachment[]; of?: string }) {
  const others = attachments.filter((a) => a.kind !== "diff");
  return (
    <section aria-label={of ? `${of} other attachments` : "Other attachments"}>
      <h2>Other attachments</h2>
      {others.length === 0 ? (
        <p className="hint">None.</p>
      ) : (
        <table className="table attachments">
          <tbody>
            {others.map((a) => (
              <tr key={a.id}>
                <td>
                  <a href={a.jira_url} target="_blank" rel="noopener noreferrer">
                    {a.filename}
                  </a>
                </td>
                <td className="nowrap">{formatSize(a.size)}</td>
                <td className="nowrap">{new Date(a.created).toLocaleDateString()}</td>
                <td>
                  {a.local_url ? (
                    <a href={a.local_url} target="_blank" rel="noopener noreferrer">
                      local copy
                    </a>
                  ) : (
                    <span className="hint">not downloaded</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Comments({ issue, baseUrl, of }: { issue: Issue; baseUrl: string | null; of?: string }) {
  return (
    <section aria-label={of ? `${of} comments` : "Comments"}>
      <h2>Comments ({issue.comments.length})</h2>
      {issue.comments.length === 0 && <p className="hint">No comments.</p>}
      {issue.comments.map((c) => (
        <div key={c.id} className="jira-comment">
          <div className="jira-comment-header">
            <strong>{c.author}</strong> <time dateTime={c.created}>{new Date(c.created).toLocaleString()}</time>
          </div>
          <JiraHtml html={c.body_html} baseUrl={baseUrl} />
        </div>
      ))}
    </section>
  );
}

function LinkedTickets({ workspaceId, detail }: { workspaceId: number; detail: ItemDetail }) {
  return (
    <section aria-label="Linked tickets">
      <h2>Linked tickets</h2>
      {detail.links.length === 0 && <p className="hint">None.</p>}
      {detail.links.map((link) => {
        const linked = link.issue;
        return (
          <details key={`${link.link_type}-${link.direction}-${linked.key}`} className="linked">
            <summary>
              <span className="hint">{link.description}</span> <strong>{linked.key}</strong> {linked.summary}{" "}
              <span className="tag">{linked.status}</span>
              {link.cluster && <span className="tag">cluster: {link.cluster.name}</span>}
            </summary>
            <div className="linked-body">
              <p className="actions">
                {link.item_id !== null && <Link to={`/workspaces/${workspaceId}/items/${linked.key}`}>Open backlog item</Link>}
                {link.item_id !== null && <AddToMyCluster workspaceId={workspaceId} detail={detail} link={link} />}
                {linked.browse_url && (
                  <a href={linked.browse_url} target="_blank" rel="noopener noreferrer">
                    Open in Jira
                  </a>
                )}
              </p>
              <JiraHtml html={linked.description_html} baseUrl={detail.jira_base_url} />
              <DiffTabs workspaceId={workspaceId} attachments={linked.attachments} />
              <OtherAttachments attachments={linked.attachments} of={linked.key} />
              <Comments issue={linked} baseUrl={detail.jira_base_url} of={linked.key} />
            </div>
          </details>
        );
      })}
    </section>
  );
}

function Notes({ workspaceId, itemKey: key, detail }: { workspaceId: number; itemKey: string; detail: ItemDetail }) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const add = useMutation({
    mutationFn: () => api.addNote(workspaceId, key, text),
    onSuccess: () => {
      setText("");
      void queryClient.invalidateQueries({ queryKey: itemKey(workspaceId, key) });
    },
  });
  return (
    <section aria-label="Notes">
      <h2>Notes</h2>
      {detail.notes.length === 0 && <p className="hint">No notes yet.</p>}
      <ul className="notes">
        {detail.notes.map((n) => (
          <li key={n.id}>
            <time dateTime={n.created_at}>{new Date(n.created_at).toLocaleString()}</time>
            <p className="note-text">{n.text}</p>
          </li>
        ))}
      </ul>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) add.mutate();
        }}
      >
        <textarea aria-label="New note" rows={3} value={text} onChange={(e) => setText(e.target.value)} />
        <div className="actions">
          <button type="submit" disabled={!text.trim() || add.isPending}>
            Add note
          </button>
          {add.isError && <span className="error">{add.error.message}</span>}
        </div>
      </form>
    </section>
  );
}
