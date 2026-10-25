import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router";

import { api } from "../api";
import { clustersKey, memberWarning } from "../clusters/shared";

export function ClustersPage() {
  const workspaceId = Number(useParams().workspaceId);
  const clusters = useQuery({ queryKey: clustersKey(workspaceId), queryFn: () => api.clusters(workspaceId) });

  return (
    <>
      <p>
        <Link to={`/workspaces/${workspaceId}`}>← Backlog</Link>
      </p>
      <div className="page-header">
        <h1>Clusters</h1>
      </div>
      {clusters.isPending && <p>Loading…</p>}
      {clusters.isError && <p className="error">{clusters.error.message}</p>}
      {clusters.data?.length === 0 && (
        <p>No clusters yet. Select items in the backlog and choose “New cluster from selected”.</p>
      )}
      {!!clusters.data?.length && (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Items, in migration order</th>
              <th>Warnings</th>
            </tr>
          </thead>
          <tbody>
            {clusters.data.map((c) => {
              const warnings = c.members.filter((m) => memberWarning(m)).length;
              return (
                <tr key={c.id}>
                  <td>
                    <Link to={`/workspaces/${workspaceId}/clusters/${c.id}`}>{c.name}</Link>
                  </td>
                  <td className="mono">{c.members.map((m) => m.key).join(" → ")}</td>
                  <td>
                    {warnings > 0 && (
                      <span className="tag tag-warn" title="Members that are not relevant, closed or dropped">
                        {warnings} to check
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}
