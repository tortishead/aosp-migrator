import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../api";

export const RESET_TITLE =
  "Back to how the import left it: state New, no pre-scan, build or rework results, only the import's history. Notes and clusters stay.";

export function confirmReset(keys: string[]): boolean {
  const what = keys.length === 1 ? keys[0] : `${keys.length} items`;
  return window.confirm(
    `Reset ${what} to how the import left it? Pre-scan, build and rework results, patches and history other than the import's are deleted.`,
  );
}

/** Reset items to how the import left them; everything derived from them reloads. */
export function useResetItems(workspaceId: number, onDone?: () => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (keys: string[]) => api.resetItems(workspaceId, keys),
    onSuccess: () => {
      void queryClient.invalidateQueries();
      onDone?.();
    },
  });
}
