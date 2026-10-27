import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useCallback, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { api, type Cluster, type ClusterRef, type Item, type LocalState } from "../api";
import { Filters } from "../backlog/Filters";
import { facetsKey, ImportButton, itemsKey, useStartImport } from "../backlog/ImportPanel";
import { JobsPopup } from "../backlog/JobsPopup";
import { apiParams, currentPage, hasFilters, PAGE_SIZE, STATE_LABELS } from "../backlog/params";
import { BuildBar } from "../build/BuildBar";
import { BUILD_TEXT } from "../build/build";
import { useStartBuild } from "../build/BuildPanel";
import { BuildTag } from "../build/BuildTag";
import { clustersKey, confirmingDissolve, useRefreshClusters } from "../clusters/shared";
import { PrescanButton, useStartPrescan } from "../prescan/PrescanPanel";
import { TRIAGE_TEXT } from "../prescan/triage";
import { TriageBar } from "../prescan/TriageBar";
import { TriageTags } from "../prescan/TriageTags";
import { workspacesKey } from "../queryClient";

const SORTS: [string, string][] = [
  ["key", "Key ↑"],
  ["-key", "Key ↓"],
  ["-updated", "Updated, newest first"],
  ["updated", "Updated, oldest first"],
  ["status", "Jira status"],
  ["state", "State"],
];

const COLUMNS = 12;

type Group = "project" | "cluster" | "triage" | "build" | "status" | "state";

const GROUPS: [Group, string][] = [
  ["project", "Target project"],
  ["cluster", "Cluster"],
  ["triage", "Pre-scan"],
  ["build", "Build"],
  ["status", "Jira status"],
  ["state", "State"],
];

// The items API orders by the group first, so a group's rows stay together across pages.
function groupOf(item: Item, group: Group): string {
  switch (group) {
    case "project":
      return item.target_projects.join(", ") || "No target project";
    case "cluster":
      return item.cluster?.name ?? "Not in a cluster";
    case "triage":
      return item.triage_label ? TRIAGE_TEXT[item.triage_label] : "Not scanned";
    case "build":
      return item.build_status ? BUILD_TEXT[item.build_status] : "Not built";
    case "status":
      return item.status;
    case "state":
      return STATE_LABELS[item.local_state];
  }
}

