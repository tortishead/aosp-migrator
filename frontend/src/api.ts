export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api${path}`, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
      // FastAPI request validation errors: [{loc, msg, ...}]
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      // Not JSON; keep the status text.
    }
    throw new ApiError(response.status, detail || `HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export type Source = "env" | "file" | "unset";

export interface Settings {
  jira: {
    base_url: { source: Source; value: string | null };
    pat: { source: Source; is_set: boolean };
  };
  prescan: {
    /** The domains in effect: the defaults while unset. */
    upstream_domains: { source: Source; value: string[] };
  };
}

export interface JiraTestResult {
  ok: boolean;
  user?: string;
  error?: string;
}

export type TreeType = "repo" | "git" | "plain";

export interface Workspace {
  id: number;
  name: string;
  jql: string;
  target_tree: string;
  target_tree_type: TreeType;
  source_tree: string | null;
  source_tree_type: TreeType | null;
  // Followed link types (type name or direction); empty means all.
  link_types: string[];
  created_at: string;
  updated_at: string;
}

export interface WorkspaceInput {
  name: string;
  jql: string;
  target_tree: string;
  source_tree: string;
  link_types: string[];
}

export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
}

export type LocalState = "new" | "not_relevant" | "closed";

export interface ClusterRef {
  id: number;
  name: string;
}

export interface Item {
  id: number;
  key: string;
  summary: string;
  status: string;
  labels: string[];
  components: string[];
  fix_versions: string[];
  jira_updated: string;
  local_state: LocalState;
  dropped: boolean;
  last_import_error: string | null;
  diff_count: number;
  /** Commit subjects from the format-patch headers of the current diffs. */
  diff_subjects: string[];
  cluster: ClusterRef | null;
  /** Null until pre-scanned, and for items without diffs. */
  triage_label: TriageLabel | null;
  triage_reason: string | null;
  /** Worst apply result over the current diffs that were apply-checked; null when none was. */
  apply: ApplyResult | null;
  /** Why the pre-scan results are out of date; empty when fresh or never scanned. */
  stale: string[];
}

export interface ItemPage {
  items: Item[];
  total: number;
  offset: number;
  limit: number;
}

export interface Facets {
  statuses: string[];
  labels: string[];
  components: string[];
  fix_versions: string[];
}

export interface JiraComment {
  id: number;
  author: string;
  created: string;
  body_html: string;
}

export type FileStatus = "modified" | "added" | "deleted" | "renamed" | "copied";

export interface FileChange {
  path: string;
  old_path: string | null;
  status: FileStatus;
  added: number;
  removed: number;
  binary: boolean;
}

/** Email-style header of one format-patch commit. */
export interface Commit {
  sha: string;
  author: string | null;
  /** ISO 8601 with offset; the raw header if it could not be parsed. */
  date: string | null;
  subject: string;
  message: string;
}

export interface ParsedDiff {
  format: "format-patch" | "plain";
  files: FileChange[];
  /** Missing in diffs parsed before commits were kept; reimport fills it. */
  commits?: Commit[];
}

/** A diff as the viewer gets it: each file also carries the raw text of its hunks. */
export interface DiffWithHunks {
  format: ParsedDiff["format"];
  files: (FileChange & { hunks: string[] })[];
  commits: Commit[];
}

export interface Attachment {
  id: number;
  jira_id: string;
  filename: string;
  size: number;
  mime: string;
  kind: "diff" | "image" | "other";
  jira_url: string;
  // Not downloaded: null.
  local_url: string | null;
  is_current: boolean;
  created: string;
  parsed: ParsedDiff | null;
}

export interface Issue {
  key: string;
  summary: string;
  description_html: string;
  status: string;
  labels: string[];
  components: string[];
  fix_versions: string[];
  jira_updated: string;
  fetched_at: string;
  browse_url: string | null;
  comments: JiraComment[];
  attachments: Attachment[];
}

export interface IssueLink {
  link_type: string;
  direction: string;
  description: string;
  // Set when the linked ticket is a backlog item too.
  item_id: number | null;
  // That backlog item's cluster.
  cluster: ClusterRef | null;
  issue: Issue;
}

export interface Note {
  id: number;
  text: string;
  created_at: string;
}

