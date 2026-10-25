// Everything a person does from the browser besides settings: run steps as background jobs,
// review clusters, edit engineer notes, decide on memory lessons. Every write needs the admin
// token (the same one as for Settings) and a name, which is recorded with the change. A server
// started without --admin-token (data-token="off") asks only for the name.
import { html, useEffect, useRef, useState } from "./htm-preact.js";

export const STATIC = document.body.dataset.mode === "static";
export const NO_TOKEN = document.body.dataset.token === "off";
const TOKEN_KEY = "patchmig-admin-token";
const AUTHOR_KEY = "patchmig-author";

function stored(storage, key) {
  try { return storage.getItem(key) || ""; } catch { return ""; }
}
function keep(storage, key, value) {
  try { value ? storage.setItem(key, value) : storage.removeItem(key); } catch { /* private mode */ }
}

// ---------- who is signed in (shared by every page) ----------

const auth = { token: stored(sessionStorage, TOKEN_KEY), author: stored(localStorage, AUTHOR_KEY), listeners: new Set() };
function setAuth(token, author) {
  auth.token = token; auth.author = author;
  keep(sessionStorage, TOKEN_KEY, token); keep(localStorage, AUTHOR_KEY, author);
  auth.listeners.forEach((f) => f());
}
export function useAuth() {
  const [, tick] = useState(0);
  useEffect(() => {
    const f = () => tick((n) => n + 1);
    auth.listeners.add(f);
    // The settings page signs in with the same storage keys.
    const t = stored(sessionStorage, TOKEN_KEY), a = stored(localStorage, AUTHOR_KEY);
    if (t !== auth.token || a !== auth.author) setAuth(t, a);
    return () => auth.listeners.delete(f);
  }, []);
  return { token: auth.token, author: auth.author, ready: Boolean((NO_TOKEN || auth.token) && auth.author.trim()) };
}

