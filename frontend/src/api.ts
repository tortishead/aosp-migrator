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
  created_at: string;
  updated_at: string;
}

export interface WorkspaceInput {
  name: string;
  jql: string;
  target_tree: string;
  source_tree: string;
}

export interface AuthStatus {
  required: boolean;
  authenticated: boolean;
}

export type LocalState = "new" | "not_relevant" | "closed";

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
}

export const api = {
  authStatus: () => apiFetch<AuthStatus>("/auth/status"),
  login: (token: string) =>
    apiFetch<{ authenticated: boolean }>("/auth/login", { method: "POST", body: JSON.stringify({ token }) }),
  logout: () => apiFetch<{ authenticated: boolean }>("/auth/logout", { method: "POST" }),
  settings: () => apiFetch<Settings>("/settings"),
  updateSettings: (update: { jira: { base_url?: string; pat?: string } }) =>
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
  // No keys: import everything the workspace JQL matches.
  startImport: (workspaceId: number, keys?: string[]) =>
    apiFetch<ImportJob>(`/workspaces/${workspaceId}/import`, { method: "POST", body: JSON.stringify({ keys }) }),
  importJobs: (workspaceId: number, limit: number) =>
    apiFetch<ImportJob[]>(`/workspaces/${workspaceId}/import-jobs?limit=${limit}`),
  cancelImport: (jobId: number) => apiFetch<ImportJob>(`/import-jobs/${jobId}/cancel`, { method: "POST" }),
};
