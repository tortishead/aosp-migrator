import { useQueryClient } from "@tanstack/react-query";

import { ApiError, type LocalState } from "../api";
import { itemsKey } from "../backlog/ImportPanel";

export const clustersKey = (workspaceId: number) => ["clusters", workspaceId];
export const clusterKey = (workspaceId: number, clusterId: number) => ["cluster", workspaceId, clusterId];

/**
 * Run a cluster change. If the server answers 409 because it would dissolve a cluster left with
 * fewer than two items, ask first and retry with confirmation. Resolves to null when the user declines.
 */
export async function confirmingDissolve<T>(run: (confirmDissolve: boolean) => Promise<T>): Promise<T | null> {
  try {
    return await run(false);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 409) throw error;
    return window.confirm(`${error.message}. Continue?`) ? run(true) : null;
  }
}

/** Cluster changes show up in the cluster pages, the backlog and item pages. */
export function useRefreshClusters(workspaceId: number) {
  const queryClient = useQueryClient();
  return () => {
    for (const queryKey of [clustersKey(workspaceId), ["cluster", workspaceId], itemsKey(workspaceId), ["item", workspaceId]]) {
      void queryClient.invalidateQueries({ queryKey });
    }
  };
}

/** Members that later migration would skip or that need a second look. */
export function memberWarning(member: { local_state: LocalState; dropped?: boolean }): string | null {
  if (member.dropped) return "dropped";
  if (member.local_state === "not_relevant") return "not relevant";
  if (member.local_state === "closed") return "closed";
  return null;
}
