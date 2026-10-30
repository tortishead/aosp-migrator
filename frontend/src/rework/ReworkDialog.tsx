import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { api, RUNGS, type PlanItem, type ReworkPlan, type ReworkRequest, type Rung } from "../api";
import { RUNG_TEXT } from "./rework";
import { useStartRework } from "./ReworkPanel";

type Caps = ReworkPlan["caps"];

const CAPS: [keyof Caps, string, number, number][] = [
  ["item_cap", "Copilot calls per item", 1, 50],
  ["job_cap", "Copilot calls for the job", 1, 10000],
  ["build_rounds", "Build-fix rounds", 0, 10],
];

interface Props {
  workspaceId: number;
  keys: string[];
  onClose: () => void;
}

/**
 * What a rework of the selected items would do, from the server's plan: units in order, the prerequisites outside the selection
 * (added by default, closed / not relevant ones only when ticked), Copilot rungs (preselected by the plan), caps and the estimate.
 */
export function ReworkDialog({ workspaceId, keys, onClose }: Props) {
  const [includeDependents, setIncludeDependents] = useState(false);
  const [together, setTogether] = useState(false);
  const [include, setInclude] = useState<string[]>([]);
  const [exclude, setExclude] = useState<string[]>([]);
  // Null until the user changes them: the plan's.
  const [rungs, setRungs] = useState<Rung[] | null>(null);
  // Caps the user typed; empty: the settings.
  const [caps, setCaps] = useState<Partial<Record<keyof Caps, string>>>({});
  const start = useStartRework(workspaceId);

  const capValues = Object.fromEntries(
    Object.entries(caps)
      .filter(([, v]) => v !== undefined && v.trim() !== "")
      .map(([k, v]) => [k, Number(v)]),
  ) as Partial<Caps>;
  const request: ReworkRequest = {
    keys,
    include_dependents: includeDependents,
    include,
    exclude,
    together,
    ...capValues,
  };
  const plan = useQuery({
    queryKey: ["reworkPlan", workspaceId, request],
    queryFn: () => api.reworkPlan(workspaceId, request),
    placeholderData: keepPreviousData,
    retry: false,
  });
  const chosen = rungs ?? plan.data?.rungs ?? [];

  useEffect(() => {
    const close = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [onClose]);

  function togglePrerequisite(p: PlanItem, on: boolean) {
    // A closed / not relevant one is left out unless ticked; any other is added unless unticked.
    if (p.warning) setInclude((current) => (on ? [...current, p.key] : current.filter((k) => k !== p.key)));
    else setExclude((current) => (on ? current.filter((k) => k !== p.key) : [...current, p.key]));
  }

  function toggleRung(rung: Rung, on: boolean) {
    setRungs(RUNGS.filter((r) => (r === rung ? on : chosen.includes(r))));
  }

  const data = plan.data;
  const notReworked = data?.items.filter((i) => i.selected && !i.rework) ?? [];
  const invalidCap = CAPS.find(([name, , low, high]) => {
    const value = capValues[name];
    return value !== undefined && !(Number.isInteger(value) && value >= low && value <= high);
  });

  return createPortal(
    <div className="popup-backdrop" onClick={onClose}>
      <div className="popup" role="dialog" aria-modal="true" aria-label="Rework" onClick={(e) => e.stopPropagation()}>
        <div className="popup-header">
          <h2>Rework {keys.length === 1 ? keys[0] : `${keys.length} items`}</h2>
          <button type="button" className="link-button" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="hint">
          Makes a patch that applies for each item: first stacked on its prerequisites and merged with the source base, then with
          Copilot on only what still fails. The patches are proposed for review.
        </p>
        {plan.isError && <p className="error">{plan.error.message}</p>}
        {plan.isPending && <p>Planning…</p>}
        {data && (
          <>
            <section aria-label="Units">
              <h3>Reworked in this order</h3>
              {data.units.length === 0 ? (
                <p className="hint">Nothing to rework.</p>
              ) : (
                <ol className="stack-order">
                  {data.units.map((unit) => (
                    <li key={unit.join(",")}>
                      <strong>{unit.join(" + ")}</strong>
                      {unit.length > 1 && <span className="tag">together</span>}
                      {unit.some((k) => data.items.find((i) => i.key === k)?.dependent) && <span className="tag">dependent</span>}
                    </li>
                  ))}
                </ol>
              )}
              {notReworked.length > 0 && (
                <ul className="hint">
                  {notReworked.map((i) => (
                    <li key={i.key}>
                      {i.key}: {i.reason}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {data.prerequisites.length > 0 && (
              <section aria-label="Prerequisites">
                <h3>Prerequisites outside the selection</h3>
                <ul className="filter-options">
                  {data.prerequisites.map((p) => (
                    <li key={p.key}>
                      <label>
                        <input
                          type="checkbox"
                          aria-label={`Include ${p.key}`}
                          checked={p.included}
                          onChange={(e) => togglePrerequisite(p, e.target.checked)}
                        />
                        <strong>{p.key}</strong> {p.summary}
                      </label>{" "}
                      {p.warning && <span className="tag tag-warn">{p.warning}</span>}
                      {p.accepted ? (
                        <span className="tag tag-ok">accepted patch</span>
                      ) : (
                        p.has_patch && <span className="tag">has a patch</span>
                      )}
                      {p.stale.length > 0 && (
                        <span className="tag tag-warn" title={p.stale.join("; ")}>
                          stale
                        </span>
                      )}
                      {p.reason && <div className="hint">{p.reason}</div>}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}

        <section aria-label="Options" className="form">
          <label>
            <input type="checkbox" checked={includeDependents} onChange={(e) => setIncludeDependents(e.target.checked)} /> Also rework the
            items that need these
          </label>
          {keys.length > 1 && (
            <label>
              <input type="checkbox" checked={together} onChange={(e) => setTogether(e.target.checked)} /> Rework the selected items
              together, as one unit
            </label>
          )}
          <fieldset>
            <legend>Copilot rungs</legend>
            {RUNGS.map((rung) => (
              <label key={rung} title={RUNG_TEXT[rung].hint}>
                <input type="checkbox" checked={chosen.includes(rung)} onChange={(e) => toggleRung(rung, e.target.checked)} />{" "}
                {RUNG_TEXT[rung].name}
              </label>
            ))}
            {chosen.length === 0 && <p className="hint">Without Copilot: only stacking and the source base.</p>}
          </fieldset>
          <fieldset>
            <legend>Caps</legend>
            {CAPS.map(([name, label, low, high]) => (
              <label key={name}>
                {label}{" "}
                <input
                  type="number"
                  min={low}
                  max={high}
                  value={caps[name] ?? ""}
                  placeholder={data ? String(data.caps[name]) : undefined}
                  onChange={(e) => setCaps((current) => ({ ...current, [name]: e.target.value }))}
                />
              </label>
            ))}
          </fieldset>
        </section>

        {data && (
          <p role="status" aria-label="Estimate">
            {data.estimate.units} {data.estimate.units === 1 ? "unit" : "units"}, {data.estimate.items}{" "}
            {data.estimate.items === 1 ? "item" : "items"}; Copilot calls likely {data.estimate.likely_calls}, at most{" "}
            {data.estimate.max_calls}
          </p>
        )}
        <div className="actions">
          <button
            onClick={() => start.mutate({ ...request, rungs: chosen }, { onSuccess: onClose })}
            disabled={!data || plan.isError || data.units.length === 0 || invalidCap !== undefined || start.isPending}
            title={invalidCap ? `${invalidCap[1]}: ${invalidCap[2]} to ${invalidCap[3]}` : undefined}
          >
            Start rework
          </button>
          {start.isError && <span className="error">{start.error.message}</span>}
        </div>
      </div>
    </div>,
    document.body,
  );
}