export function BacklogPage() {
  const workspaceId = Number(useParams().workspaceId);
  const [search, setSearch] = useSearchParams();
  const queryClient = useQueryClient();
  const workspaces = useQuery({ queryKey: workspacesKey, queryFn: api.workspaces });
  const workspace = workspaces.data?.find((w) => w.id === workspaceId);

  const params = apiParams(search);
  const items = useQuery({
    queryKey: [...itemsKey(workspaceId), params.toString()],
    queryFn: () => api.items(workspaceId, params),
    placeholderData: keepPreviousData,
  });
  const facets = useQuery({ queryKey: facetsKey(workspaceId), queryFn: () => api.itemFacets(workspaceId) });
  const clusters = useQuery({ queryKey: clustersKey(workspaceId), queryFn: () => api.clusters(workspaceId) });
  // Where the last bulk cluster action put the selected items.
  const [clustered, setClustered] = useState<ClusterRef | null>(null);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  // A selection only makes sense for the rows on screen.
  const searchString = search.toString();
  useEffect(() => setSelected(new Set()), [searchString, workspaceId]);

  /** Change filters in the URL. Any filter change goes back to page 1. */
  const update = useCallback(
    (change: (next: URLSearchParams) => void, options?: { replace?: boolean }) => {
      setSearch(
        (current) => {
          const next = new URLSearchParams(current);
          change(next);
          next.delete("page");
          return next;
        },
        options,
      );
    },
    [setSearch],
  );

  function goToPage(page: number) {
    setSearch((current) => {
      const next = new URLSearchParams(current);
      if (page > 1) next.set("page", String(page));
      else next.delete("page");
      return next;
    });
  }

  const group = GROUPS.find(([value]) => value === search.get("group"))?.[0];
  const page = items.data;
  const rows = page?.items ?? [];
  // Groups folded away, by heading; a different grouping starts unfolded.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  useEffect(() => setCollapsed(new Set()), [group]);
  const pageCount = page ? Math.max(1, Math.ceil(page.total / PAGE_SIZE)) : 1;

  function toggleGroup(group: string) {
    setCollapsed((current) => {
      const next = new Set(current);
      if (!next.delete(group)) next.add(group);
      return next;
    });
  }

  function toggle(key: string, on: boolean) {
    setSelected((current) => {
      const next = new Set(current);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  return (
    <>
      <div className="page-header">
        <h1>{workspace?.name ?? "Backlog"}</h1>
        {workspace && (
          <>
            <span className="mono hint">{workspace.jql}</span>
            <Link to={`/workspaces/${workspaceId}/clusters`}>Clusters</Link>
            <Link to={`/workspaces/${workspaceId}/edit`}>Edit workspace</Link>
          </>
        )}
        <div className="header-jobs">
          <ImportButton workspaceId={workspaceId} />
          <PrescanButton workspaceId={workspaceId} />
          <JobsPopup workspaceId={workspaceId} />
        </div>
      </div>
      <div className="count-bars">
        <TriageBar workspaceId={workspaceId} search={search} update={update} />
        <BuildBar workspaceId={workspaceId} search={search} update={update} />
      </div>
      <div className="filter-row">
        <Filters search={search} facets={facets.data} clusters={clusters.data} update={update} />
        <div className="view-options" role="group" aria-label="View">
          <select aria-label="Sort" value={search.get("sort") ?? "key"} onChange={(e) => update((next) => next.set("sort", e.target.value))}>
            {SORTS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <select
            aria-label="Group"
            value={group ?? ""}
            onChange={(e) =>
              update((next) => {
                if (e.target.value) next.set("group", e.target.value);
                else next.delete("group");
              })
            }
          >
            <option value="">No grouping</option>
            {GROUPS.map(([value, label]) => (
              <option key={value} value={value}>
                Group by {label.toLowerCase()}
              </option>
            ))}
          </select>
        </div>
      </div>
      {/* Stays on top while scrolling the list, so the selection can be acted on from anywhere. */}
      <div className="backlog-toolbar">
        {selected.size > 0 && (
          <BulkActions
            workspaceId={workspaceId}
            keys={[...selected]}
            clusters={clusters.data ?? []}
            onStateChanged={() => {
              setSelected(new Set());
              void queryClient.invalidateQueries({ queryKey: itemsKey(workspaceId) });
            }}
            onClustered={(cluster) => {
              setSelected(new Set());
              setClustered(cluster);
            }}
          />
        )}
        {selected.size === 0 && clustered && (
          <span role="status">
            Saved to cluster <Link to={`/workspaces/${workspaceId}/clusters/${clustered.id}`}>{clustered.name}</Link>
          </span>
        )}
      </div>

      {items.isPending && <p>Loading…</p>}
      {items.isError && <p className="error">{items.error.message}</p>}
      {page && page.total === 0 && (
        <p>{hasFilters(search) ? "No items match the filters." : "No items yet. Import from Jira to fill the backlog."}</p>
      )}
      {rows.length > 0 && (
        <table className="table backlog">
          <thead>
            <tr>
              <th>
                <input
                  type="checkbox"
                  aria-label="Select all on this page"
                  checked={rows.every((r) => selected.has(r.key))}
                  onChange={(e) => setSelected(e.target.checked ? new Set(rows.map((r) => r.key)) : new Set())}
                />
              </th>
              <th>Key</th>
              <th>Summary</th>
              <th>Jira status</th>
              <th>State</th>
              <th>Labels</th>
              <th>Diffs</th>
              <th>Target project</th>
              <th>Pre-scan</th>
              <th>Build</th>
              <th>Cluster</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item, i) => {
              const heading = group && groupOf(item, group);
              const folded = heading !== undefined && collapsed.has(heading);
              return (
                <Fragment key={item.id}>
                  {heading !== undefined && (i === 0 || heading !== groupOf(rows[i - 1], group!)) && (
                    <tr className="group-row">
                      <th colSpan={COLUMNS} scope="rowgroup">
                        <button className="link-button" aria-expanded={!folded} onClick={() => toggleGroup(heading)}>
                          <span aria-hidden="true">{folded ? "▸" : "▾"}</span> {heading}
                        </button>
                        <span className="group-count">{rows.filter((r) => groupOf(r, group!) === heading).length}</span>
                      </th>
                    </tr>
                  )}
                  {!folded && <Row workspaceId={workspaceId} item={item} selected={selected.has(item.key)} toggle={toggle} />}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
      {page && page.total > 0 && (
        <div className="pagination">
          <button onClick={() => goToPage(currentPage(search) - 1)} disabled={currentPage(search) <= 1}>
            Previous
          </button>
          <span>
            {page.offset + 1}–{page.offset + rows.length} of {page.total}
          </span>
          <button onClick={() => goToPage(currentPage(search) + 1)} disabled={currentPage(search) >= pageCount}>
            Next
          </button>
        </div>
      )}
    </>
  );
}

// Jira only hands us the status name, so colour by the usual workflow words.
function statusClass(status: string): string {
  const name = status.toLowerCase();
  if (/done|closed|resolved|fixed|merged/.test(name)) return "badge badge-done";
  if (/progress|review|testing|verify/.test(name)) return "badge badge-active";
  return "badge";
}

interface RowProps {
  workspaceId: number;
  item: Item;
  selected: boolean;
  toggle: (key: string, on: boolean) => void;
}

function Row({ workspaceId, item, selected, toggle }: RowProps) {
  return (
    <tr className={item.dropped ? "dropped" : undefined}>
      <td>
        <input type="checkbox" aria-label={`Select ${item.key}`} checked={selected} onChange={(e) => toggle(item.key, e.target.checked)} />
      </td>
      <td className="nowrap">
        <Link to={`/workspaces/${workspaceId}/items/${item.key}`}>{item.key}</Link>
      </td>
      <td>
        <Link to={`/workspaces/${workspaceId}/items/${item.key}`} className="summary-link">
          {item.summary}
        </Link>
        {item.dropped && (
          <span className="tag tag-warn" title="No longer matched by the workspace JQL">
            dropped
          </span>
        )}
        {item.last_import_error && (
          <span className="tag tag-error" title={item.last_import_error}>
            import error
          </span>
        )}
        {item.diff_subjects.map((subject) => (
          <div key={subject} className="diff-subject" title="Commit subject from the patch">
            {subject}
          </div>
        ))}
      </td>
      <td className="nowrap">
        <span className={statusClass(item.status)}>{item.status}</span>
      </td>
      <td className="nowrap">{STATE_LABELS[item.local_state]}</td>
      <td>{item.labels.join(", ")}</td>
      <td>{item.diff_count || ""}</td>
      <td className="mono">
        {item.target_projects.map((project) => (
          <div key={project}>{project}</div>
        ))}
      </td>
      <td className="nowrap">
        <TriageTags item={item} />
      </td>
      <td className="nowrap">
        <BuildTag status={item.build_status} reason={item.build_reason} />
      </td>
      <td>{item.cluster && <Link to={`/workspaces/${workspaceId}/clusters/${item.cluster.id}`}>{item.cluster.name}</Link>}</td>
      <td className="nowrap">{new Date(item.jira_updated).toLocaleDateString()}</td>
    </tr>
  );
}

interface BulkProps {
  workspaceId: number;
  keys: string[];
  clusters: Cluster[];
  onStateChanged: () => void;
  onClustered: (cluster: ClusterRef) => void;
}

function BulkActions({ workspaceId, keys, clusters, onStateChanged, onClustered }: BulkProps) {
  const [state, setState] = useState<LocalState | "">("");
  const changeState = useMutation({
    mutationFn: (to: LocalState) => api.changeState(workspaceId, keys, to),
    onSuccess: () => {
      setState("");
      onStateChanged();
    },
  });
  const reimport = useStartImport(workspaceId);
  const prescan = useStartPrescan(workspaceId);
  const build = useStartBuild(workspaceId);
  const refreshClusters = useRefreshClusters(workspaceId);
  const [target, setTarget] = useState("");
  // No target cluster: a new one from the selection.
  const cluster = useMutation({
    mutationFn: (clusterId: number | null) =>
      confirmingDissolve((confirm) =>
        clusterId === null
          ? api.createCluster(workspaceId, keys, confirm)
          : api.addClusterMembers(workspaceId, clusterId, keys, confirm),
      ),
    onSuccess: (result) => {
      if (result === null) return;
      refreshClusters();
      onClustered({ id: result.id, name: result.name });
    },
  });

  return (
    <div className="bulk-actions" role="group" aria-label="Bulk actions">
      <span>{keys.length} selected</span>
      <select aria-label="New state" value={state} onChange={(e) => setState(e.target.value as LocalState | "")}>
        <option value="">Set state…</option>
        {(Object.entries(STATE_LABELS) as [LocalState, string][]).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
      <button onClick={() => state && changeState.mutate(state)} disabled={!state || changeState.isPending}>
        Apply
      </button>
      <button onClick={() => reimport.mutate(keys)} disabled={reimport.isPending}>
        Reimport selected
      </button>
      <button onClick={() => prescan.mutate(keys)} disabled={prescan.isPending}>
        Pre-scan selected
      </button>
      <button onClick={() => build.mutate({ keys })} disabled={build.isPending} title="Items the pre-scan found applying, as is or 3-way">
        Build selected
      </button>
      <button
        onClick={() => cluster.mutate(null)}
        disabled={keys.length < 2 || cluster.isPending}
        title={keys.length < 2 ? "A cluster needs at least two items" : undefined}
      >
        New cluster from selected
      </button>
      {clusters.length > 0 && (
        <>
          <select aria-label="Target cluster" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">Add to cluster…</option>
            {clusters.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <button onClick={() => target && cluster.mutate(Number(target))} disabled={!target || cluster.isPending}>
            Add
          </button>
        </>
      )}
      {changeState.isError && <span className="error">{changeState.error.message}</span>}
      {cluster.isError && <span className="error">{cluster.error.message}</span>}
      {reimport.isError && <span className="error">{reimport.error.message}</span>}
      {prescan.isError && <span className="error">{prescan.error.message}</span>}
      {build.isError && <span className="error">{build.error.message}</span>}
    </div>
  );
}