export async function call(method, path, body) {
  const res = await fetch(`api/${path}`, {
    method,
    headers: { "Content-Type": "application/json", ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).catch((e) => ({ ok: false, status: 0, json: async () => ({ detail: e.message }) }));
  const data = await res.json().catch(() => ({}));
  if (res.ok) return data;
  const d = data.detail;
  const text = res.status === 401 ? "the admin token is wrong (sign in again)"
    : typeof d === "string" ? d : d?.problems ? d.problems.join("; ") : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `HTTP ${res.status}`;
  throw new Error(text);
}

// Whether this server takes actions (not --read-only, not a static export).
let actionsAllowed = null;
export function useActions() {
  const [ok, setOk] = useState(actionsAllowed);
  useEffect(() => {
    if (STATIC) { setOk(false); return; }
    if (actionsAllowed === null) call("GET", "steps").then((r) => { actionsAllowed = r.actions; setOk(r.actions); }, () => setOk(false));
  }, []);
  return ok;
}

export function SignIn() {
  const { token, author } = useAuth();
  const [t, setT] = useState(token);
  const [a, setA] = useState(author);
  const [msg, setMsg] = useState(null);
  const save = async () => {
    setAuth(t, a);
    if (NO_TOKEN && !a.trim()) { setMsg({ error: true, text: "Enter your name." }); return; }
    if (!NO_TOKEN && (!t || !a.trim())) { setMsg({ error: true, text: "Enter both the admin token and your name." }); return; }
    try { await call("GET", "settings/auth"); setMsg({ text: "Signed in." }); } catch (e) { setMsg({ error: true, text: e.message }); }
  };
  return html`<div class="panel signin">
    ${NO_TOKEN ? null : html`<label>Admin token <input type="password" autocomplete="off" value=${t} onInput=${(e) => setT(e.target.value)} /></label>`}
    <label>Your name <input type="text" value=${a} onInput=${(e) => setA(e.target.value)} /></label>
    <button type="button" class="primary" onClick=${save}>Sign in</button>
    ${msg ? html`<span class=${msg.error ? "field-error inline" : "ok"}>${msg.text}</span>` : null}
    <span class="muted small">${NO_TOKEN ? "" : html`The token is printed by <code>migrate serve</code> (or set in PATCHMIG_ADMIN_TOKEN). `}Your name is recorded with every action.</span>
  </div>`;
}

export function SignedIn() {
  const { author, ready } = useAuth();
  if (!ready) return null;
  return html`<p class="muted small">Acting as <b>${author}</b> <button type="button" class="link" onClick=${() => setAuth("", author)}>sign out</button></p>`;
}

// Shows the sign-in panel when needed; renders nothing on a read-only server.
export function NeedsSignIn({ children }) {
  const allowed = useActions();
  const { ready } = useAuth();
  if (!allowed) return null;
  return ready ? children : html`<${SignIn} />`;
}

// Start a job; returns it, or throws with the server's reason.
export function startJob(stepNames, options = {}, runId = null) {
  return call("POST", "jobs", { steps: stepNames, options, by: auth.author.trim(), run_id: runId });
}

// A button that starts a job and then opens it on the Run page.
export function JobButton({ steps: stepNames, options, run, label, primary, confirm: question }) {
  const allowed = useActions();
  const { ready } = useAuth();
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  if (!allowed) return null;
  const go = async () => {
    if (!ready) { location.hash = "#/run"; return; }
    if (question && !window.confirm(question)) return;
    setBusy(true); setError(null);
    try { const job = await startJob(stepNames, options, run ?? null); location.hash = `#/run/${job.id}`; }
    catch (e) { setError(e.message); setBusy(false); }
  };
  return html`<span><button type="button" class=${primary ? "primary" : ""} disabled=${busy} onClick=${go}>${label}</button>${error ? html` <span class="field-error inline">${error}</span>` : null}</span>`;
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");
const label = (s) => (s || "").replaceAll("_", " ");
const CREDIT_TAGS = { yes: ["uses credits", "tag-credits"], maybe: ["credits only when needed", "tag-maybe"], none: ["no credits", "tag-free"] };

function duration(job) {
  if (!job.started_at) return "";
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const s = Math.max(0, Math.round((end - new Date(job.started_at)) / 1000));
  return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
}

// ---------- Run page ----------

function OptionInput({ opt, value, set }) {
  if (opt.kind === "bool") {
    const on = value ?? opt.default ?? false;
    return html`<label class="check"><input type="checkbox" checked=${on} onChange=${(e) => set(e.target.checked === Boolean(opt.default) ? undefined : e.target.checked)} /> ${opt.label}</label>`;
  }
  const type = opt.kind === "int" ? "number" : "text";
  const placeholder = opt.help || (opt.kind === "ints" ? "ids, comma-separated" : "");
  return html`<label class="opt">${opt.label}
    <input type=${type} min="1" value=${value ?? ""} placeholder=${placeholder}
      onInput=${(e) => set(e.target.value === "" ? undefined : opt.kind === "int" ? Number(e.target.value) : e.target.value)} /></label>`;
}

function JobView({ id, steps: catalogue, onFinished }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const lines = useRef([]);
  const logEl = useRef(null);
  useEffect(() => {
    let live = true, timer = null, after = 0;
    lines.current = [];
    const poll = async () => {
      try {
        const j = await call("GET", `jobs/${id}?after=${after}`);
        if (!live) return;
        lines.current = lines.current.concat(j.log);
        after = j.lines;
        setJob({ ...j, log: lines.current });
        if (j.state === "running" || j.state === "queued") timer = setTimeout(poll, 1000);
        else onFinished && onFinished();
      } catch (e) { if (live) setError(e.message); }
    };
    poll();
    return () => { live = false; clearTimeout(timer); };
  }, [id]);
  useEffect(() => {
    const el = logEl.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 80) el.scrollTop = el.scrollHeight;
  }, [job?.log.length]);
  if (error) return html`<div class="panel"><p class="error">Job ${id}: ${error}</p></div>`;
  if (!job) return html`<div class="panel"><p class="muted">Loading job ${id}…</p></div>`;
  const titles = Object.fromEntries((catalogue || []).map((s) => [s.name, s.title]));
  const running = job.state === "running";
  const stop = async () => { try { await call("POST", `jobs/${id}/stop`); setJob({ ...job, stop_requested: true }); } catch (e) { setError(e.message); } };
  const seen = new Set(job.log.filter((l) => l.level === "step").map((l) => l.text.replace(/^== /, "")));
  return html`<div class="panel job">
    <h2>Job ${job.id} <span class=${`job-state js-${job.state}`}>${job.state}</span>
      <span class="muted"> · ${job.by} · ${fmtTime(job.started_at)} · ${duration(job)}${job.run_id ? html` · <a href=${`#/r/${job.run_id}`}>run ${job.run_id}</a>` : ""}</span></h2>
    <ol class="job-steps">${job.steps.map((s) => {
      const t = titles[s] || label(s);
      const st = job.current === s ? "current" : seen.has(t) ? "seen" : "";
      return html`<li class=${st}>${t}</li>`;
    })}</ol>
    <p>Copilot credits used by this job: <b>${job.credits.toFixed(1)}</b> <span class="muted">(${job.agent_calls} agent call${job.agent_calls === 1 ? "" : "s"}, estimated)</span></p>
    ${job.error ? html`<p class="error" style="padding:0">${job.error}</p>` : null}
    ${running ? html`<div class="actions">
      <button type="button" onClick=${stop} disabled=${job.stop_requested}>${job.stop_requested ? "Stopping after this step…" : "Stop after this step"}</button>
      <span class="muted small">A running step is not cut off: the ledger stays consistent. You can close this page; the job keeps running on the server.</span></div>` : null}
    <pre class="log" ref=${logEl}>${job.log.map((l) => html`<div class=${`lv-${l.level}`}>${l.text}</div>`)}${running ? html`<div class="muted">…</div>` : null}</pre>
  </div>`;
}

export function RunPage({ runs, jobId, onRunsChanged }) {
  const allowed = useActions();
  const { ready } = useAuth();
  const [cat, setCat] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [options, setOptions] = useState({});
  const [chosen, setChosen] = useState(null);
  const [picked, setRunId] = useState(null);
  const runId = picked ?? runs[0]?.id ?? null;
  const [error, setError] = useState(null);
  const [version, setVersion] = useState(0);
  useEffect(() => { call("GET", "steps").then((c) => { setCat(c); setChosen(new Set(c.pipeline)); }, (e) => setError(e.message)); }, []);
  useEffect(() => { call("GET", "jobs").then(setJobs, () => {}); }, [version, jobId]);
  if (error) return html`<p class="error">${error}</p>`;
  if (!cat) return html`<p class="loading muted">Loading…</p>`;
  const byName = Object.fromEntries(cat.steps.map((s) => [s.name, s]));
  const running = jobs.find((j) => j.state === "running");
  const shown = jobId ? Number(jobId) : running?.id;
  const setOpt = (step, name, v) => setOptions((o) => ({ ...o, [step]: { ...(o[step] || {}), [name]: v } }));
  const optsFor = (names) => Object.fromEntries(names.filter((n) => options[n]).map((n) => [n, Object.fromEntries(Object.entries(options[n]).filter(([, v]) => v !== undefined))]));
  const launch = async (names) => {
    setError(null);
    try {
      const job = await startJob(names, optsFor(names), options.ingest?.new_run ? null : runId);
      setVersion((v) => v + 1);
      location.hash = `#/run/${job.id}`;
    } catch (e) { setError(e.message); }
  };
  const pipe = cat.pipeline.filter((n) => chosen.has(n));
  const credits = pipe.filter((n) => byName[n].credits !== "none");
  const disabled = !allowed || !ready || Boolean(running);
  const runPicker = html`<label class="opt">Run <select value=${runId ?? ""} onChange=${(e) => setRunId(Number(e.target.value))}>
    ${runs.map((r) => html`<option value=${r.id}>Run ${r.id}${r.note ? ` · ${r.note}` : ""}</option>`)}</select></label>`;
  return html`
    <h1>Run <span class="muted">· steps run on the server, one job at a time, with the same code as the command line</span></h1>
    ${allowed === false ? html`<p class="muted">This server is read-only: steps can only be run with the <code>migrate</code> command.</p>` : null}
    <${NeedsSignIn}><${SignedIn} /><//>
    ${error ? html`<p class="field-error banner">${error}</p>` : null}
    ${shown ? html`<${JobView} key=${shown} id=${shown} steps=${cat.steps} onFinished=${() => { setVersion((v) => v + 1); onRunsChanged && onRunsChanged(); }} />` : null}
    <div class="panel">
      <h2>Steps 1 to 4 in one go <span class="muted">· import, analyse, build and test, in this order; stops at the first failure or when the credit budget is reached. Step 5 starts with your review.</span></h2>
      <div class="pipeline">${cat.pipeline.map((n) => html`<label class="check"><input type="checkbox" checked=${chosen.has(n)}
          onChange=${(e) => { const next = new Set(chosen); e.target.checked ? next.add(n) : next.delete(n); setChosen(next); }} /> ${byName[n].title}</label>`)}</div>
      <p class="muted small">${runs.length ? html`Works on the backlog of the run chosen here: ${runPicker}${pipe.includes("ingest") ? " (Import refreshes it; its option New backlog starts a new run)" : ""}` : pipe.includes("ingest") ? "The first import starts the backlog (run 1)." : "No backlog yet: include Import."}
        ${" "}Options set on the step cards below apply here too.${credits.length ? ` Copilot credits: ${credits.map((n) => byName[n].title).join(", ")}.` : ""}</p>
      <div class="actions">
        <button type="button" class="primary" disabled=${disabled || !pipe.length || (!pipe.includes("ingest") && !runs.length)} onClick=${() => launch(pipe)}>Run ${pipe.length} step${pipe.length === 1 ? "" : "s"}</button>
        ${running ? html`<span class="muted">Job ${running.id} is running.</span>` : null}
      </div>
    </div>
    <h2>The workflow, step by step ${runs.length ? html`<span class="muted">· on ${runPicker}</span>` : null}</h2>
    ${(cat.workflow || []).map((w, n) => html`<div class="workflow-step">
      <h3>${n + 1}. ${w.title}</h3><p class="muted small">${w.help}${w.steps.length ? "" : html` <a href=${runId ? `#/r/${runId}/backlog` : "#/run"}>Open the backlog →</a>`}</p>
      ${w.steps.length ? html`<div class="cards">${w.steps.map((name) => byName[name]).filter(Boolean).map((s) => {
      const [tag, cls] = CREDIT_TAGS[s.credits];
      return html`<div class="panel card">
        <h2>${s.title} <span class=${`tag ${cls}`}>${tag}</span></h2>
        <p class="muted small">${s.help}</p>
        ${s.options.length ? html`<div class="opts">${s.options.map((o) => html`<${OptionInput} opt=${o} value=${options[s.name]?.[o.name]} set=${(v) => setOpt(s.name, o.name, v)} />`)}</div>` : null}
        <button type="button" disabled=${disabled || (s.needs_run && !runs.length)} onClick=${() => launch([s.name])}>Run</button>
      </div>`;
    })}</div>` : null}</div>`)}
    ${jobs.length ? html`<div class="panel table-wrap"><h2>Jobs</h2><table>
      <thead><tr><th>Job</th><th>Steps</th><th>By</th><th>State</th><th>Started</th><th>Took</th><th class="num">Credits</th><th>Run</th></tr></thead>
      <tbody>${jobs.slice(0, 50).map((j) => html`<tr>
        <td><a href=${`#/run/${j.id}`}>${j.id}</a></td><td class="wrap">${j.steps.map((n) => byName[n]?.title || n).join(", ")}</td><td>${j.by}</td>
        <td><span class=${`job-state js-${j.state}`}>${j.state}</span></td><td>${fmtTime(j.started_at)}</td><td>${duration(j)}</td>
        <td class="num">${j.credits.toFixed(1)}</td><td>${j.run_id ? html`<a href=${`#/r/${j.run_id}`}>${j.run_id}</a>` : "–"}</td></tr>`)}</tbody>
    </table></div>` : null}`;
}

// ---------- review actions ----------

const WHAT = {
  approve: ["Approve", "Why (optional)"],
  reject: ["Reject", "What is wrong: the cluster agent gets this"],
  retry: ["Retry", "A hint for the agent (optional)"],
  decide: ["Decide", "Your answer to the question"],
};

function actionsFor(queue) {
  if (queue === "quick_approve" || queue === "careful_review") return ["approve", "reject"];
  if (queue === "dropped") return ["approve", "reject"];
  if (queue === "failed") return ["retry"];
  if (queue === "needs_decision") return ["decide"];
  if (queue === "approved") return ["reject"];
  return [];
}

export function ReviewActions({ run, cluster, queue, onDone, compact }) {
  const allowed = useActions();
  const { ready } = useAuth();
  const [open, setOpen] = useState(null);
  const [reason, setReason] = useState("");
  const [choice, setChoice] = useState("migrate");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const acts = actionsFor(queue);
  if (!allowed || !acts.length) return null;
  if (!ready) return html`<a href="#/run" class="small">sign in to review</a>`;
  const send = async (action) => {
    setBusy(true); setError(null);
    try {
      await call("POST", `runs/${run}/clusters/${cluster}/review`, { action, by: auth.author.trim(), reason, choice });
      setOpen(null); setReason(""); onDone && onDone();
    } catch (e) { setError(e.message); }
    setBusy(false);
  };
  const dropped = queue === "dropped";
  const names = { ...WHAT, approve: [dropped ? "Confirm drop" : "Approve", WHAT.approve[1]], reject: [dropped ? "Needed after all" : queue === "approved" ? "Reopen" : "Reject", WHAT.reject[1]] };
  return html`<div class=${compact ? "review-actions compact" : "review-actions"}>
    <div class="actions">${acts.map((a) => html`<button type="button" class=${a === "approve" ? "primary" : ""} onClick=${() => { setOpen(open === a ? null : a); setError(null); }}>${names[a][0]}</button>`)}</div>
    ${open ? html`<div class="inline-form">
      ${open === "decide" ? html`<label class="opt">Choice <select value=${choice} onChange=${(e) => setChoice(e.target.value)}>
        <option value="migrate">migrate: it is needed</option><option value="upstream">upstream: already in the target</option><option value="obsolete">obsolete: not needed any more</option></select></label>` : null}
      <textarea rows="2" placeholder=${names[open][1]} value=${reason} onInput=${(e) => setReason(e.target.value)}></textarea>
      <div class="actions"><button type="button" class="primary" disabled=${busy} onClick=${() => send(open)}>${names[open][0]}</button>
        <button type="button" onClick=${() => setOpen(null)}>Cancel</button>
        ${open === "reject" || open === "retry" || (open === "decide" && choice === "migrate") ? html`<span class="muted small">Then run <b>Migrate clusters</b> on the Run page.</span>` : null}</div>
    </div>` : null}
    ${error ? html`<p class="field-error">${error}</p>` : null}
  </div>`;
}

// ---------- engineer notes ----------

const SCOPES = [["all", "every patch"], ["path", "a path glob"], ["patch", "one patch (id)"], ["cluster", "one cluster (id)"], ["ticket", "one Jira ticket"]];
const STAGE_INFO = { analyse: "analyst", migrate: "cluster agent, reviewer, build fixer" };

function Stages({ value, set }) {
  return html`<span class="stages">${Object.entries(STAGE_INFO).map(([s, help]) => html`<label class="check" title=${help}><input type="checkbox" checked=${value.includes(s)}
    onChange=${(e) => set(e.target.checked ? [...value, s] : value.filter((x) => x !== s))} /> ${s}</label>`)}</span>`;
}

export function NoteForm({ run, scope: fixedScope, target: fixedTarget, onDone }) {
  const [scope, setScope] = useState(fixedScope || "all");
  const [target, setTarget] = useState(fixedTarget ?? "");
  const [stages, setStages] = useState(["analyse", "migrate"]);
  const [text, setText] = useState("");
  const [error, setError] = useState(null);
  const save = async () => {
    setError(null);
    try {
      await call("POST", "notes", { run_id: run, text, by: auth.author.trim(), scope, target: scope === "all" ? null : target, stages });
      setText(""); onDone && onDone();
    } catch (e) { setError(e.message); }
  };
  return html`<div class="inline-form">
    ${fixedScope ? null : html`<div class="opts"><label class="opt">For <select value=${scope} onChange=${(e) => setScope(e.target.value)}>
        ${SCOPES.map(([s, l]) => html`<option value=${s}>${l}</option>`)}</select></label>
      ${scope !== "all" ? html`<label class="opt">${scope === "path" ? "Glob" : scope === "ticket" ? "Jira key" : "Id"}
        <input type="text" value=${target} placeholder=${scope === "path" ? "frameworks/base/services/*/power/*" : ""} onInput=${(e) => setTarget(e.target.value)} /></label>` : null}</div>`}
    <textarea rows="3" placeholder="What the agents should know" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>
    <div class="actions"><span class="muted small">Given to:</span> <${Stages} value=${stages} set=${setStages} />
      <button type="button" class="primary" disabled=${!text.trim() || !stages.length} onClick=${save}>Add note</button>
      ${error ? html`<span class="field-error inline">${error}</span>` : null}</div>
  </div>`;
}

function NoteRow({ n, onDone }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(n.text);
  const [stages, setStages] = useState(n.stages);
  const [error, setError] = useState(null);
  const allowed = useActions();
  const { ready } = useAuth();
  const save = async () => {
    try { await call("PUT", `notes/${n.id}`, { by: auth.author.trim(), text, stages }); setEditing(false); onDone(); } catch (e) { setError(e.message); }
  };
  const remove = async () => {
    if (!window.confirm(`Remove note ${n.id}? The agents stop getting it.`)) return;
    try { await call("POST", `notes/${n.id}/remove`, { by: auth.author.trim() }); onDone(); } catch (e) { setError(e.message); }
  };
  return html`<li class="note">
    <span class="muted">[${n.id}] for ${n.where} · ${n.stages.join(" + ")} · ${n.author}${n.edits ? ` · edited ${n.edits}×` : ""}${n.stale ? " · changed since this result" : ""}${n.removed_at ? ` · removed ${fmtTime(n.removed_at)}` : ""}</span>
    ${allowed && ready && !n.removed_at && !editing ? html` <button type="button" class="link" onClick=${() => setEditing(true)}>edit</button><button type="button" class="link" onClick=${remove}>remove</button>` : null}
    ${editing ? html`<div class="inline-form"><textarea rows="3" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>
      <div class="actions"><${Stages} value=${stages} set=${setStages} /><button type="button" class="primary" disabled=${!text.trim() || !stages.length} onClick=${save}>Save</button>
        <button type="button" onClick=${() => { setEditing(false); setText(n.text); setStages(n.stages); }}>Cancel</button></div></div>`
    : html`<div class="note-text">${n.text}</div>`}
    ${error ? html`<p class="field-error">${error}</p>` : null}
  </li>`;
}

// The notes on a patch or cluster page, with a form for one more about it.
export function NotesPanel({ notes, run, scope, target, onDone }) {
  const allowed = useActions();
  const { ready } = useAuth();
  const [adding, setAdding] = useState(false);
  if (!notes?.length && !allowed) return null;
  return html`<div class="panel"><h2>Engineer notes <span class="muted">(given to the agents${STATIC ? "" : html`; <a href=${`#/r/${run}/notes`}>all notes</a>`})</span></h2>
    ${notes?.length ? html`<ul class="plain notes">${notes.map((n) => html`<${NoteRow} key=${n.id} n=${n} onDone=${onDone} />`)}</ul>` : html`<p class="muted">No notes reach this ${scope} yet.</p>`}
    ${allowed && ready ? (adding ? html`<${NoteForm} run=${run} scope=${scope} target=${target} onDone=${() => { setAdding(false); onDone(); }} />`
      : html`<button type="button" onClick=${() => setAdding(true)}>Add a note for this ${scope}</button>`) : null}
  </div>`;
}

export function NotesPage({ run }) {
  const [notes, setNotes] = useState(null);
  const [stale, setStale] = useState(null);
  const [removed, setRemoved] = useState(false);
  const [version, setVersion] = useState(0);
  const [error, setError] = useState(null);
  const allowed = useActions();
  const { ready } = useAuth();
  useEffect(() => {
    call("GET", `notes${removed ? "?removed=true" : ""}`).then(setNotes, (e) => setError(e.message));
    call("GET", `runs/${run}/stale`).then(setStale, () => setStale(null));
  }, [run, removed, version]);
  if (error) return html`<p class="error">${error}</p>`;
  if (!notes) return html`<p class="loading muted">Loading…</p>`;
  const reload = () => setVersion((v) => v + 1);
  return html`
    <h1>Engineer notes <span class="muted">· what the agents should know beyond the patches and tickets; notes carry over to later runs</span></h1>
    <${NeedsSignIn}>${html`<div class="panel"><h2>Add a note</h2><${NoteForm} run=${run} onDone=${reload} /></div>`}<//>
    ${stale && (stale.items.length || stale.clusters.length) ? html`<div class="panel">
      <h2>Made with information that changed since <span class="muted">· rerun only these to save credits</span></h2>
      ${stale.items.length ? html`<p>${stale.items.length} backlog item(s): ${stale.items.map((x, i) => html`${i ? ", " : ""}<a href=${`#/r/${run}/backlog/${x.name}`}>${x.name}</a>`)}</p>` : null}
      ${stale.clusters.length ? html`<p>${stale.clusters.length} cluster(s): ${stale.clusters.map((x, i) => html`${i ? ", " : ""}<a href=${`#/r/${run}/clusters/${x.id}`}>${x.name}</a> <span class="muted">(${x.why})</span>`)}</p>` : null}
      <div class="actions">
        ${stale.items.length ? html`<${JobButton} steps=${["analyse"]} run=${run} label="Analyse stale items again" />` : null}
        ${stale.clusters.length ? html`<${JobButton} steps=${["apply"]} options=${{ apply: { stale: true } }} run=${run} label="Rerun stale clusters" />` : null}
        ${stale.items.length && stale.clusters.length ? html`<${JobButton} steps=${["analyse", "apply"]} options=${{ apply: { stale: true } }} run=${run} label="Both, analysis first" primary=${true} />` : null}
      </div></div>` : null}
    <div class="panel">
      <h2>Notes <span class="muted">(${notes.length})</span> <label class="check small" style="margin-left:12px"><input type="checkbox" checked=${removed} onChange=${(e) => setRemoved(e.target.checked)} /> show removed</label></h2>
      ${notes.length ? html`<ul class="plain notes">${notes.map((n) => html`<${NoteRow} key=${`${n.id}-${n.updated_at}`} n=${n} onDone=${reload} />`)}</ul>` : html`<p class="muted">No notes yet.</p>`}
      ${allowed && !ready ? html`<p class="muted small">Sign in to add, edit or remove notes.</p>` : null}
    </div>`;
}

// ---------- backlog: add information to an item, then import and analyse it again ----------

const enc = (key) => encodeURIComponent(key);

function readFile(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result.slice(r.result.indexOf(",") + 1)); // base64 after the data: prefix
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

export function InfoForm({ run, item, onDone }) {
  const allowed = useActions();
  const { ready } = useAuth();
  const [kind, setKind] = useState("text");
  const [text, setText] = useState("");
  const [file, setFile] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  if (!allowed) return null;
  if (!ready) return html`<p class="muted small"><a href="#/run">Sign in</a> to add information.</p>`;
  const send = async () => {
    setBusy(true); setError(null);
    try {
      const body = { by: auth.author.trim() };
      if (kind === "text") body.text = text;
      else if (kind === "link") body.link = text;
      else Object.assign(body, { filename: file.name, content: await readFile(file), base64: true });
      await call("POST", `runs/${run}/backlog/${enc(item.key)}/info`, body);
      setText(""); setFile(null); onDone && onDone();
    } catch (e) { setError(e.message); }
    setBusy(false);
  };
  const empty = kind === "file" ? !file : !text.trim();
  return html`<div class="inline-form">
    <div class="opts"><label class="opt">Add <select value=${kind} onChange=${(e) => { setKind(e.target.value); setError(null); }}>
      <option value="text">text: a note for the agents</option><option value="link">a link: commit, PR, spec…</option>
      <option value="file">a file: .diff/.patch, log, document</option></select></label></div>
    ${kind === "text" ? html`<textarea rows="3" placeholder="What the ticket leaves out" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>`
      : kind === "link" ? html`<input type="text" placeholder="https://…" value=${text} onInput=${(e) => setText(e.target.value)} />`
      : html`<input type="file" onChange=${(e) => setFile(e.target.files[0] || null)} />`}
    <div class="actions"><button type="button" class="primary" disabled=${busy || empty} onClick=${send}>Add</button>
      <span class="muted small">${item.kind === "ticket" ? "Commit/PR links and .diff/.patch files become patches of the ticket at the next import; the rest is context for the analysis." : "Context for the analysis of this patch."}</span>
      ${error ? html`<span class="field-error inline">${error}</span>` : null}</div>
  </div>`;
}

export function RemoveAddition({ run, item, addition, onDone }) {
  const allowed = useActions();
  const { ready } = useAuth();
  const [error, setError] = useState(null);
  if (!allowed || !ready || addition.removed_at) return null;
  const go = async () => {
    if (!window.confirm(`Remove ${addition.url || addition.filename}? It stays in the history.`)) return;
    try { await call("POST", `runs/${run}/backlog/${enc(item.key)}/info/${addition.id}/remove`, { by: auth.author.trim() }); onDone && onDone(); }
    catch (e) { setError(e.message); }
  };
  return html` <button type="button" class="link" onClick=${go}>remove</button>${error ? html` <span class="field-error inline">${error}</span>` : null}`;
}

// ---------- migration memory ----------

function Lesson({ l, proposed, onDone }) {
  const allowed = useActions();
  const { ready } = useAuth();
  const [error, setError] = useState(null);
  const decide = async (d) => {
    try { await call("POST", `memory/lessons/${l.name}/${d}`, { by: auth.author.trim() }); onDone(); } catch (e) { setError(e.message); }
  };
  return html`<li class="note"><b>${l.title}</b> <span class="muted">· applies to ${l.applies_to.length ? l.applies_to.join(", ") : "everything"}${l.meta.approved_by ? ` · approved by ${l.meta.approved_by}` : ""}</span>
    <div class="note-text">${l.text}</div>
    ${proposed && allowed && ready ? html`<div class="actions"><button type="button" class="primary" onClick=${() => decide("approve")}>Approve</button>
      <button type="button" onClick=${() => decide("reject")}>Drop</button></div>` : null}
    ${error ? html`<p class="field-error">${error}</p>` : null}</li>`;
}

export function MemoryPage({ run }) {
  const [mem, setMem] = useState(null);
  const [version, setVersion] = useState(0);
  const [error, setError] = useState(null);
  useEffect(() => { call("GET", "memory").then(setMem, (e) => setError(e.message)); }, [version]);
  if (error) return html`<p class="error">${error}</p>`;
  if (!mem) return html`<p class="loading muted">Loading…</p>`;
  const reload = () => setVersion((v) => v + 1);
  return html`
    <h1>Migration memory <span class="muted">· confirmed knowledge: approved results, confirmed ticket analyses, decisions and approved lessons, for the next migration's analyst and cluster agent</span></h1>
    ${!mem.configured ? html`<p class="muted">No memory configured: set <b>memory.path</b> under <a href="#/settings/analysis">Settings</a>.</p>` : null}
    ${mem.error ? html`<p class="muted">${mem.error} It is created when this run is recorded.</p>` : null}
    <${NeedsSignIn}>${null}<//>
    ${mem.configured && run ? html`<div class="actions">
      <${JobButton} steps=${["memory_record"]} run=${run} label=${`Record run ${run} in memory`} />
      <${JobButton} steps=${["memory_curate"]} run=${run} label="Propose lessons (one agent call)" />
    </div>` : null}
    ${mem.configured ? html`<div class="panel"><h2>Proposed <span class="muted">(${mem.proposed.length}) · waiting for a human</span></h2>
      ${mem.proposed.length ? html`<ul class="plain notes">${mem.proposed.map((l) => html`<${Lesson} key=${l.name} l=${l} proposed=${true} onDone=${reload} />`)}</ul>` : html`<p class="muted">None.</p>`}</div>
    <div class="panel"><h2>Approved <span class="muted">(${mem.approved.length})</span></h2>
      ${mem.approved.length ? html`<ul class="plain notes">${mem.approved.map((l) => html`<${Lesson} key=${l.name} l=${l} onDone=${reload} />`)}</ul>` : html`<p class="muted">None.</p>`}</div>` : null}`;
}

// ---------- run report ----------

export function ReportPage({ run }) {
  const [md, setMd] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => { call("GET", `runs/${run}/report`).then((r) => setMd(r.markdown), (e) => setError(e.message)); }, [run]);
  if (error) return html`<p class="error">${error}</p>`;
  if (md === null) return html`<p class="loading muted">Loading…</p>`;
  const download = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
    a.download = `run-${run}-report.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return html`<h1>Report <span class="muted">· run ${run}, for the final human approval</span></h1>
    <div class="actions"><button type="button" class="primary" onClick=${download}>Download Markdown</button></div>
    <div class="panel"><pre class="block">${md}</pre></div>`;
}