export interface HistoryEvent {
  id: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ItemCluster extends ClusterRef {
  // All members including this item, in migration order.
  members: { key: string; summary: string; local_state: LocalState }[];
}

export interface ItemDetail {
  id: number;
  local_state: LocalState;
  dropped: boolean;
  last_import_error: string | null;
  created_at: string;
  jira_base_url: string | null;
  issue: Issue;
  cluster: ItemCluster | null;
  links: IssueLink[];
  notes: Note[];
  // Oldest first.
  history: HistoryEvent[];
}

export interface ClusterMember {
  item_id: number;
  key: string;
  summary: string;
  status: string;
  local_state: LocalState;
  dropped: boolean;
  diff_count: number;
}

export interface Cluster {
  id: number;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  // Migration order.
  members: ClusterMember[];
}

export interface ClusterDetail extends Cluster {
  // Member keys per changed path, in member order.
  files: { path: string; touched_by: string[] }[];
  notes: Note[];
  // Oldest first.
  history: HistoryEvent[];
}

export type JobStatus = "running" | "done" | "failed" | "canceled";

export interface ImportJob {
  id: number;
  workspace_id: number;
  scope: "all" | "keys";
  issue_keys: string[];
  status: JobStatus;
  done: number;
  total: number | null;
  errors: { key: string; message: string }[];
  error: string | null;
  cancel_requested: boolean;
  started_at: string;
  finished_at: string | null;
  /** Seconds left while a running job waits out Jira's rate limit. */
  rate_limit_wait: number | null;
}

export interface PrescanJob {
  id: number;
  workspace_id: number;
  scope: "all" | "keys";
  issue_keys: string[];
  status: JobStatus;
  /** While running: `projects` (git/plain trees only), then `scan` (repo mapping, already-present check). */
  stage: "projects" | "scan" | null;
  done: number;
  total: number | null;
  errors: { key: string; message: string }[];
  error: string | null;
  cancel_requested: boolean;
  started_at: string;
  finished_at: string | null;
}

export type Presence = "present" | "likely_present" | "not_present" | "partial" | "unknown";
export type ApplyResult = "applies" | "3way" | "conflicts";
/** Worst first. */
export const TRIAGE_LABELS = [
  "error",
  "unmapped",
  "unknown",
  "conflicts",
  "partial",
  "3way",
  "applies",
  "likely_present",
  "present",
] as const;
export type TriageLabel = (typeof TRIAGE_LABELS)[number];

export interface PrescanSummary {
  /** Items per label, every label in order, zeros included. Dropped items are not counted. */
  labels: Record<TriageLabel, number>;
  /** Not scanned yet, or without diffs. */
  unlabeled: number;
  stale: number;
}

export interface DiffScan {
  attachment_id: number;
  jira_id: string;
  filename: string;
  /** Null while this diff version has not been scanned. */
  scanned_at: string | null;
  /** Target project path; null while unmapped. */
  project: string | null;
  mapping_source: "hint" | "files" | "copilot" | "manual" | null;
  /** Projects to choose from while unmapped. */
  candidates: string[];
  presence: Presence | null;
  presence_detail: {
    files?: Record<string, "present" | "likely_present" | "not_present" | "unknown">;
    history?: { subject: string; match: "change_id" | "subject" | "patch_id" | null; sha: string | null }[];
    /** Content heuristic evidence per file it checked; first few missing / remaining lines. */
    heuristic?: Record<
      string,
      { added: { found: number; total: number; missing: string[] }; removed: { gone: number; total: number; remaining: string[] } }
    >;
    reason?: string;
  } | null;
  /** Null when present, unmapped or not checked. */
  apply: ApplyResult | null;
  /** Per checked file; `errors` says why each conflicting file conflicts. */
  apply_detail: { files: Record<string, ApplyResult>; errors?: Record<string, string> } | null;
  target_head: string | null;
}

export interface ItemPrescan {
  scanned: boolean;
  label: TriageLabel | null;
  label_reason: string | null;
  /** Why the results are out of date; empty when fresh. */
  stale: string[];
  diffs: DiffScan[];
}

export const api = {
  authStatus: () => apiFetch<AuthStatus>("/auth/status"),
  login: (token: string) =>
    apiFetch<{ authenticated: boolean }>("/auth/login", { method: "POST", body: JSON.stringify({ token }) }),
  logout: () => apiFetch<{ authenticated: boolean }>("/auth/logout", { method: "POST" }),
  settings: () => apiFetch<Settings>("/settings"),
  updateSettings: (update: { jira?: { base_url?: string; pat?: string }; prescan?: { upstream_domains: string } }) =>
    apiFetch<Settings>("/settings", { method: "PUT", body: JSON.stringify(update) }),
  testJira: (values: { base_url?: string; pat?: string }) =>
    apiFetch<JiraTestResult>("/settings/jira/test", { method: "POST", body: JSON.stringify(values) }),
  workspaces: () => apiFetch<Workspace[]>("/workspaces"),
  workspace: (id: number) => apiFetch<Workspace>(`/workspaces/${id}`),
  createWorkspace: (input: WorkspaceInput) =>
    apiFetch<Workspace>("/workspaces", { method: "POST", body: JSON.stringify(input) }),
  updateWorkspace: (id: number, input: WorkspaceInput) =>
    apiFetch<Workspace>(`/workspaces/${id}`, { method: "PUT", body: JSON.stringify(input) }),
  deleteWorkspace: (id: number) => apiFetch<void>(`/workspaces/${id}`, { method: "DELETE" }),
  items: (workspaceId: number, params: URLSearchParams) =>
    apiFetch<ItemPage>(`/workspaces/${workspaceId}/items?${params}`),
  itemFacets: (workspaceId: number) => apiFetch<Facets>(`/workspaces/${workspaceId}/items/facets`),
  changeState: (workspaceId: number, keys: string[], state: LocalState) =>
    apiFetch<{ changed: string[] }>(`/workspaces/${workspaceId}/items/state`, {
      method: "POST",
      body: JSON.stringify({ keys, state }),
    }),
  item: (workspaceId: number, key: string) =>
    apiFetch<ItemDetail>(`/workspaces/${workspaceId}/items/${encodeURIComponent(key)}`),
  addNote: (workspaceId: number, key: string, text: string) =>
    apiFetch<Note>(`/workspaces/${workspaceId}/items/${encodeURIComponent(key)}/notes`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  clusters: (workspaceId: number) => apiFetch<Cluster[]>(`/workspaces/${workspaceId}/clusters`),
  cluster: (workspaceId: number, clusterId: number) =>
    apiFetch<ClusterDetail>(`/workspaces/${workspaceId}/clusters/${clusterId}`),
  // `confirmDissolve`: allowed to dissolve a cluster that would keep fewer than two items (409 otherwise).
  createCluster: (workspaceId: number, keys: string[], confirmDissolve = false) =>
    apiFetch<ClusterDetail>(`/workspaces/${workspaceId}/clusters`, {
      method: "POST",
      body: JSON.stringify({ keys, confirm_dissolve: confirmDissolve }),
    }),
  updateCluster: (workspaceId: number, clusterId: number, update: { name?: string; description?: string }) =>
    apiFetch<ClusterDetail>(`/workspaces/${workspaceId}/clusters/${clusterId}`, {
      method: "PATCH",
      body: JSON.stringify(update),
    }),
  dissolveCluster: (workspaceId: number, clusterId: number) =>
    apiFetch<void>(`/workspaces/${workspaceId}/clusters/${clusterId}`, { method: "DELETE" }),
  addClusterMembers: (workspaceId: number, clusterId: number, keys: string[], confirmDissolve = false) =>
    apiFetch<ClusterDetail>(`/workspaces/${workspaceId}/clusters/${clusterId}/members`, {
      method: "POST",
      body: JSON.stringify({ keys, confirm_dissolve: confirmDissolve }),
    }),
  removeClusterMember: (workspaceId: number, clusterId: number, key: string, confirmDissolve = false) =>
    apiFetch<{ dissolved: boolean }>(
      `/workspaces/${workspaceId}/clusters/${clusterId}/members/${encodeURIComponent(key)}${confirmDissolve ? "?confirm_dissolve=true" : ""}`,
      { method: "DELETE" },
    ),
  reorderCluster: (workspaceId: number, clusterId: number, keys: string[]) =>
    apiFetch<ClusterDetail>(`/workspaces/${workspaceId}/clusters/${clusterId}/order`, {
      method: "PUT",
      body: JSON.stringify({ keys }),
    }),
  addClusterNote: (workspaceId: number, clusterId: number, text: string) =>
    apiFetch<Note>(`/workspaces/${workspaceId}/clusters/${clusterId}/notes`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  diff: (workspaceId: number, jiraId: string) =>
    apiFetch<DiffWithHunks>(`/workspaces/${workspaceId}/diffs/${encodeURIComponent(jiraId)}`),
  // No keys: import everything the workspace JQL matches.
  startImport: (workspaceId: number, keys?: string[]) =>
    apiFetch<ImportJob>(`/workspaces/${workspaceId}/import`, { method: "POST", body: JSON.stringify({ keys }) }),
  importJobs: (workspaceId: number, limit: number) =>
    apiFetch<ImportJob[]>(`/workspaces/${workspaceId}/import-jobs?limit=${limit}`),
  importJob: (jobId: number) => apiFetch<ImportJob>(`/import-jobs/${jobId}`),
  cancelImport: (jobId: number) => apiFetch<ImportJob>(`/import-jobs/${jobId}/cancel`, { method: "POST" }),
  // No keys: every backlog item that is not dropped.
  startPrescan: (workspaceId: number, keys?: string[]) =>
    apiFetch<PrescanJob>(`/workspaces/${workspaceId}/prescan`, { method: "POST", body: JSON.stringify({ keys }) }),
  prescanJobs: (workspaceId: number, limit: number) =>
    apiFetch<PrescanJob[]>(`/workspaces/${workspaceId}/prescan-jobs?limit=${limit}`),
  cancelPrescan: (jobId: number) => apiFetch<PrescanJob>(`/prescan-jobs/${jobId}/cancel`, { method: "POST" }),
  prescanSummary: (workspaceId: number) => apiFetch<PrescanSummary>(`/workspaces/${workspaceId}/prescan-summary`),
  itemPrescan: (workspaceId: number, key: string) =>
    apiFetch<ItemPrescan>(`/workspaces/${workspaceId}/items/${encodeURIComponent(key)}/prescan`),
  targetProjects: (workspaceId: number) => apiFetch<string[]>(`/workspaces/${workspaceId}/target-projects`),
  // Null project: back to the automatic mapping. The item is pre-scanned again before this returns.
  pickProject: (workspaceId: number, key: string, attachmentId: number, project: string | null) =>
    apiFetch<ItemPrescan>(`/workspaces/${workspaceId}/items/${encodeURIComponent(key)}/diffs/${attachmentId}/project`, {
      method: "PUT",
      body: JSON.stringify({ project }),
    }),
};
