// Read-only front end over the migration ledger. No build step: Preact + htm, vendored.
// Live mode reads /api/...; a static export reads the same answers from data/*.json.
import { html, render, useEffect, useState } from "./htm-preact.js";

const STATIC = document.body.dataset.mode === "static";

async function load(path) {
  const url = STATIC ? `data/${path}.json` : `api/${path}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  return res.json();
}

function useData(path) {
  const [state, setState] = useState({ data: null, error: null });
  useEffect(() => {
    let live = true;
    setState({ data: null, error: null });
    load(path).then(
      (data) => live && setState({ data, error: null }),
      (error) => live && setState({ data: null, error }),
    );
    return () => { live = false; };
  }, [path]);
  return state;
}

function useRoute() {
  const parse = () => location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const [parts, setParts] = useState(parse());
  useEffect(() => {
    const onHash = () => { setParts(parse()); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return parts;
}

// ---------- small building blocks ----------

const stateColor = (s) => `var(--s-${s}, var(--muted))`;
const forecastColor = (f) => `var(--f-${f}, var(--muted))`;
const label = (s) => (s || "").replaceAll("_", " ");

const State = ({ s }) => html`<span class="state" style=${{ background: stateColor(s) }}>${label(s)}</span>`;
const Forecast = ({ f }) =>
  f ? html`<span class="forecast" style=${{ background: forecastColor(f) }}>${f}</span>` : html`<span class="muted">–</span>`;

function Loading({ state, children }) {
  if (state.error) return html`<p class="error">Could not load: ${state.error.message}</p>`;
  if (!state.data) return html`<p class="loading muted">Loading…</p>`;
  return children(state.data);
}

function StackedBar({ counts, order, color }) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
  const keys = order.filter((k) => counts[k]);
  return html`
    <div class="bar" role="img" aria-label=${keys.map((k) => `${label(k)} ${counts[k]}`).join(", ")}>
      ${keys.map((k) => html`<span style=${{ width: `${(100 * counts[k]) / total}%`, background: color(k) }} title=${`${label(k)}: ${counts[k]}`}></span>`)}
    </div>
    <div class="legend">
      ${keys.map((k) => html`<span><i style=${{ background: color(k) }}></i>${label(k)} <b>${counts[k]}</b></span>`)}
    </div>`;
}

function Diff({ text }) {
  if (!text) return html`<p class="muted">No diff.</p>`;
  const lines = text.split("\n");
  return html`<pre class="diff">${lines.map((l) => {
    let cls = "";
    if (l.startsWith("diff --git")) cls = "file";
    else if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("index ")) cls = "hunk";
    else if (l.startsWith("@@")) cls = "hunk";
    else if (l.startsWith("+")) cls = "add";
    else if (l.startsWith("-")) cls = "del";
    return html`<div class=${cls}>${l}</div>`;
  })}</pre>`;
}

function KeyValues({ obj }) {
  const entries = Object.entries(obj || {}).filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (!entries.length) return html`<p class="muted">None.</p>`;
  const show = (v) =>
    Array.isArray(v) ? (v.length ? v.join(", ") : "–")
      : typeof v === "object" ? html`<code>${JSON.stringify(v)}</code>`
        : String(v);
  return html`<dl class="kv">${entries.map(([k, v]) => html`<dt>${label(k)}</dt><dd>${show(v)}</dd>`)}</dl>`;
}

function Timeline({ items }) {
  if (!items?.length) return html`<p class="muted">No transitions yet.</p>`;
  return html`<ul class="timeline">${items.map((t) => html`
    <li><${State} s=${t.from} /> → <${State} s=${t.to} />
      <span class="muted"> ${t.actor}${t.forced ? " (forced)" : ""} · ${fmtTime(t.at)}</span>
      ${t.reason ? html`<div>${t.reason}</div>` : null}</li>`)}</ul>`;
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");
const short = (sha) => (sha ? sha.slice(0, 12) : "–");

function Migration({ m }) {
  const final = m.agent?.final;
  const attempts = m.agent?.attempts || [];
  return html`
    <div class="panel">
      <h2>Migration <span class="muted">· ${label(m.strategy || "not finished")}${m.finished_at ? ` · ${fmtTime(m.finished_at)}` : ""}</span></h2>
      ${m.reason ? html`<p>${m.reason}</p>` : null}
      ${m.question ? html`<p><b>Question:</b> ${m.question}</p>` : null}
      <div class="table-wrap"><table>
        <thead><tr><th>Project</th><th>Branch</th><th>How</th><th>Base → head</th><th>Notes</th></tr></thead>
        <tbody>${m.projects.map((p) => html`<tr>
          <td>${p.project}</td><td><code>${p.branch}</code></td><td>${label(p.strategy) || "–"}</td>
          <td class="mono">${short(p.base)} → ${short(p.head)}</td>
          <td class="wrap">${[p.cherry_pick, p.net_diff].filter((r) => r && !r.ok).map((r) => html`<div>${label(r.strategy)} failed${r.conflicts.length ? html`: conflicts in ${r.conflicts.map((f) => html`<code>${f}</code> `)}` : r.error ? `: ${r.error}` : ""}</div>`)}</td>
        </tr>`)}</tbody>
      </table></div>
    </div>
    ${final || m.review ? html`<div class="grid">
      ${final ? html`<div class="panel">
        <h2>Cluster agent <span class="muted">· ${attempts.length} attempt(s)</span></h2>
        <p>${label(final.outcome)} · confidence ${final.confidence.toFixed(2)}</p>
        <p>${final.summary}</p>
        ${final.risks?.length ? html`<p class="muted">Risks to check:</p><ul class="plain">${final.risks.map((r) => html`<li>${r}</li>`)}</ul>` : null}
        ${attempts.filter((a) => a.guardrails && !a.guardrails.ok).map((a, i) => html`<p class="muted">Attempt ${i + 1} rejected by guardrails:</p><ul class="plain">${a.guardrails.hard.map((h) => html`<li>${h}</li>`)}</ul>`)}
        ${m.guardrails?.soft?.length ? html`<p class="muted">Noted by guardrails:</p><ul class="plain">${m.guardrails.soft.map((h) => html`<li>${h}</li>`)}</ul>` : null}
      </div>` : null}
      ${m.review ? html`<div class="panel">
        <h2>Reviewer</h2>
        ${m.review.error ? html`<p class="error">${m.review.error}</p>` : html`
          <p>${label(m.review.verdict)} · confidence ${m.review.confidence.toFixed(2)}${m.review.intent_met ? "" : " · intent not met"}</p>
          <p>${m.review.summary}</p>
          ${m.review.issues.length ? html`<ul class="plain">${m.review.issues.map((r) => html`<li>${r}</li>`)}</ul>` : null}`}
      </div>` : null}
    </div><div class="stack"></div>` : null}
    ${m.diff ? html`<div class="panel"><h2>Migrated change <span class="muted">(on the cluster branches in the target)</span></h2><${Diff} text=${m.diff} /></div>` : null}`;
}

function Run({ title, r }) {
  return html`<details open=${!r.ok}>
    <summary>${title}: <b class=${r.ok ? "" : "error"}>${r.ok ? "ok" : r.timed_out ? "timed out" : `failed (exit ${r.exit_code})`}</b>
      <span class="muted"> · ${r.duration_s}s · <code>${r.command}</code></span></summary>
    <pre class="block">${r.log_tail}</pre></details>`;
}

function Verification({ v }) {
  const sel = v.selection || {};
  return html`
    <div class="panel">
      <h2>Build and test <span class="muted">· ${v.host} · ${v.slot}${v.finished_at ? ` · ${fmtTime(v.finished_at)}` : ""}</span></h2>
      ${v.reason ? html`<p>${v.reason}</p>` : null}
      <dl class="kv">
        <dt>Modules</dt><dd>${sel.modules?.length ? sel.modules.map((m) => html`<code>${m}</code> `) : "–"}</dd>
        <dt>Tests</dt><dd>${sel.tests?.length ? sel.tests.map((t) => html`<code>${t}</code> `) : "–"}</dd>
        ${sel.notes?.length ? html`<dt>Notes</dt><dd>${sel.notes.join("; ")}</dd>` : null}
      </dl>
      ${v.builds.map((b, i) => html`<${Run} title=${i ? `Build after fix ${i}` : "Build"} r=${b} />`)}
      ${v.fixes.map((f) => html`<div><b>Fix ${f.attempt}:</b> ${f.answer ? html`${label(f.answer.outcome)} · confidence ${f.answer.confidence.toFixed(2)} · ${f.answer.summary}` : html`<span class="error">${f.error}</span>`}
        ${f.base_build ? html`<${Run} title="Build of the unpatched target" r=${f.base_build} />` : null}</div>`)}
      ${v.tests ? html`<${Run} title="Tests" r=${v.tests} />` : null}
      ${v.tests?.baseline ? html`<${Run} title="Tests on the unpatched target" r=${v.tests.baseline} />` : null}
    </div>`;
}

// ---------- pages ----------

function Dashboard({ run }) {
  const summary = useData(`runs/${run}/summary`);
  const clusters = useData(`runs/${run}/clusters`);
  const activity = useData(`runs/${run}/activity`);
  return html`<${Loading} state=${summary}>${(s) => {
    const dropped = ["upstream", "obsolete", "reverted"].reduce((a, k) => a + (s.states[k] || 0), 0);
    const toMigrate = (s.states.needed || 0);
    const migrated = ["applied", "resolved", "rewritten", "verified"].reduce((a, k) => a + (s.states[k] || 0), 0);
    return html`
      <h1>${s.project} <span class="muted">· Android ${s.migration?.source_version} → ${s.migration?.target_version}, run ${s.run.id}</span></h1>
      <div class="tiles">
        <div class="tile"><div class="value">${s.patches}</div><div class="label">patches</div></div>
        <div class="tile"><div class="value">${toMigrate}</div><div class="label">still needed</div></div>
        <div class="tile"><div class="value">${migrated}</div><div class="label">migrated</div></div>
        <div class="tile"><div class="value">${dropped}</div><div class="label">dropped (upstream, obsolete, reverted)</div></div>
        <div class="tile"><div class="value">${s.states.needs_decision || 0}</div><div class="label">need a decision</div></div>
        <div class="tile"><div class="value">${s.clusters}</div><div class="label">clusters</div></div>
        <div class="tile"><div class="value">${s.credits}</div><div class="label">credits (${s.agent_calls} agent calls)</div></div>
      </div>
      <div class="grid">
        <div class="panel">
          <h2>Patches by state</h2>
          <${StackedBar} counts=${s.states} order=${s.state_order} color=${stateColor} />
        </div>
        <div class="panel">
          <h2>Clusters to migrate, by forecast</h2>
          ${Object.keys(s.forecasts).length
            ? html`<${StackedBar} counts=${s.forecasts} order=${["clean", "conflict", "rewrite", "obsolete"]} color=${forecastColor} />`
            : html`<p class="muted">No analysis yet. Run <code>migrate analyse</code>.</p>`}
        </div>
        <div class="panel">
          <h2>Decided by</h2>
          <${KeyValues} obj=${Object.fromEntries(Object.entries(s.rungs).map(([k, v]) => [k === "none" ? "no rung concluded" : k, v]))} />
        </div>
        <div class="panel">
          <h2>Patches per project</h2>
          <${KeyValues} obj=${s.projects} />
        </div>
      </div>
      <div class="stack"></div>
      <div class="panel">
        <h2>Migration order</h2>
        <${Loading} state=${clusters}>${(cs) => cs.length
          ? html`<${ClusterTable} run=${run} clusters=${cs} limit=${10} />`
          : html`<p class="muted">No clusters yet.</p>`}<//>
      </div>
      <div class="panel">
        <h2>Recent activity</h2>
        <${Loading} state=${activity}>${(items) => items.length ? html`<ul class="timeline">${items.slice(0, 15).map((t) => html`
          <li><a href=${`#/r/${run}/${t.entity === "patch" ? "patches" : "clusters"}/${t.id}`}>${t.entity} ${t.id}</a>
            ${" "}<${State} s=${t.to} /> <span class="muted">${t.actor} · ${t.reason}</span></li>`)}</ul>`
          : html`<p class="muted">Nothing yet.</p>`}<//>
      </div>`;
  }}<//>`;
}

function PatchesPage({ run }) {
  const state = useData(`runs/${run}/patches`);
  const [text, setText] = useState("");
  const [st, setSt] = useState("");
  const [project, setProject] = useState("");
  return html`<${Loading} state=${state}>${(rows) => {
    const states = [...new Set(rows.map((r) => r.state))];
    const projects = [...new Set(rows.map((r) => r.project))].sort();
    const q = text.toLowerCase();
    const shown = rows.filter((r) =>
      (!st || r.state === st) && (!project || r.project === project) &&
      (!q || r.subject.toLowerCase().includes(q) || (r.commit || "").startsWith(q) ||
        r.jira_keys.some((k) => k.toLowerCase().includes(q))));
    return html`
      <h1>Patches <span class="muted">(${shown.length} of ${rows.length})</span></h1>
      <div class="filters">
        <input placeholder="Filter by subject, commit or Jira key" value=${text} onInput=${(e) => setText(e.target.value)} />
        <select value=${st} onChange=${(e) => setSt(e.target.value)}>
          <option value="">All states</option>${states.map((s) => html`<option value=${s}>${label(s)}</option>`)}
        </select>
        <select value=${project} onChange=${(e) => setProject(e.target.value)}>
          <option value="">All projects</option>${projects.map((p) => html`<option value=${p}>${p}</option>`)}
        </select>
      </div>
      <div class="panel table-wrap"><table>
        <thead><tr><th class="num">#</th><th>Subject</th><th>Project</th><th>State</th><th>Decided by</th><th>Role</th><th>Jira</th><th>Cluster</th></tr></thead>
        <tbody>${shown.map((p) => html`<tr>
          <td class="num">${p.seq + 1}</td>
          <td><a href=${`#/r/${run}/patches/${p.id}`}>${p.subject}</a><div class="muted mono">${short(p.commit)}</div></td>
          <td>${p.project}</td>
          <td><${State} s=${p.state} /></td>
          <td>${p.rung || html`<span class="muted">–</span>`}</td>
          <td>${p.role}</td>
          <td>${p.jira_keys.join(", ")}</td>
          <td>${p.cluster_id ? html`<a href=${`#/r/${run}/clusters/${p.cluster_id}`}>${p.cluster_id}</a>` : "–"}</td>
        </tr>`)}</tbody>
      </table></div>`;
  }}<//>`;
}

function Evidence({ verdict }) {
  if (!verdict || !verdict.verdict) return html`<p class="muted">Not analysed yet.</p>`;
  return html`
    <dl class="kv">
      <dt>verdict</dt><dd>${label(verdict.verdict)}</dd>
      <dt>decided by</dt><dd>${verdict.rung || "no deterministic rung concluded"}</dd>
      ${verdict.question ? html`<dt>question</dt><dd>${verdict.question}</dd>` : null}
    </dl>
    ${verdict.evidence ? html`<h2 style="margin-top:12px">Evidence</h2><${KeyValues} obj=${verdict.evidence} />` : null}
    ${verdict.checks?.length ? html`<h2 style="margin-top:12px">Checks</h2>
      <ul class="plain">${verdict.checks.map((c) => {
        const { rung, conclusive, ...rest } = c;
        const detail = Object.entries(rest).map(([k, v]) => `${label(k)}: ${Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : v}`).join("; ");
        return html`<li><b>${rung}</b> ${conclusive ? "concluded" : "did not conclude"}${detail ? html`<span class="muted"> · ${detail}</span>` : null}</li>`;
      })}</ul>` : null}
    ${verdict.notes?.length ? html`<h2 style="margin-top:12px">Notes</h2><ul class="plain">${verdict.notes.map((n) => html`<li>${n}</li>`)}</ul>` : null}`;
}

function PatchPage({ run, id }) {
  const state = useData(`runs/${run}/patches/${id}`);
  return html`<${Loading} state=${state}>${(p) => html`
    <p><a href=${`#/r/${run}/patches`}>← Patches</a></p>
    <h1>${p.subject} <${State} s=${p.state} /></h1>
    <div class="grid">
      <div class="panel">
        <h2>Patch</h2>
        <dl class="kv">
          <dt>project</dt><dd>${p.project}</dd>
          <dt>commit</dt><dd class="mono">${p.commit || p.diff_path || "–"}</dd>
          <dt>author</dt><dd>${p.author || "–"} <span class="muted">${fmtTime(p.authored_at)}</span></dd>
          <dt>jira</dt><dd>${p.jira_keys.join(", ") || "–"}</dd>
          <dt>role</dt><dd>${p.role}</dd>
          <dt>cluster</dt><dd>${p.cluster_id ? html`<a href=${`#/r/${run}/clusters/${p.cluster_id}`}>cluster ${p.cluster_id}</a>` : "–"}</dd>
          <dt>change-id</dt><dd class="mono">${p.change_id || "–"}</dd>
          <dt>sources</dt><dd>${p.sources.join(", ")}</dd>
          <dt>files</dt><dd>${(p.footprint.files || []).map((f) => `${f.status} ${f.path}`).join(", ")}</dd>
        </dl>
        ${Object.keys(p.links || {}).length ? html`<h2 style="margin-top:12px">Links</h2><${KeyValues} obj=${p.links} />` : null}
      </div>
      <div class="panel"><h2>Analysis</h2><${Evidence} verdict=${p.verdict} />
        ${p.confidence !== null ? html`<p class="muted">Confidence ${p.confidence}</p>` : null}</div>
    </div>
    <div class="stack"></div>
    <div class="grid">
      <div class="panel"><h2>Intent</h2>${p.intent ? html`<pre class="block">${p.intent}</pre>` : html`<p class="muted">No intent yet (<code>migrate intents</code>).</p>`}</div>
      <div class="panel"><h2>History</h2><${Timeline} items=${p.history} /></div>
    </div>
    <div class="stack"></div>
    <div class="panel"><h2>Commit message</h2><pre class="block">${p.message}</pre></div>
    <div class="panel"><h2>Diff</h2><${Diff} text=${p.diff} /></div>`}<//>`;
}

function ClusterTable({ run, clusters, limit }) {
  const shown = limit ? clusters.slice(0, limit) : clusters;
  return html`<div class="table-wrap"><table>
    <thead><tr><th class="num">Order</th><th>Cluster</th><th>State</th><th>Forecast</th><th class="num">Patches</th><th>Projects</th><th>Depends on</th></tr></thead>
    <tbody>${shown.map((c) => html`<tr>
      <td class="num">${c.order + 1}</td>
      <td><a href=${`#/r/${run}/clusters/${c.id}`}>${c.name}</a>${c.fold_errors ? html`<div class="muted">${c.fold_errors} fold warning(s)</div>` : null}</td>
      <td><${State} s=${c.state} /></td>
      <td><${Forecast} f=${c.forecast} /></td>
      <td class="num">${c.members.length}</td>
      <td>${c.projects.join(", ")}</td>
      <td>${c.depends_on.length ? c.depends_on.map((d, i) => html`${i ? ", " : ""}<a href=${`#/r/${run}/clusters/${d}`}>${d}</a>`) : "–"}</td>
    </tr>`)}</tbody>
  </table>${limit && clusters.length > limit ? html`<p><a href=${`#/r/${run}/clusters`}>All ${clusters.length} clusters →</a></p>` : null}</div>`;
}

function ClustersPage({ run }) {
  const state = useData(`runs/${run}/clusters`);
  const [st, setSt] = useState("");
  return html`<${Loading} state=${state}>${(cs) => {
    const states = [...new Set(cs.map((c) => c.state))];
    const shown = cs.filter((c) => !st || c.state === st);
    const deps = cs.filter((c) => c.depends_on.length);
    return html`
      <h1>Clusters <span class="muted">(${shown.length} of ${cs.length}, in migration order)</span></h1>
      <div class="filters">
        <select value=${st} onChange=${(e) => setSt(e.target.value)}>
          <option value="">All states</option>${states.map((s) => html`<option value=${s}>${label(s)}</option>`)}
        </select>
      </div>
      <div class="panel"><${ClusterTable} run=${run} clusters=${shown} /></div>
      ${deps.length ? html`<div class="panel"><h2>Dependencies between clusters</h2>
        <${Graph} nodes=${cs.filter((c) => c.depends_on.length || deps.some((d) => d.depends_on.includes(c.id))).map((c) => ({ id: c.id, label: `${c.order + 1}. ${c.name}`, color: stateColor(c.state), href: `#/r/${run}/clusters/${c.id}` }))}
          edges=${deps.flatMap((c) => c.depends_on.map((d) => ({ from: d, to: c.id, kind: "depends" })))} /></div>` : null}`;
  }}<//>`;
}

const EDGE_COLORS = { revert: "var(--s-reverted)", fixes: "var(--s-failed)", jira: "var(--accent)", symbol: "var(--s-needs_decision)", jira_link: "var(--muted)", overlap: "var(--s-applied)", depends: "var(--muted)" };

// Nodes in a column per position; edges drawn as arcs above the nodes, labelled by kind.
function Graph({ nodes, edges }) {
  if (!nodes.length) return null;
  const w = 190, gap = 40, h = 46;
  const top = edges.length ? 26 + 0.75 * (18 + 22 * (Math.min(edges.length, 6) - 1)) : 10;
  const x = {};
  nodes.forEach((n, i) => { x[n.id] = 10 + i * (w + gap); });
  const width = 20 + nodes.length * (w + gap) - gap;
  const height = top + h + 16;
  const trim = (s) => (s.length > 26 ? `${s.slice(0, 25)}…` : s);
  return html`<div class="table-wrap"><svg class="graph" width=${width} height=${height} viewBox=${`0 0 ${width} ${height}`} role="img" aria-label="graph">
    ${edges.map((e, i) => {
      if (x[e.from] === undefined || x[e.to] === undefined) return null;
      const x1 = x[e.from] + w / 2, x2 = x[e.to] + w / 2;
      const lift = 18 + 22 * (i % 6);
      const color = EDGE_COLORS[e.kind] || "var(--muted)";
      return html`<g>
        <path class="edge" stroke=${color} d=${`M ${x1} ${top} C ${x1} ${top - lift}, ${x2} ${top - lift}, ${x2} ${top}`} />
        <text class="edge-label" x=${(x1 + x2) / 2} y=${top - lift * 0.75 - 2} text-anchor="middle">${e.kind}${e.detail ? `: ${trim(e.detail)}` : ""}</text>
      </g>`;
    })}
    ${nodes.map((n) => html`<a href=${n.href}><g class="node">
      <rect x=${x[n.id]} y=${top} width=${w} height=${h} rx="6" stroke=${n.color} />
      <text x=${x[n.id] + 10} y=${top + 19}>${trim(n.label)}</text>
      ${n.sub ? html`<text x=${x[n.id] + 10} y=${top + 36} class="edge-label">${trim(n.sub)}</text>` : null}
    </g></a>`)}
  </svg></div>`;
}

function ClusterPage({ run, id }) {
  const state = useData(`runs/${run}/clusters/${id}`);
  return html`<${Loading} state=${state}>${(c) => html`
    <p><a href=${`#/r/${run}/clusters`}>← Clusters</a></p>
    <h1>${c.order + 1}. ${c.name} <${State} s=${c.state} /> <${Forecast} f=${c.forecast?.level} /></h1>
    <div class="panel">
      <h2>Patches and how they are linked</h2>
      <${Graph} nodes=${c.members.map((m) => ({ id: m.id, label: m.subject, sub: `${m.project} · ${label(m.state)}`, color: stateColor(m.state), href: `#/r/${run}/patches/${m.id}` }))}
        edges=${c.edges.map((e) => ({ from: e.from, to: e.to, kind: e.kind, detail: e.detail }))} />
      <div class="table-wrap"><table>
        <thead><tr><th class="num">#</th><th>Patch</th><th>Project</th><th>Role</th><th>State</th><th>Decided by</th></tr></thead>
        <tbody>${c.members.map((m) => html`<tr>
          <td class="num">${m.seq + 1}</td><td><a href=${`#/r/${run}/patches/${m.id}`}>${m.subject}</a></td>
          <td>${m.project}</td><td>${m.role}</td><td><${State} s=${m.state} /></td><td>${m.rung || "–"}</td></tr>`)}</tbody>
      </table></div>
    </div>
    <div class="grid">
      <div class="panel">
        <h2>Forecast</h2>
        ${c.forecast ? html`<p><${Forecast} f=${c.forecast.level} /></p>
          ${c.forecast.reasons.length ? html`<ul class="plain">${c.forecast.reasons.map((r) => html`<li>${r}</li>`)}</ul>` : html`<p class="muted">Upstream left the changed lines alone.</p>`}`
          : html`<p class="muted">Nothing left to migrate in this cluster.</p>`}
        ${c.fold_errors.length ? html`<h2 style="margin-top:12px">Fold warnings</h2><ul class="plain">${c.fold_errors.map((e) => html`<li>${e}</li>`)}</ul>` : null}
        ${c.upstream_check ? html`<p class="muted">Net diff upstream check: ${c.upstream_check.matched} of ${c.upstream_check.total} lines already in the target.</p>` : null}
        ${c.depends_on.length ? html`<p>Depends on ${c.depends_on.map((d, i) => html`${i ? ", " : ""}<a href=${`#/r/${run}/clusters/${d}`}>cluster ${d}</a>`)}</p>` : null}
      </div>
      <div class="panel"><h2>History</h2><${Timeline} items=${c.history} /></div>
    </div>
    <div class="stack"></div>
    <${ReviewStatus} c=${c} />
    ${c.migration ? html`<${Migration} m=${c.migration} />` : null}
    ${c.verification ? html`<${Verification} v=${c.verification} />` : null}
    <div class="panel"><h2>Net diff <span class="muted">(the patches still needed, folded on the source base)</span></h2><${Diff} text=${c.net_diff} /></div>`}<//>`;
}

function KnowledgePage({ run }) {
  const state = useData(`runs/${run}/knowledge`);
  const [st, setSt] = useState("");
  return html`<${Loading} state=${state}>${(k) => {
    const shown = k.fates.filter((f) => !st || f.status === st);
    return html`
      <h1>Knowledge pack</h1>
      <div class="grid">${Object.entries(k.footprints).map(([project, fp]) => html`
        <div class="panel">
          <h2>${project}</h2>
          <p class="muted">${fp.patches} patches, ${Object.keys(fp.files).length} files</p>
          ${fp.hot_spots.length ? html`<p>Hot spots: ${fp.hot_spots.map((h) => html`<code>${h}</code> (${fp.files[h].length}) `)}</p>` : null}
          <details><summary>Files</summary><ul class="plain">${Object.entries(fp.files).map(([path, ids]) => html`<li><code>${path}</code> <span class="muted">${ids.length} patch(es)</span></li>`)}</ul></details>
        </div>`)}
      </div>
      <div class="stack"></div>
      <div class="panel">
        <h2>Upstream fate of touched files</h2>
        <div class="filters">
          <select value=${st} onChange=${(e) => setSt(e.target.value)}>
            <option value="">All (${k.fates.length})</option>
            ${Object.entries(k.fate_counts).map(([s, n]) => html`<option value=${s}>${label(s)} (${n})</option>`)}
          </select>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>File</th><th>Fate</th><th>Now at</th><th>Upstream commits</th><th>Changed signatures</th></tr></thead>
          <tbody>${shown.map((f) => html`<tr>
            <td class="wrap"><div class="muted">${f.project}</div><code>${f.path}</code></td>
            <td>${label(f.status)}${f.exact ? "" : html`<div class="muted">by content</div>`}</td>
            <td class="wrap">${f.new_path ? html`<code>${f.new_project ? `${f.new_project}:` : ""}${f.new_path}</code>` : f.candidates.length ? html`<span class="muted">candidates: ${f.candidates.join(", ")}</span>` : "–"}</td>
            <td class="commits">${f.commits.length ? html`<ul class="plain">${f.commits.slice(0, 5).map((c) => html`<li class="mono">${c}</li>`)}</ul>` : "–"}</td>
            <td class="wrap">${Object.keys(f.changed_signatures).length || f.removed_symbols.length ? html`<ul class="plain">
              ${Object.entries(f.changed_signatures).map(([n, s]) => html`<li><code>${s.before.join(" | ")}</code> → <code>${s.after.join(" | ")}</code></li>`)}
              ${f.removed_symbols.map((n) => html`<li>removed: <code>${n}</code></li>`)}</ul>` : "–"}</td>
          </tr>`)}</tbody>
        </table></div>
      </div>`;
  }}<//>`;
}

function AgentsPage({ run }) {
  const state = useData(`runs/${run}/agent-calls`);
  return html`<${Loading} state=${state}>${(calls) => html`
    <h1>Agent calls <span class="muted">(${calls.length}, ${calls.reduce((a, c) => a + c.credits, 0).toFixed(1)} credits)</span></h1>
    <div class="panel table-wrap">${calls.length ? html`<table>
      <thead><tr><th>#</th><th>Agent</th><th>Task</th><th class="num">Attempt</th><th>Model</th><th class="num">Seconds</th><th class="num">Credits</th><th>Result</th></tr></thead>
      <tbody>${calls.map((c) => html`<tr>
        <td>${c.id}</td><td>${c.agent}</td><td class="mono">${c.task}</td><td class="num">${c.attempt}</td>
        <td>${c.model || "default"}</td><td class="num">${c.duration_s}</td><td class="num">${c.credits}</td>
        <td>${c.ok ? "ok" : html`<span class="error" style="padding:0">${c.error || "failed"}</span>`}</td></tr>`)}</tbody>
    </table>` : html`<p class="muted">No agent calls in this run.</p>`}</div>`}<//>`;
}

function ConfigPage({ run }) {
  const state = useData(`runs/${run}/config`);
  return html`<${Loading} state=${state}>${(c) => html`
    <h1>Configuration <span class="muted">used by run ${run}</span></h1>
    <p class="muted">Read-only snapshot (hash ${c.hash.slice(0, 12)}). Secrets appear only as references. Edit the project's YAML file to change it.</p>
    <div class="panel"><pre class="block">${JSON.stringify(c.config, null, 2)}</pre></div>`}<//>`;
}

const QUEUE_INFO = {
  quick_approve: ["Quick approve", "Verified, high confidence"],
  careful_review: ["Careful review", "Verified, lower confidence: read the agent notes"],
  needs_decision: ["Needs a decision", "A human has to choose"],
  failed: ["Failed", "Could not be migrated or verified"],
  dropped: ["Dropped", "Upstream, obsolete or reverted: confirm"],
  in_progress: ["In progress", "Still to migrate or verify"],
  approved: ["Approved", "Ready for the Jira export"],
};

function ReviewPage({ run }) {
  const state = useData(`runs/${run}/review`);
  return html`<${Loading} state=${state}>${(r) => html`
    <h1>Review</h1>
    <p class="muted">Approve with <code>migrate review approve ID --by NAME</code> or by approving or merging the PR; request changes on the PR or use <code>migrate review reject</code> to send a cluster back to the agent. <code>migrate review sync</code> reads the PRs.</p>
    <div class="tiles">${Object.entries(QUEUE_INFO).map(([q, [name]]) => html`
      <div class="tile"><div class="value">${r.counts[q] || 0}</div><div class="label">${name.toLowerCase()}</div></div>`)}</div>
    ${Object.entries(QUEUE_INFO).filter(([q]) => r.queues[q]?.length).map(([q, [name, hint]]) => html`
      <div class="panel">
        <h2>${name} <span class="muted">· ${hint}</span></h2>
        <div class="table-wrap"><table>
          <thead><tr><th>Cluster</th><th>State</th><th class="num">Confidence</th><th>PRs</th><th>Notes</th></tr></thead>
          <tbody>${r.queues[q].map((c) => html`<tr>
            <td class="wrap"><a href=${`#/r/${run}/clusters/${c.id}`}>${c.name}</a></td>
            <td><${State} s=${c.state} /></td>
            <td class="num">${c.confidence != null ? c.confidence.toFixed(2) : "–"}</td>
            <td>${c.prs.length ? c.prs.map((u) => html`<div><a href=${u} target="_blank" rel="noopener">${u.split("/").slice(-3).join("/")}</a></div>`) : "–"}</td>
            <td class="wrap">${c.question ? html`<b>Question:</b> ${c.question}` : c.review ? html`${c.review.decision} by ${c.review.by}${c.jira.length ? ` · Jira: ${c.jira.join(", ")}` : ""}` : c.reason || ""}</td>
          </tr>`)}</tbody>
        </table></div>
      </div>`)}`}<//>`;
}

function ReviewStatus({ c }) {
  const prs = c.publish?.prs || [];
  if (!prs.length && !c.review && !c.feedback.length && !c.decisions.length) return null;
  return html`<div class="panel">
    <h2>Review <span class="muted">· ${(QUEUE_INFO[c.queue] || [c.queue])[0]}</span></h2>
    ${c.review ? html`<p>${label(c.review.decision)} by ${c.review.by} (${c.review.source}) · ${fmtTime(c.review.at)}${c.review.reason ? html`<br />${c.review.reason}` : null}</p>` : null}
    ${prs.length ? html`<p>${prs.map((pr) => html`<div>${pr.project}: <a href=${pr.url} target="_blank" rel="noopener">${pr.repo} #${pr.number}</a> <span class="muted">(${pr.branch})</span></div>`)}</p>` : null}
    ${c.publish?.error ? html`<p class="error">Publishing failed: ${c.publish.error}</p>` : null}
    ${c.feedback.length ? html`<p class="muted">Feedback for the agent:</p><ul class="plain">${c.feedback.map((f) => html`<li>${f.by} (${f.source}): ${f.text}</li>`)}</ul>` : null}
    ${c.decisions.length ? html`<p class="muted">Decisions:</p><ul class="plain">${c.decisions.map((d) => html`<li>${d.by}: ${d.choice} · ${d.reason}</li>`)}</ul>` : null}
    ${c.jira_export ? html`<p class="muted">Exported to Jira: ${Object.keys(c.jira_export).join(", ")}</p>` : null}
  </div>`;
}

function RunsPage({ runs }) {
  return html`<h1>Runs</h1><div class="panel table-wrap"><table>
    <thead><tr><th>Run</th><th>Started</th><th>Note</th><th class="num">Patches</th><th class="num">Clusters</th><th>Config</th></tr></thead>
    <tbody>${runs.map((r) => html`<tr>
      <td><a href=${`#/r/${r.id}`}>Run ${r.id}</a></td><td>${fmtTime(r.created_at)}</td><td>${r.note || ""}</td>
      <td class="num">${r.patches}</td><td class="num">${r.clusters}</td><td class="mono">${r.config_hash}</td></tr>`)}</tbody>
  </table></div>`;
}

const TABS = [["", "Overview"], ["review", "Review"], ["patches", "Patches"], ["clusters", "Clusters"], ["knowledge", "Knowledge"], ["agents", "Agent calls"], ["config", "Config"]];

function App() {
  const runs = useData("runs");
  const parts = useRoute();
  return html`<${Loading} state=${runs}>${(list) => {
    if (!list.length) return html`<main><h1>No runs yet</h1><p>Start with <code>migrate ingest</code>, then <code>migrate analyse</code>.</p></main>`;
    const latest = list[0].id;
    const run = parts[0] === "r" ? Number(parts[1]) : latest;
    const page = parts[0] === "r" ? parts[2] || "" : parts[0] === "runs" ? "runs" : "";
    const id = parts[3];
    let body;
    if (page === "runs") body = html`<${RunsPage} runs=${list} />`;
    else if (page === "patches" && id) body = html`<${PatchPage} run=${run} id=${id} />`;
    else if (page === "patches") body = html`<${PatchesPage} run=${run} />`;
    else if (page === "clusters" && id) body = html`<${ClusterPage} run=${run} id=${id} />`;
    else if (page === "clusters") body = html`<${ClustersPage} run=${run} />`;
    else if (page === "review") body = html`<${ReviewPage} run=${run} />`;
    else if (page === "knowledge") body = html`<${KnowledgePage} run=${run} />`;
    else if (page === "agents") body = html`<${AgentsPage} run=${run} />`;
    else if (page === "config") body = html`<${ConfigPage} run=${run} />`;
    else body = html`<${Dashboard} run=${run} />`;
    return html`
      <header>
        <span class="title">Patch migration${STATIC ? html` <span class="badge-static">static snapshot</span>` : null}</span>
        <nav>${TABS.map(([p, name]) => html`<a class=${page === p ? "active" : ""} href=${`#/r/${run}${p ? `/${p}` : ""}`}>${name}</a>`)}
          <a class=${page === "runs" ? "active" : ""} href="#/runs">Runs</a></nav>
        <select aria-label="Run" value=${run} onChange=${(e) => { location.hash = `#/r/${e.target.value}${page && page !== "runs" ? `/${page}` : ""}`; }}>
          ${list.map((r) => html`<option value=${r.id}>Run ${r.id}${r.note ? ` · ${r.note}` : ""}</option>`)}
        </select>
      </header>
      <main key=${`${run}/${page}/${id}`}>${body}</main>`;
  }}<//>`;
}

render(html`<${App} />`, document.getElementById("app"));
