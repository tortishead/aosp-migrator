import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";

import { ApiError } from "./api";

export const authStatusKey = ["auth", "status"];
export const workspacesKey = ["workspaces"];

export function createQueryClient(): QueryClient {
  // Any 401 means the LAN session is gone: refetch auth status so the login page shows.
  const onError = (error: Error) => {
    if (error instanceof ApiError && error.status === 401) {
      void client.invalidateQueries({ queryKey: authStatusKey });
    }
  };
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return client;
}
