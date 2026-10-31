import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { api, type CopilotUsage, type CopilotUsageBucket } from "../api";
import { workspacesKey } from "../queryClient";

const PERIODS = [
  { days: 7, label: "Last 7 days" },
  { days: 30, label: "Last 30 days" },
  { days: 90, label: "Last 90 days" },
  { days: null, label: "All time" },
] as const;

const DEFAULT_DAYS = 30;

const RUNG_LABELS: Record<string, string> = { resolve: "Resolve", port: "Port", reimpl: "Re-implement" };

const integer = new Intl.NumberFormat("en-US");
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const decimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

export function formatTokens(n: number): string {
  return n < 10_000 ? integer.format(n) : compact.format(n);
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${decimal.format(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function percent(part: number, whole: number): string {
  return whole ? `${Math.round((part / whole) * 100)}%` : "–";
}

/** Every UTC day of the period, with zeros for days without calls. */
export function dailySeries(usage: CopilotUsage, today: Date = new Date()): { day: string; calls: number; premium: number; tokens: number }[] {
  const byDay = new Map(usage.by_day.map((d) => [d.day, d]));
  const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const first = usage.days ? new Date(end.getTime() - (usage.days - 1) * 86_400_000) : usage.by_day.length ? new Date(`${usage.by_day[0].day}T00:00:00Z`) : end;
  const out = [];
  for (let t = first.getTime(); t <= end.getTime(); t += 86_400_000) {
    const day = new Date(t).toISOString().slice(0, 10);
    const d = byDay.get(day);
    out.push({ day, calls: d?.calls ?? 0, premium: d?.premium_requests ?? 0, tokens: (d?.input_tokens ?? 0) + (d?.output_tokens ?? 0) });
  }
  return out;
}

export function CopilotPage() {
  const [params, setParams] = useSearchParams();
  const workspaceId = Number(params.get("workspace")) || null;
  const daysParam = params.get("days");
  const days = daysParam === "all" ? null : Number(daysParam) || DEFAULT_DAYS;

  const workspaces = useQuery({ queryKey: workspacesKey, queryFn: api.workspaces });
  const usage = useQuery({
    queryKey: ["copilot", "usage", workspaceId, days],
    queryFn: () => api.copilotUsage({ workspaceId, days }),
    placeholderData: (previous) => previous,
  });

  const update = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  return (
    <>
      <div className="page-header">
        <h1>Copilot usage</h1>
      </div>
      <div className="copilot-filters">
        <label>
          Workspace{" "}
          <select aria-label="Workspace filter" value={workspaceId ?? ""} onChange={(e) => update("workspace", e.target.value || null)}>
            <option value="">All workspaces</option>
            {workspaces.data?.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
        <div className="copilot-periods" role="group" aria-label="Period">
          {PERIODS.map((p) => (
            <button
              key={p.label}
              type="button"
              aria-pressed={days === p.days}
              onClick={() => update("days", p.days === DEFAULT_DAYS ? null : p.days === null ? "all" : String(p.days))}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>
      {usage.isPending && <p>Loading…</p>}
      {usage.isError && <p className="error">{usage.error.message}</p>}
      {usage.data && <Dashboard usage={usage.data} />}
    </>
  );
}

function Dashboard({ usage }: { usage: CopilotUsage }) {
  const t = usage.totals;
  if (t.calls === 0) {
    return (
      <p className="copilot-empty">
        No Copilot calls in this period. Calls are counted when a rework runs the Copilot rungs (resolve, port, re-implement).
      </p>
    );
  }
  const tokens = t.input_tokens + t.output_tokens;
  return (
    <>
      <div className="stat-tiles">
        <Stat label="Copilot calls" value={integer.format(t.calls)} note={`in ${integer.format(t.attempts)} rework attempt${t.attempts === 1 ? "" : "s"}`} />
        <Stat label="Premium requests" value={decimal.format(t.premium_requests)} note={t.calls ? `${decimal.format(t.premium_requests / t.calls)} per call` : undefined} />
        <Stat
          label="Tokens"
          value={formatTokens(tokens)}
          title={`${integer.format(tokens)} tokens`}
          note={`${formatTokens(t.input_tokens)} in · ${formatTokens(t.output_tokens)} out`}
        />
        <Stat label="Succeeded" value={percent(t.ok, t.calls)} note={`${integer.format(t.failed)} failed · ${integer.format(t.canceled)} canceled`} />
        <Stat label="Copilot time" value={formatDuration(t.duration)} note={`${formatDuration(t.duration / t.calls)} per call`} />
      </div>
      {t.reported < t.calls && (
        <p className="hint copilot-note">
          {integer.format(t.calls - t.reported)} of {integer.format(t.calls)} calls reported no usage (a Copilot CLI without{" "}
          <code>--usage-output-file</code>, or a call that failed early): premium requests and tokens leave them out.
        </p>
      )}
      <DailyChart usage={usage} />
      <div className="copilot-grid">
        <section>
          <h2>By model</h2>
          <ModelTable usage={usage} />
        </section>
        <section>
          <h2>By rung</h2>
          <BucketTable
            label="Rung"
            rows={usage.by_rung.map((r) => ({ key: r.rung, name: RUNG_LABELS[r.rung] ?? r.rung, bucket: r }))}
          />
        </section>
        {usage.workspace_id === null && usage.by_workspace.length > 1 && (
          <section>
            <h2>By workspace</h2>
            <BucketTable
              label="Workspace"
              rows={usage.by_workspace.map((w) => ({
                key: String(w.workspace_id),
                name: <Link to={`/workspaces/${w.workspace_id}`}>{w.name}</Link>,
                bucket: w,
              }))}
            />
          </section>
        )}
      </div>
      <section className="copilot-recent">
        <h2>Recent calls</h2>
        <RecentCalls usage={usage} />
      </section>
    </>
  );
}

function Stat({ label, value, note, title }: { label: string; value: string; note?: string; title?: string }) {
  return (
    <div className="stat-tile">
      <div className="stat-label">{label}</div>
      <div className="stat-value" title={title}>
        {value}
      </div>
      {note && <div className="stat-note">{note}</div>}
    </div>
  );
}

type Metric = "calls" | "premium" | "tokens";

const METRICS: { id: Metric; label: string; format: (n: number) => string }[] = [
  { id: "calls", label: "Calls", format: (n) => integer.format(n) },
  { id: "premium", label: "Premium requests", format: (n) => decimal.format(n) },
  { id: "tokens", label: "Tokens", format: formatTokens },
];

function DailyChart({ usage }: { usage: CopilotUsage }) {
  const [metric, setMetric] = useState<Metric>("calls");
  const [hover, setHover] = useState<number | null>(null);
  const series = dailySeries(usage);
  const { format, label } = METRICS.find((m) => m.id === metric)!;
  const max = Math.max(...series.map((d) => d[metric]), 0);
  const scale = max || 1;
  const width = 720;
  const height = 160;
  const slot = width / series.length;
  const bar = Math.max(1, Math.min(24, slot - 2));
  const shown = hover !== null ? series[hover] : null;

  return (
    <section className="copilot-chart">
      <div className="copilot-chart-header">
        <h2>{label} per day</h2>
        <div className="copilot-periods" role="group" aria-label="Chart metric">
          {METRICS.map((m) => (
            <button key={m.id} type="button" aria-pressed={metric === m.id} onClick={() => setMetric(m.id)}>
              {m.label}
            </button>
          ))}
        </div>
      </div>
      <div className="copilot-chart-body">
        <div className="copilot-chart-max">{format(max)}</div>
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={`${label} per day`} onMouseLeave={() => setHover(null)}>
          <line x1={0} x2={width} y1={height - 0.5} y2={height - 0.5} className="copilot-axis" />
          <line x1={0} x2={width} y1={0.5} y2={0.5} className="copilot-grid-line" />
          {series.map((d, i) => {
            const h = d[metric] ? Math.max(2, (d[metric] / scale) * (height - 4)) : 0;
            const x = i * slot + (slot - bar) / 2;
            return (
              <g key={d.day} onMouseEnter={() => setHover(i)}>
                <rect x={i * slot} y={0} width={slot} height={height} className="copilot-hit" />
                {h > 0 && <rect x={x} y={height - h} width={bar} height={h} rx={Math.min(2, bar / 2)} className={hover === i ? "copilot-bar active" : "copilot-bar"} />}
              </g>
            );
          })}
        </svg>
        {shown && hover !== null && (
          <div className="copilot-tooltip" style={{ left: `${((hover + 0.5) / series.length) * 100}%` }} role="tooltip">
            <strong>{shown.day}</strong>
            <div>{integer.format(shown.calls)} calls</div>
            <div>{decimal.format(shown.premium)} premium requests</div>
            <div>{formatTokens(shown.tokens)} tokens</div>
          </div>
        )}
      </div>
      <div className="copilot-chart-axis-labels">
        <span>{series[0]?.day}</span>
        <span>{series[series.length - 1]?.day}</span>
      </div>
    </section>
  );
}

function ModelTable({ usage }: { usage: CopilotUsage }) {
  if (!usage.by_model.length) return <p className="hint">No call reported which model answered it.</p>;
  const total = usage.by_model.reduce((sum, m) => sum + m.input_tokens + m.output_tokens, 0) || 1;
  return (
    <table className="table copilot-table">
      <thead>
        <tr>
          <th>Model</th>
          <th className="num">Calls</th>
          <th className="num" title="Model API requests: one per tool round">
            Requests
          </th>
          <th className="num">Input tokens</th>
          <th className="num">Output tokens</th>
          <th>Share of tokens</th>
        </tr>
      </thead>
      <tbody>
        {usage.by_model.map((m) => {
          const share = (m.input_tokens + m.output_tokens) / total;
          return (
            <tr key={m.model}>
              <td className="mono">{m.model}</td>
              <td className="num">{integer.format(m.calls)}</td>
              <td className="num">{integer.format(m.requests)}</td>
              <td className="num" title={integer.format(m.input_tokens)}>
                {formatTokens(m.input_tokens)}
              </td>
              <td className="num" title={integer.format(m.output_tokens)}>
                {formatTokens(m.output_tokens)}
              </td>
              <td>
                <Meter share={share} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Meter({ share }: { share: number }) {
  return (
    <span className="copilot-meter" title={`${Math.round(share * 100)}%`}>
      <span className="copilot-meter-track">
        <span className="copilot-meter-fill" style={{ width: `${Math.max(share * 100, share > 0 ? 2 : 0)}%` }} />
      </span>
      <span className="copilot-meter-label">{Math.round(share * 100)}%</span>
    </span>
  );
}

function BucketTable({ label, rows }: { label: string; rows: { key: string; name: ReactNode; bucket: CopilotUsageBucket }[] }) {
  return (
    <table className="table copilot-table">
      <thead>
        <tr>
          <th>{label}</th>
          <th className="num">Calls</th>
          <th className="num">Succeeded</th>
          <th className="num">Premium</th>
          <th className="num">Tokens</th>
          <th className="num">Time</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(({ key, name, bucket: b }) => (
          <tr key={key}>
            <td>{name}</td>
            <td className="num">{integer.format(b.calls)}</td>
            <td className="num">{percent(b.ok, b.calls)}</td>
            <td className="num">{decimal.format(b.premium_requests)}</td>
            <td className="num" title={integer.format(b.input_tokens + b.output_tokens)}>
              {formatTokens(b.input_tokens + b.output_tokens)}
            </td>
            <td className="num">{formatDuration(b.duration)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function statusTag(status: string | null) {
  if (status === "ok") return <span className="tag tag-ok">ok</span>;
  if (status === "canceled") return <span className="tag tag-warn">canceled</span>;
  return <span className="tag tag-error">{status ?? "unknown"}</span>;
}

function RecentCalls({ usage }: { usage: CopilotUsage }) {
  return (
    <table className="table copilot-table">
      <thead>
        <tr>
          <th>Started</th>
          <th>Items</th>
          {usage.workspace_id === null && <th>Workspace</th>}
          <th>Project</th>
          <th>Rung</th>
          <th>Model</th>
          <th>Status</th>
          <th className="num">Premium</th>
          <th className="num">Tokens in / out</th>
          <th className="num">Time</th>
        </tr>
      </thead>
      <tbody>
        {usage.recent.map((c, i) => (
          <tr key={`${c.attempt_id}-${i}`}>
            <td className="nowrap">{new Date(c.at).toLocaleString()}</td>
            <td className="mono nowrap">
              {c.keys.map((key, j) => (
                <span key={key}>
                  {j > 0 && ", "}
                  <Link to={`/workspaces/${c.workspace_id}/items/${encodeURIComponent(key)}`}>{key}</Link>
                </span>
              ))}
            </td>
            {usage.workspace_id === null && <td>{c.workspace}</td>}
            <td className="mono">{c.project ?? "–"}</td>
            <td>
              {RUNG_LABELS[c.rung ?? ""] ?? c.rung ?? "–"}
              {c.round > 0 && <span className="hint"> · fix {c.round}</span>}
              {c.build_round > 0 && <span className="hint"> · build {c.build_round}</span>}
            </td>
            <td className="mono">{c.model ?? (c.served_models.length ? `${c.served_models.join(", ")} (default)` : "default")}</td>
            <td>{statusTag(c.status)}</td>
            <td className="num">{c.premium_requests === null ? "–" : decimal.format(c.premium_requests)}</td>
            <td className="num">{c.input_tokens === null ? "–" : `${formatTokens(c.input_tokens)} / ${formatTokens(c.output_tokens ?? 0)}`}</td>
            <td className="num">{formatDuration(c.duration)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
