import { useEffect, useState } from "react";

import { TRIAGE_LABELS, type Cluster, type Facets, type LocalState } from "../api";
import { TRIAGE_TEXT } from "../prescan/triage";
import { hasFilters, STATE_LABELS, type ListFilter } from "./params";

export const SEARCH_DEBOUNCE_MS = 300;

type Update = (change: (next: URLSearchParams) => void, options?: { replace?: boolean }) => void;

interface Props {
  search: URLSearchParams;
  facets: Facets | undefined;
  clusters: Cluster[] | undefined;
  update: Update;
}

export function Filters({ search, facets, clusters, update }: Props) {
  const states = Object.entries(STATE_LABELS) as [LocalState, string][];
  const triage: [string, string][] = [...TRIAGE_LABELS.map((l): [string, string] => [l, TRIAGE_TEXT[l]]), ["none", "Not scanned"]];
  return (
    <div className="filters" role="search">
      <SearchBox value={search.get("q") ?? ""} update={update} />
      <MultiFilter label="State" param="state" options={states} search={search} update={update} />
      <MultiFilter label="Jira status" param="status" options={pairs(facets?.statuses)} search={search} update={update} />
      <MultiFilter label="Labels" param="label" options={pairs(facets?.labels)} search={search} update={update} />
      <MultiFilter label="Components" param="component" options={pairs(facets?.components)} search={search} update={update} />
      <MultiFilter label="Fix version" param="fix_version" options={pairs(facets?.fix_versions)} search={search} update={update} />
      <FlagFilter label="Diff" param="has_diff" yes="With diff" no="Without diff" search={search} update={update} />
      <FlagFilter label="Import error" param="has_error" yes="With import error" no="Without import error" search={search} update={update} />
      <FlagFilter label="Dropped" param="dropped" yes="Dropped only" no="Not dropped" search={search} update={update} />
      <MultiFilter label="Pre-scan" param="triage" options={triage} search={search} update={update} />
      <FlagFilter label="Stale" param="stale" yes="Stale only" no="Not stale" search={search} update={update} />
      <ClusterFilter clusters={clusters} search={search} update={update} />
      {hasFilters(search) && (
        <button
          type="button"
          onClick={() =>
            update((next) => {
              for (const name of [...next.keys()]) if (name !== "sort") next.delete(name);
            })
          }
        >
          Clear filters
        </button>
      )}
    </div>
  );
}

function pairs(values: string[] | undefined): [string, string][] {
  return (values ?? []).map((v) => [v, v]);
}

function SearchBox({ value, update }: { value: string; update: Update }) {
  const [text, setText] = useState(value);
  // Follow URL changes from elsewhere (back button, "Clear filters").
  useEffect(() => setText(value), [value]);

  useEffect(() => {
    if (text.trim() === value.trim()) return;
    const timer = setTimeout(
      () =>
        update(
          (next) => {
            if (text.trim()) next.set("q", text.trim());
            else next.delete("q");
          },
          { replace: true },
        ),
      SEARCH_DEBOUNCE_MS,
    );
    return () => clearTimeout(timer);
  }, [text, value, update]);

  return (
    <input
      type="search"
      className="filter-search"
      aria-label="Search"
      placeholder="Key, summary, description, diff path"
      value={text}
      onChange={(e) => setText(e.target.value)}
    />
  );
}

interface MultiFilterProps {
  label: string;
  param: ListFilter;
  options: [string, string][];
  search: URLSearchParams;
  update: Update;
}

function MultiFilter({ label, param, options, search, update }: MultiFilterProps) {
  const selected = search.getAll(param);
  // Keep selected values that the facets no longer list (e.g. from an old link) so they can be unchecked.
  const all = [...options, ...selected.filter((v) => !options.some(([o]) => o === v)).map((v): [string, string] => [v, v])];

  function toggle(value: string, on: boolean) {
    update((next) => {
      const values = next.getAll(param).filter((v) => v !== value);
      next.delete(param);
      for (const v of on ? [...values, value] : values) next.append(param, v);
    });
  }

  return (
    <details className="filter">
      <summary>
        {label}
        {selected.length > 0 && ` (${selected.length})`}
      </summary>
      <div className="filter-options" role="group" aria-label={label}>
        {all.length === 0 && <span className="hint">No values</span>}
        {all.map(([value, text]) => (
          <label key={value}>
            <input type="checkbox" checked={selected.includes(value)} onChange={(e) => toggle(value, e.target.checked)} />
            {text}
          </label>
        ))}
      </div>
    </details>
  );
}

function ClusterFilter({ clusters, search, update }: { clusters: Cluster[] | undefined; search: URLSearchParams; update: Update }) {
  const value = search.get("cluster") ?? "";
  const known = value === "" || value === "any" || value === "none" || clusters?.some((c) => String(c.id) === value);
  return (
    <select
      aria-label="Cluster"
      value={value}
      onChange={(e) =>
        update((next) => {
          if (e.target.value) next.set("cluster", e.target.value);
          else next.delete("cluster");
        })
      }
    >
      <option value="">Cluster: any</option>
      <option value="any">In a cluster</option>
      <option value="none">Not in a cluster</option>
      {clusters?.map((c) => (
        <option key={c.id} value={c.id}>
          {c.name}
        </option>
      ))}
      {/* A cluster from an old link that no longer exists, so it can be cleared. */}
      {!known && <option value={value}>Cluster {value}</option>}
    </select>
  );
}

interface FlagFilterProps {
  label: string;
  param: string;
  yes: string;
  no: string;
  search: URLSearchParams;
  update: Update;
}

function FlagFilter({ label, param, yes, no, search, update }: FlagFilterProps) {
  return (
    <select
      aria-label={label}
      value={search.get(param) ?? ""}
      onChange={(e) =>
        update((next) => {
          if (e.target.value) next.set(param, e.target.value);
          else next.delete(param);
        })
      }
    >
      <option value="">{label}: any</option>
      <option value="true">{yes}</option>
      <option value="false">{no}</option>
    </select>
  );
}
