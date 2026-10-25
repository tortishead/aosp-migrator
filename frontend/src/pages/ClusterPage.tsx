import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, type ClusterDetail, type LocalState } from "../api";
import { STATE_LABELS } from "../backlog/params";
import { clusterKey, confirmingDissolve, memberWarning, useRefreshClusters } from "../clusters/shared";
import { describeClusterEvent, HistoryTimeline } from "../item/History";

export function ClusterPage() {
  const workspaceId = Number(useParams().workspaceId);
  const clusterId = Number(useParams().clusterId);
  const cluster = useQuery({ queryKey: clusterKey(workspaceId, clusterId), queryFn: () => api.cluster(workspaceId, clusterId) });

  const back = (
    <p className="actions">
      <Link to={`/workspaces/${workspaceId}`}>← Backlog</Link>
      <Link to={`/workspaces/${workspaceId}/clusters`}>All clusters</Link>
    </p>
  );
  if (!cluster.data) {
    return (
      <>
        {back}
        <h1>Cluster</h1>
        {cluster.isError ? <p className="error">{cluster.error.message}</p> : <p>Loading…</p>}
      </>
    );
  }
  const detail = cluster.data;

  return (
    <article className="cluster">
      {back}
      <div className="page-header">
        <h1>{detail.name}</h1>
        <DissolveButton workspaceId={workspaceId} detail={detail} />
      </div>
      <NameForm workspaceId={workspaceId} detail={detail} />
      <Members workspaceId={workspaceId} detail={detail} />
      <ChangedFiles detail={detail} />
      <Notes workspaceId={workspaceId} detail={detail} />
      <section aria-label="History">
        <h2>History</h2>
        <HistoryTimeline events={detail.history} describe={describeClusterEvent} />
      </section>
    </article>
  );
}

function DissolveButton({ workspaceId, detail }: { workspaceId: number; detail: ClusterDetail }) {
  const navigate = useNavigate();
  const refresh = useRefreshClusters(workspaceId);
  const dissolve = useMutation({
    mutationFn: () => api.dissolveCluster(workspaceId, detail.id),
    onSuccess: () => {
      refresh();
      navigate(`/workspaces/${workspaceId}/clusters`);
    },
  });
  return (
    <>
      <button
        className="danger"
        disabled={dissolve.isPending}
        onClick={() => window.confirm(`Dissolve cluster "${detail.name}"? Its items stay in the backlog.`) && dissolve.mutate()}
      >
        Dissolve cluster
      </button>
      {dissolve.isError && <span className="error">{dissolve.error.message}</span>}
    </>
  );
}

function NameForm({ workspaceId, detail }: { workspaceId: number; detail: ClusterDetail }) {
  const refresh = useRefreshClusters(workspaceId);
  const [name, setName] = useState(detail.name);
  const [description, setDescription] = useState(detail.description);
  useEffect(() => {
    setName(detail.name);
    setDescription(detail.description);
  }, [detail.name, detail.description]);
  const save = useMutation({
    mutationFn: () => api.updateCluster(workspaceId, detail.id, { name, description }),
    onSuccess: refresh,
  });
  const changed = name.trim() !== detail.name || description.trim() !== detail.description;

  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        if (changed && name.trim()) save.mutate();
      }}
    >
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        Description
        <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <div className="actions">
        <button type="submit" disabled={!changed || !name.trim() || save.isPending}>
          Save
        </button>
        {save.isError && <span className="error">{save.error.message}</span>}
      </div>
    </form>
  );
}

