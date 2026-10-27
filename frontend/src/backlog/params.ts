import type { LocalState } from "../api";

export const PAGE_SIZE = 50;

/** Multi-value filters: repeated URL params, e.g. `?label=a&label=b`. */
export const LIST_FILTERS = ["status", "label", "triage", "build"] as const;
export type ListFilter = (typeof LIST_FILTERS)[number];

const FILTERS = [...LIST_FILTERS, "q", "has_error", "cluster", "stale"];
// URL params the items API understands; `page` is turned into offset/limit.
const API_PARAMS = [...FILTERS, "sort", "group"];

export const STATE_LABELS: Record<LocalState, string> = {
  new: "New",
  not_relevant: "Not relevant",
  closed: "Closed",
};

export function currentPage(search: URLSearchParams): number {
  return Math.max(1, Math.floor(Number(search.get("page"))) || 1);
}

/** Items API query for the backlog URL. The URL is the single source of truth for filters. */
export function apiParams(search: URLSearchParams): URLSearchParams {
  const params = new URLSearchParams();
  for (const [name, value] of search) {
    if (API_PARAMS.includes(name) && value !== "") params.append(name, value);
  }
  params.set("offset", String((currentPage(search) - 1) * PAGE_SIZE));
  params.set("limit", String(PAGE_SIZE));
  return params;
}

export function hasFilters(search: URLSearchParams): boolean {
  return FILTERS.some((name) => search.has(name));
}
