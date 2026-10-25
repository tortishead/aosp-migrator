import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { vi } from "vitest";

import { createQueryClient } from "./queryClient";
import { routes } from "./routes";

type Reply = unknown | { status: number; body: unknown };
type Handler = (body: unknown) => Reply;

export interface ApiCall {
  method: string;
  path: string;
  body: unknown;
}

/**
 * Stub fetch for `/api/*`. Keys look like "GET /settings"; a key without a query string
 * also answers requests with one. Returns the recorded calls (paths include the query).
 */
export function mockApi(handlers: Record<string, Handler>): ApiCall[] {
  const calls: ApiCall[] = [];
  const merged: Record<string, Handler> = {
    "GET /auth/status": () => ({ required: false, authenticated: true }),
    "GET /workspaces": () => [],
    ...handlers,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      const path = url.replace(/^\/api/, "");
      const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
      calls.push({ method, path, body });
      const handler = merged[`${method} ${path}`] ?? merged[`${method} ${path.split("?")[0]}`];
      if (!handler) return new Response(JSON.stringify({ detail: "Not mocked" }), { status: 404 });
      const reply = handler(body);
      if (reply === undefined) return new Response(null, { status: 204 });
      const isWrapped = typeof reply === "object" && reply !== null && "status" in reply && "body" in reply;
      const status = isWrapped ? (reply as { status: number }).status : 200;
      const payload = isWrapped ? (reply as { body: unknown }).body : reply;
      return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

export function renderRoute(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const result = render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...result, router };
}
