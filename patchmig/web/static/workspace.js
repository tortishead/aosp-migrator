// A workspace server (`migrate serve --workspace`) serves several migration projects: the page
// at / lists them and creates new ones, and each project page (under p/<folder>/) gets a
// switcher in its header. Each project keeps its ledger, jobs and settings in its own folder,
// so switching away and back loses nothing.
import { html, useEffect, useState } from "./htm-preact.js";
import { call, SignedIn, SignIn, useAuth } from "./actions.js";

// undefined: a one-project server or a static export; "": the project list; else this project.
export const WORKSPACE = document.body.dataset.workspace;
const HOME = WORKSPACE ? "../../" : "";
const projectUrl = (folder, hash = "") => `${HOME}p/${encodeURIComponent(folder)}/${hash}`;
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");

function useWorkspace(version = 0) {
  const [state, setState] = useState({ data: null, error: null });
  useEffect(() => {
    if (WORKSPACE === undefined) return;
    fetch(`${HOME}api/workspace`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`${res.status} ${res.statusText}`))))
      .then((data) => setState({ data, error: null }), (error) => setState({ data: null, error }));
  }, [version]);
  return state;
}

// In a project page's header: the other projects of the workspace, and the list of them all.
export function ProjectSwitcher() {
  const { data } = useWorkspace();
  if (WORKSPACE === undefined || !data) return null;
  const go = (folder) => { location.href = folder ? projectUrl(folder) : HOME || "./"; };
  return html`<select class="project-switch" aria-label="Project" value=${WORKSPACE} onChange=${(e) => go(e.target.value)}>
    ${data.projects.map((p) => html`<option value=${p.folder}>${p.name}${p.job ? " · job running" : ""}</option>`)}
    <option value="">All projects…</option>
  </select>`;
}

function ProjectCard({ p }) {
  const run = p.latest_run;
  return html`<div class="panel card">
    <h2><a href=${projectUrl(p.folder)}>${p.name}</a> ${p.job ? html`<span class="job-state js-running">job running</span>` : null}</h2>
    <p class="muted small mono">${p.folder}/project.yaml</p>
    ${p.valid ? html`<p>${p.migration}</p>` : html`<p class="field-error">The config has problems: <a href=${projectUrl(p.folder, "#/settings")}>fix it in the settings</a>.</p>`}
    <p class="small">${run
      ? html`${p.runs} run${p.runs === 1 ? "" : "s"}; latest <a href=${projectUrl(p.folder, `#/r/${run.id}`)}>run ${run.id}</a>${run.note ? ` · ${run.note}` : ""}, ${fmtTime(run.created_at)}: ${run.patches} patches, ${run.clusters} clusters`
      : p.valid ? "No runs yet." : null}</p>
    ${p.job ? html`<p class="small"><a href=${projectUrl(p.folder, `#/run/${p.job.id}`)}>Job ${p.job.id}</a>${p.job.current ? `: ${p.job.current}` : ""}</p>` : null}
    <a class="button-link" href=${projectUrl(p.folder)}>Open</a>
  </div>`;
}

function NewProject({ onMade }) {
  const { ready } = useAuth();
  const [folder, setFolder] = useState("");
  const [error, setError] = useState(null);
  if (!ready) return html`<${SignIn} />`;
  const make = async () => {
    setError(null);
    try {
      const p = await call("POST", "workspace/projects", { folder: folder.trim() });
      onMade();
      location.href = projectUrl(p.folder, "#/settings");
    } catch (e) { setError(e.message); }
  };
  return html`<div class="panel">
    <h2>New project</h2>
    <${SignedIn} />
    <p class="muted small">Creates <code>${folder.trim() || "<folder>"}/project.yaml</code> in the workspace from the example config, then opens its settings to fill in.</p>
    <div class="actions">
      <label class="opt">Folder <input type="text" placeholder="a15-to-a16" value=${folder} onInput=${(e) => setFolder(e.target.value)}
        onKeyDown=${(e) => { if (e.key === "Enter" && folder.trim()) make(); }} /></label>
      <button type="button" class="primary" disabled=${!folder.trim()} onClick=${make}>Create</button>
    </div>
    ${error ? html`<p class="field-error">${error}</p>` : null}
  </div>`;
}

// The page at / of a workspace server.
export function WorkspacePage() {
  const [version, setVersion] = useState(0);
  const { data, error } = useWorkspace(version);
  let body;
  if (error) body = html`<p class="error">${error.message}</p>`;
  else if (!data) body = html`<p class="loading muted">Loading…</p>`;
  else body = html`
    <h1>Projects <span class="muted">· ${data.root}</span></h1>
    ${data.projects.length
      ? html`<div class="cards">${data.projects.map((p) => html`<${ProjectCard} key=${p.folder} p=${p} />`)}</div>`
      : html`<p>No projects yet: every folder here with a <code>project.yaml</code> is one.${data.actions ? " Create the first below." : ""}</p>`}
    ${data.actions ? html`<${NewProject} onMade=${() => setVersion((v) => v + 1)} />` : html`<p class="muted">This server is read-only: projects are created with <code>migrate init</code>.</p>`}
    <p class="muted small">Each project keeps its ledger, jobs and settings in its own folder: switch between them in the header of any project page and come back where you left off. One job runs at a time across all projects.</p>`;
  return html`
    <header><span class="title">Patch migration <small>workspace</small></span></header>
    <main>${body}</main>`;
}
