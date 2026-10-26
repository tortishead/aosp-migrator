import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router";

import { api, type Workspace, type WorkspaceInput } from "../api";
import { workspacesKey } from "../queryClient";

/** Create (`/workspaces/new`) or edit (`/workspaces/:id/edit`) a workspace. */
export function WorkspaceFormPage() {
  const { workspaceId } = useParams();
  const id = workspaceId ? Number(workspaceId) : undefined;
  const workspace = useQuery({
    queryKey: [...workspacesKey, id],
    queryFn: () => api.workspace(id!),
    enabled: id !== undefined,
  });

  if (id === undefined) return <WorkspaceForm />;
  if (workspace.isPending) return <p>Loading…</p>;
  if (workspace.isError) return <p className="error">{workspace.error.message}</p>;
  return <WorkspaceForm workspace={workspace.data} />;
}

function WorkspaceForm({ workspace }: { workspace?: Workspace }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Omit<WorkspaceInput, "link_types">>({
    name: workspace?.name ?? "",
    jql: workspace?.jql ?? "",
    target_tree: workspace?.target_tree ?? "",
    repo_root: workspace?.repo_root ?? "",
    source_tree: workspace?.source_tree ?? "",
  });
  const [linkTypes, setLinkTypes] = useState(workspace?.link_types.join(", ") ?? "");

  const save = useMutation({
    mutationFn: (input: WorkspaceInput) =>
      workspace ? api.updateWorkspace(workspace.id, input) : api.createWorkspace(input),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: workspacesKey });
      navigate("/");
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteWorkspace(workspace!.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: workspacesKey });
      navigate("/");
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate({ ...values, link_types: linkTypes.split(",").map((t) => t.trim()).filter(Boolean) });
  }

  function confirmDelete() {
    if (window.confirm(`Delete workspace "${workspace!.name}"?`)) remove.mutate();
  }

  const field = (key: keyof typeof values) => ({
    value: values[key],
    onChange: (e: { target: { value: string } }) => setValues((v) => ({ ...v, [key]: e.target.value })),
  });

  return (
    <>
      <h1>{workspace ? `Edit ${workspace.name}` : "New workspace"}</h1>
      <form onSubmit={submit} className="form">
        <label>
          Name
          <input required {...field("name")} />
        </label>
        <label>
          JQL
          <textarea required rows={3} placeholder="project = OEM AND labels = migrate" {...field("jql")} />
        </label>
        <label>
          Target tree path
          <input required placeholder="/home/me/aosp-15" {...field("target_tree")} />
        </label>
        <p className="hint">Tree the patches are migrated to. Must be an existing directory on this machine.</p>
        <label>
          .repo path (optional)
          <input placeholder="/home/me/aosp-15" {...field("repo_root")} />
        </label>
        <p className="hint">
          Repo checkout holding the target tree, when the target tree is a subdirectory of it (e.g. target tree
          /home/me/aosp-15/qssi). Its .repo/project.list gives the projects. Empty: the target tree's own .repo, if any.
        </p>
        <label>
          Source tree path (optional)
          <input placeholder="/home/me/aosp-14" {...field("source_tree")} />
        </label>
        <label>
          Relevant link types
          <input placeholder="All" value={linkTypes} onChange={(e) => setLinkTypes(e.target.value)} />
        </label>
        <p className="hint">
          Comma-separated link type names or directions, e.g. "Relates, is blocked by". Only these linked tickets are
          imported and shown. Empty means all. Applies on the next import.
        </p>

        <div className="actions">
          <button type="submit" disabled={save.isPending}>
            {workspace ? "Save" : "Create"}
          </button>
          <button type="button" onClick={() => navigate("/")}>
            Cancel
          </button>
          {workspace && (
            <button type="button" className="danger" onClick={confirmDelete} disabled={remove.isPending}>
              Delete
            </button>
          )}
        </div>
        {save.isError && <p className="error">{save.error.message}</p>}
        {remove.isError && <p className="error">{remove.error.message}</p>}
      </form>
    </>
  );
}