function Members({ workspaceId, detail }: { workspaceId: number; detail: ClusterDetail }) {
  const navigate = useNavigate();
  const refresh = useRefreshClusters(workspaceId);
  const keys = detail.members.map((m) => m.key);

  const reorder = useMutation({
    mutationFn: (order: string[]) => api.reorderCluster(workspaceId, detail.id, order),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (key: string) => confirmingDissolve((confirm) => api.removeClusterMember(workspaceId, detail.id, key, confirm)),
    onSuccess: (result) => {
      if (result === null) return;
      refresh();
      if (result.dissolved) navigate(`/workspaces/${workspaceId}/clusters`);
    },
  });
  const [state, setState] = useState<LocalState | "">("");
  const changeState = useMutation({
    mutationFn: (to: LocalState) => api.changeState(workspaceId, keys, to),
    onSuccess: () => {
      setState("");
      refresh();
    },
  });

  function move(index: number, by: -1 | 1) {
    const order = [...keys];
    [order[index], order[index + by]] = [order[index + by], order[index]];
    reorder.mutate(order);
  }

  const busy = reorder.isPending || remove.isPending;
  return (
    <section aria-label="Members">
      <h2>Items, in migration order</h2>
      <table className="table cluster-members">
        <thead>
          <tr>
            <th>#</th>
            <th>Key</th>
            <th>Summary</th>
            <th>Jira status</th>
            <th>State</th>
            <th>Diffs</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {detail.members.map((m, index) => {
            const warning = memberWarning(m);
            return (
              <tr key={m.key}>
                <td>{index + 1}</td>
                <td className="nowrap">
                  <Link to={`/workspaces/${workspaceId}/items/${m.key}`}>{m.key}</Link>
                </td>
                <td>
                  {m.summary}
                  {warning && (
                    <span className="tag tag-warn" title="Check whether this item still belongs in the cluster">
                      {warning}
                    </span>
                  )}
                </td>
                <td>{m.status}</td>
                <td className="nowrap">{STATE_LABELS[m.local_state]}</td>
                <td>{m.diff_count || ""}</td>
                <td className="nowrap">
                  <button aria-label={`Move ${m.key} up`} disabled={busy || index === 0} onClick={() => move(index, -1)}>
                    ↑
                  </button>
                  <button
                    aria-label={`Move ${m.key} down`}
                    disabled={busy || index === keys.length - 1}
                    onClick={() => move(index, 1)}
                  >
                    ↓
                  </button>
                  <button aria-label={`Remove ${m.key}`} disabled={busy} onClick={() => remove.mutate(m.key)}>
                    Remove
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="actions">
        <select aria-label="State for all items" value={state} onChange={(e) => setState(e.target.value as LocalState | "")}>
          <option value="">Set state for all…</option>
          {(Object.entries(STATE_LABELS) as [LocalState, string][]).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <button onClick={() => state && changeState.mutate(state)} disabled={!state || changeState.isPending}>
          Apply
        </button>
        {[reorder, remove, changeState].map((m, i) => m.isError && <span key={i} className="error">{m.error.message}</span>)}
      </div>
    </section>
  );
}

function ChangedFiles({ detail }: { detail: ClusterDetail }) {
  const overlaps = detail.files.filter((f) => f.touched_by.length > 1).length;
  return (
    <section aria-label="Changed files">
      <h2>Changed files ({detail.files.length})</h2>
      {detail.files.length === 0 ? (
        <p className="hint">No diffs.</p>
      ) : (
        <>
          <p className="hint">
            {overlaps === 0 ? "No file is touched by more than one item." : `${overlaps} touched by more than one item.`}
          </p>
          <table className="table cluster-files">
            <tbody>
              {detail.files.map((f) => (
                <tr key={f.path} className={f.touched_by.length > 1 ? "overlap" : undefined}>
                  <td className="mono">{f.path}</td>
                  <td className="nowrap">{f.touched_by.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

function Notes({ workspaceId, detail }: { workspaceId: number; detail: ClusterDetail }) {
  const refresh = useRefreshClusters(workspaceId);
  const [text, setText] = useState("");
  const add = useMutation({
    mutationFn: () => api.addClusterNote(workspaceId, detail.id, text),
    onSuccess: () => {
      setText("");
      refresh();
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
