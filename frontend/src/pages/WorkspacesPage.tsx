import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { api, type TreeType } from "../api";
import { workspacesKey } from "../queryClient";

export function WorkspacesPage() {
  const workspaces = useQuery({ queryKey: workspacesKey, queryFn: api.workspaces });

  return (
    <>
      <div className="page-header">
        <h1>Workspaces</h1>
        <Link className="button-link" to="/workspaces/new">
          New workspace
        </Link>
      </div>
      {workspaces.isPending && <p>Loading…</p>}
      {workspaces.isError && <p className="error">{workspaces.error.message}</p>}
      {workspaces.data?.length === 0 && <p>No workspaces yet. Create one to import tickets.</p>}
      {!!workspaces.data?.length && (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>JQL</th>
              <th>Target tree</th>
              <th>Source tree</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {workspaces.data.map((w) => (
              <tr key={w.id}>
                <td>
                  <Link to={`/workspaces/${w.id}`}>{w.name}</Link>
                </td>
                <td className="mono">{w.jql}</td>
                <td>
                  <Tree path={w.target_tree} type={w.target_tree_type} />
                </td>
                <td>{w.source_tree && <Tree path={w.source_tree} type={w.source_tree_type} />}</td>
                <td>
                  <Link to={`/workspaces/${w.id}/edit`} aria-label={`Edit ${w.name}`}>
                    Edit
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function Tree({ path, type }: { path: string; type: TreeType | null }) {
  return (
    <>
      <span className="mono">{path}</span>
      {type && <span className="tag">{type}</span>}
    </>
  );
}
