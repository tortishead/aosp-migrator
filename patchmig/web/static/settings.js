// Project settings: forms generated from the config's JSON schema, plus the raw YAML.
// Values are the ones written in project.yaml; an empty field means "use the default".
// Tokens typed here go to the server's secret store; the page never receives a secret back.
import { createContext, html, useContext, useEffect, useState } from "./htm-preact.js";

const Ctx = createContext(null);
const TOKEN_KEY = "patchmig-admin-token";
const AUTHOR_KEY = "patchmig-author";
const NO_TOKEN = document.body.dataset.token === "off"; // started without --admin-token

const SECTIONS = [
  ["general", "General", ["name", "workdir", "migration"], []],
  ["repos", "Trees and repos", ["trees", "code_hosts"], ["trees", "hosts"]],
  ["sources", "Patch sources", ["patch_sources"], []],
  ["copilot", "Copilot", ["llm", "budget"], ["copilot"]],
  ["jira", "Jira", ["jira"], ["jira"]],
  ["build", "Build and tests", ["build", "tests"], ["build"]],
  ["output", "Publishing", ["output"], ["hosts"]],
  ["analysis", "Analysis and agents", ["analysis", "thresholds", "path_rules", "migrate", "memory"], []],
];

// Every field is explained in docs/settings.md; the anchor is the section's heading there.
const GUIDE = "https://github.com/tortishead/aosp-migrator/blob/main/docs/settings.md";
const GUIDE_ANCHORS = { general: "general", repos: "trees-and-repos", sources: "patch-sources", copilot: "copilot",
  jira: "jira", build: "build-and-tests", output: "publishing", analysis: "analysis-and-agents" };

const CHECK_NAMES = { trees: "Trees", hosts: "Git host tokens", copilot: "Copilot sign-in", jira: "Jira", build: "Build host" };

function stored(storage, key) {
  try { return storage.getItem(key) || ""; } catch { return ""; }
}
function store(storage, key, value) {
  try { value ? storage.setItem(key, value) : storage.removeItem(key); } catch { /* private mode */ }
}

async function call(method, path, token, body) {
  const res = await fetch(`api/${path}`, {
    method,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { status: res.status, data };
}

// ---------- value helpers ----------

const isEmpty = (v) => v === undefined || v === null || v === "";
const dotted = (path) => path.join(".");

function setIn(obj, path, value) {
  if (!path.length) return value;
  const [head, ...rest] = path;
  const copy = Array.isArray(obj) ? [...obj] : { ...(obj || {}) };
  const next = setIn(copy[head], rest, value);
  if (next === undefined && !Array.isArray(copy)) delete copy[head];
  else copy[head] = next;
  return copy;
}

function deref(root, schema) {
  if (!schema || !schema.$ref) return schema || {};
  const target = root.$defs[schema.$ref.split("/").pop()];
  const { $ref, ...rest } = schema;
  return { ...target, ...rest };
}

const showDefault = (d) => (d === undefined || d === null ? "" : typeof d === "object" ? JSON.stringify(d) : String(d));

// ---------- fields ----------

function Problems({ path }) {
  const { problems } = useContext(Ctx);
  const key = dotted(path);
  const mine = problems.filter((p) => p.loc === key);
  return mine.length ? html`<div class="field-error">${mine.map((p) => html`<div>${p.msg}</div>`)}</div>` : null;
}

function Row({ title, help, required, path, children }) {
  const { missing } = useContext(Ctx);
  const unset = missing.includes(dotted(path));
  return html`<div class="field">
    <label>${title}${required ? html`<span class="req" title="required"> *</span>` : null}${unset ? html`<span class="dot" title="required, not set"></span>` : null}</label>
    <div class="control">${children}${help ? html`<small class="muted">${help}</small>` : null}<${Problems} path=${path} /></div>
  </div>`;
}

// Labels come from the field names (the schema's titles are class names for nested models).
const LABELS = {
  llm: "Copilot CLI", github_host: "GitHub host", github_token: "GitHub token", url: "URL",
  api_url: "API URL", ssh: "SSH", jql: "JQL", mcp_flag: "MCP flag", git_url: "Git URL",
  code_hosts: "Code hosts", max_attachment_kb: "Max attachment (KB)", github: "GitHub", gerrit: "Gerrit",
};
const titleOf = (schema, name) => {
  if (name === undefined || name === null) return schema.title || "";
  if (LABELS[name]) return LABELS[name];
  const words = String(name).replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};

function Field({ schema, path, value, set, name, required }) {
  const { root } = useContext(Ctx);
  schema = deref(root, schema);
  const title = titleOf(schema, name);
  const help = schema.description;

  if (schema.anyOf) {
    const variants = schema.anyOf.map((s) => deref(root, s));
    const nonNull = variants.filter((s) => s.type !== "null");
    if (nonNull.length === 1) {
      const inner = { ...nonNull[0], description: schema.description || nonNull[0].description, default: schema.default };
      if (inner.type === "object" && inner.properties) return html`<${OptionalGroup} schema=${inner} path=${path} value=${value} set=${set} name=${name} />`;
      return html`<${Field} schema=${inner} path=${path} value=${value} set=${set} name=${name} required=${false} />`;
    }
    return html`<${UnionField} schema=${schema} variants=${nonNull} path=${path} value=${value} set=${set} title=${title} required=${required} />`;
  }
  if (schema["x-secret"]) return html`<${Row} title=${title} help=${help} required=${required} path=${path}><${SecretField} path=${path} value=${value} set=${set} /><//>`;
  if (schema.const !== undefined) return null;
  if (schema.enum) {
    return html`<${Row} title=${title} help=${help} required=${required} path=${path}>
      <select value=${value ?? ""} onChange=${(e) => set(e.target.value === "" ? undefined : e.target.value)}>
        ${required ? html`<option value="">choose…</option>` : html`<option value="">default${schema.default != null ? ` (${schema.default})` : ""}</option>`}
        ${schema.enum.map((o) => html`<option value=${o}>${o}</option>`)}
      </select><//>`;
  }
  if (schema.type === "object" && schema.properties) return html`<${Group} schema=${schema} path=${path} value=${value} set=${set} title=${title} />`;
  if (schema.type === "object") return html`<${Row} title=${title} help=${help} required=${required} path=${path}><${MapField} schema=${schema} path=${path} value=${value} set=${set} /><//>`;
  if (schema.type === "array") {
    const items = deref(root, schema.items);
    if (items.type === "object" || items.oneOf || items.anyOf) {
      return html`<${ListOfGroups} schema=${schema} items=${items} path=${path} value=${value} set=${set} title=${title} />`;
    }
    return html`<${Row} title=${title} help=${help} required=${required} path=${path}><${Lines} value=${value} set=${set} placeholder=${showDefault(schema.default)} /><//>`;
  }
  if (schema.type === "boolean") {
    const on = value ?? schema.default ?? false;
    return html`<${Row} title=${title} help=${help} required=${required} path=${path}>
      <label class="check"><input type="checkbox" checked=${on} onChange=${(e) => set(e.target.checked)} /> ${on ? "yes" : "no"}${value === undefined ? html` <span class="muted">(default)</span>` : null}</label><//>`;
  }
  if (schema.type === "integer" || schema.type === "number") {
    return html`<${Row} title=${title} help=${help} required=${required} path=${path}>
      <input type="number" step=${schema.type === "integer" ? 1 : "any"} min=${schema.minimum ?? schema.exclusiveMinimum} max=${schema.maximum}
        value=${value ?? ""} placeholder=${showDefault(schema.default)}
        onInput=${(e) => set(e.target.value === "" ? undefined : Number(e.target.value))} /><//>`;
  }
  return html`<${Row} title=${title} help=${help} required=${required} path=${path}>
    <input type="text" value=${value ?? ""} placeholder=${showDefault(schema.default)} onInput=${(e) => set(e.target.value === "" ? undefined : e.target.value)} /><//>`;
}

// Fields that only apply to some values of their siblings. Mirrors the server: with no transport
// set, an ssh section means ssh.
const buildTransport = (build) => build?.transport ?? (build?.ssh ? "ssh" : "local");
const SHOWN_IF = { "build.ssh": (build) => buildTransport(build) === "ssh" };
// Choosing "default" (local) must not let a hidden ssh section turn it back into ssh.
const ON_SET = {
  "build.transport": (build, v) => setIn(build || {}, ["transport"], v === undefined && build?.ssh ? "local" : v),
};

function Props({ schema, path, value, set, skip = [] }) {
  const req = new Set(schema.required || []);
  return Object.entries(schema.properties)
    .filter(([key, prop]) => !skip.includes(key) && !prop.deprecated)
    .filter(([key]) => !SHOWN_IF[dotted([...path, key])] || SHOWN_IF[dotted([...path, key])](value))
    .map(([key, prop]) => {
      const onSet = ON_SET[dotted([...path, key])];
      return html`<${Field} key=${key} name=${key} schema=${prop} path=${[...path, key]} value=${value?.[key]}
        required=${req.has(key)} set=${(v) => set(onSet ? onSet(value, v) : setIn(value || {}, [key], v))} />`;
    });
}

function Group({ schema, path, value, set, title }) {
  return html`<fieldset class="group"><legend>${title}</legend>
    ${schema.description ? html`<p class="muted small">${schema.description}</p>` : null}
    <${Problems} path=${path} />
    <${Props} schema=${schema} path=${path} value=${value} set=${set} /></fieldset>`;
}

function OptionalGroup({ schema, path, value, set, name }) {
  const title = titleOf(schema, name);
  const on = value !== undefined && value !== null;
  return html`<fieldset class=${`group${on ? "" : " off"}`}>
    <legend><label class="check"><input type="checkbox" checked=${on}
      onChange=${(e) => set(e.target.checked ? {} : undefined)} /> ${title}</label></legend>
    ${schema.description ? html`<p class="muted small">${schema.description}</p>` : null}
    <${Problems} path=${path} />
    ${on ? html`<${Props} schema=${schema} path=${path} value=${value} set=${set} />` : html`<p class="muted small">Not configured.</p>`}
  </fieldset>`;
}

function variantLabel(s) {
  if (s.const !== undefined) return JSON.stringify(s.const);
  if (s.type === "array") return "list";
  return s.title || s.type || "value";
}

function variantOf(variants, value) {
  return variants.findIndex((s) => (s.const !== undefined ? value === s.const
    : s.type === "array" ? Array.isArray(value)
      : s.type === "object" ? typeof value === "object" && !Array.isArray(value)
        : s.type === "integer" || s.type === "number" ? typeof value === "number" : typeof value === "string"));
}

function UnionField({ schema, variants, path, value, set, title, required }) {
  const i = isEmpty(value) ? -1 : variantOf(variants, value);
  const pick = (idx) => {
    if (idx < 0) return set(undefined);
    const s = variants[idx];
    set(s.const !== undefined ? s.const : s.type === "array" ? [] : s.type === "object" ? {} : undefined);
  };
  const chosen = i >= 0 ? variants[i] : null;
  return html`<${Row} title=${title} help=${schema.description} required=${required} path=${path}>
    <select value=${i} onChange=${(e) => pick(Number(e.target.value))}>
      <option value="-1">default${schema.default !== undefined ? ` (${showDefault(schema.default)})` : ""}</option>
      ${variants.map((s, idx) => html`<option value=${idx}>${variantLabel(s)}</option>`)}
    </select>
    ${chosen && chosen.type === "array" ? html`<${Lines} value=${value} set=${set} />` : null}
    ${chosen && chosen.const === undefined && chosen.type === "string" ? html`<input type="text" value=${value ?? ""} onInput=${(e) => set(e.target.value)} />` : null}
  <//>`;
}

function Lines({ value, set, placeholder }) {
  const [text, setText] = useState((value || []).join("\n"));
  return html`<textarea rows=${Math.min(8, Math.max(2, text.split("\n").length))} value=${text} placeholder=${placeholder ? `one per line (default: ${placeholder})` : "one per line"}
    onInput=${(e) => {
      setText(e.target.value);
      const lines = e.target.value.split("\n").map((l) => l.trim()).filter(Boolean);
      set(lines.length ? lines : undefined);
    }}></textarea>`;
}

function MapField({ schema, value, set }) {
  const { root } = useContext(Ctx);
  const itemSchema = deref(root, schema.additionalProperties || {});
  const [rows, setRows] = useState(Object.entries(value || {}));
  const asList = itemSchema.type === "array";
  const asNumber = itemSchema.type === "number" || itemSchema.type === "integer";
  const update = (next) => {
    setRows(next);
    const obj = {};
    next.forEach(([k, v]) => { if (k !== "") obj[k] = v; });
    set(Object.keys(obj).length ? obj : undefined);
  };
  const parse = (raw) => (asList ? raw.split(",").map((s) => s.trim()).filter(Boolean) : asNumber ? (raw === "" ? undefined : Number(raw)) : raw);
  const show = (v) => (asList ? (v || []).join(", ") : v ?? "");
  return html`<div class="map">
    ${rows.map(([k, v], i) => html`<div class="map-row">
      <input type="text" placeholder="key" value=${k} onInput=${(e) => update(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
      <input type=${asNumber ? "number" : "text"} step="any" placeholder=${asList ? "comma-separated" : "value"} value=${show(v)}
        onInput=${(e) => update(rows.map((r, j) => (j === i ? [r[0], parse(e.target.value)] : r)))} />
      <button type="button" class="link" onClick=${() => update(rows.filter((_, j) => j !== i))}>remove</button>
    </div>`)}
    <button type="button" class="link" onClick=${() => setRows([...rows, ["", asList ? [] : ""]])}>+ add</button>
    ${schema.default && Object.keys(schema.default).length && !rows.length ? html`<small class="muted">default: ${showDefault(schema.default)}</small>` : null}
  </div>`;
}

function ListOfGroups({ schema, items, path, value, set, title }) {
  const { root, shiftSecrets } = useContext(Ctx);
  const list = value || [];
  const disc = items.discriminator;
  const variants = disc ? Object.entries(disc.mapping).map(([k, ref]) => [k, deref(root, { $ref: ref })]) : null;
  const remove = (i) => { shiftSecrets(dotted(path), i); const next = list.filter((_, j) => j !== i); set(next.length ? next : undefined); };
  const add = () => set([...list, disc ? { [disc.propertyName]: variants[0][0] } : {}]);
  return html`<fieldset class="group"><legend>${title}</legend>
    ${schema.description ? html`<p class="muted small">${schema.description}</p>` : null}
    <${Problems} path=${path} />
    ${list.map((item, i) => {
      const itemPath = [...path, i];
      const setItem = (v) => set(list.map((x, j) => (j === i ? v : x)));
      let body;
      if (disc) {
        const kind = item?.[disc.propertyName];
        const variant = (variants.find(([k]) => k === kind) || [])[1];
        body = html`<${Row} title=${titleOf({}, disc.propertyName)} required=${true} path=${[...itemPath, disc.propertyName]}>
            <select value=${kind} onChange=${(e) => setItem({ [disc.propertyName]: e.target.value })}>
              ${variants.map(([k]) => html`<option value=${k}>${k}</option>`)}</select><//>
          ${variant ? html`<${Props} schema=${variant} path=${itemPath} value=${item} set=${setItem} skip=${[disc.propertyName]} />` : null}`;
      } else {
        body = html`<${Props} schema=${items} path=${itemPath} value=${item} set=${setItem} />`;
      }
      return html`<div class="list-item" key=${i}>
        <div class="list-head"><span class="muted">${i + 1}</span><button type="button" class="link" onClick=${() => remove(i)}>remove</button></div>
        ${body}</div>`;
    })}
    <button type="button" onClick=${add}>+ Add ${String(path[path.length - 1]).replace(/s$/, "").replaceAll("_", " ")}</button>
  </fieldset>`;
}

function SecretField({ path, value, set }) {
  const { status, pending, setSecret } = useContext(Ctx);
  const key = dotted(path);
  const st = status[key];
  const typed = pending[key];
  const [mode, setMode] = useState(null); // "token" | "ref"
  let now;
  if (typed !== undefined) now = html`<b>new token entered</b> <span class="muted">(stored when you save)</span>`;
  else if (!value) now = html`<span class="muted">not set</span>`;
  else if (st && st.managed && st.ref === value) now = html`<b>token stored</b>${st.available ? "" : html` <span class="field-error inline">file missing</span>`}`;
  else now = html`<code>${value}</code>${st && st.ref === value && !st.available ? html` <span class="field-error inline">not available on the server</span>` : ""}`;
  return html`<div class="secret">
    <div>${now}</div>
    <div class="secret-actions">
      <button type="button" class="link" onClick=${() => setMode(mode === "token" ? null : "token")}>${value || typed ? "replace token" : "enter token"}</button>
      <button type="button" class="link" onClick=${() => setMode(mode === "ref" ? null : "ref")}>use env:/file: reference</button>
      ${value || typed ? html`<button type="button" class="link" onClick=${() => { setSecret(key, undefined); set(undefined); setMode(null); }}>remove</button>` : null}
    </div>
    ${mode === "token" ? html`<input type="password" autocomplete="new-password" placeholder="paste the token" value=${typed || ""}
      onInput=${(e) => setSecret(key, e.target.value || undefined)} />` : null}
    ${mode === "ref" ? html`<input type="text" placeholder="env:VARIABLE or file:/path/on/the/server" value=${typed === undefined && value && !(st && st.managed) ? value : ""}
      onInput=${(e) => { setSecret(key, undefined); set(e.target.value || undefined); }} />` : null}
  </div>`;
}

// ---------- page ----------

function Checks({ names, token, dirty }) {
  const [results, setResults] = useState({});
  if (!names.length) return null;
  const run = async (name) => {
    setResults((r) => ({ ...r, [name]: "running" }));
    const res = await call("POST", `settings/check/${name}`, token);
    setResults((r) => ({ ...r, [name]: res.status === 200 ? res.data : [{ name, ok: false, detail: res.data.detail || `HTTP ${res.status}` }] }));
  };
  return html`<div class="panel checks"><h2>Connection tests <span class="muted">· use the saved settings${dirty ? "; save first" : ""}, cost no Copilot credits</span></h2>
    ${names.map((n) => html`<div class="check-block">
      <button type="button" onClick=${() => run(n)} disabled=${results[n] === "running"}>Test ${CHECK_NAMES[n]}</button>
      ${results[n] === "running" ? html` <span class="muted">running…</span>` : null}
      ${Array.isArray(results[n]) ? html`<ul class="plain">${results[n].map((c) => html`<li><span class=${c.ok ? "ok" : "field-error inline"}>${c.ok ? "ok" : "failed"}</span> ${c.name}${c.detail ? html`: <span class="muted">${c.detail}</span>` : null}</li>`)}</ul>` : null}
    </div>`)}</div>`;
}

const parseProblems = (list) => (list || []).map((p) => {
  const i = p.indexOf(": ");
  return i > 0 ? { loc: p.slice(0, i), msg: p.slice(i + 2), text: p } : { loc: "", msg: p, text: p };
});

export function SettingsPage({ section }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [values, setValues] = useState({});
  const [pending, setPending] = useState({});
  const [yamlText, setYamlText] = useState("");
  const [problems, setProblems] = useState([]);
  const [message, setMessage] = useState(null);
  const [formKey, setFormKey] = useState(0);
  const [token, setToken] = useState(stored(sessionStorage, TOKEN_KEY));
  const [author, setAuthor] = useState(stored(localStorage, AUTHOR_KEY));
  const [busy, setBusy] = useState(false);

  const accept = (v) => {
    setView(v); setValues(v.values || {}); setPending({}); setYamlText(v.yaml || "");
    setProblems(parseProblems(v.problems)); setFormKey((k) => k + 1);
  };
  useEffect(() => {
    call("GET", "settings").then((r) => (r.status === 200 ? accept(r.data) : setError(r.data.detail || `HTTP ${r.status}`)), (e) => setError(e.message));
  }, []);

  if (error) return html`<p class="error">Settings not available: ${error}</p>`;
  if (!view) return html`<p class="loading muted">Loading…</p>`;

  const schema = view.schema;
  const listed = new Set(SECTIONS.flatMap((s) => s[2]));
  const other = Object.keys(schema.properties).filter((k) => !listed.has(k) && k !== "version");
  const sections = other.length ? [...SECTIONS, ["other", "Other", other, []]] : SECTIONS;
  const current = section === "yaml" ? null : sections.find((s) => s[0] === section) || sections[0];
  const dirty = JSON.stringify(values) !== JSON.stringify(view.values || {}) || Object.keys(pending).length > 0;
  const yamlDirty = yamlText !== (view.yaml || "");
  const inSection = (keys) => (loc) => keys.some((k) => loc === k || loc.startsWith(`${k}.`));
  const missing = view.missing || [];
  const missingIn = (keys) => missing.filter(inSection(keys));
  const countFor = (keys) => problems.filter((p) => inSection(keys)(p.loc) && !missing.includes(p.loc)).length;

  const ctx = {
    root: schema, problems, missing: view.missing || [], status: view.secrets || {}, pending,
    setSecret: (key, v) => setPending((p) => { const n = { ...p }; if (v === undefined) delete n[key]; else n[key] = v; return n; }),
    shiftSecrets: (prefix, removed) => setPending((p) => {
      const n = {};
      Object.entries(p).forEach(([k, v]) => {
        if (!k.startsWith(`${prefix}.`)) { n[k] = v; return; }
        const [idx, ...rest] = k.slice(prefix.length + 1).split(".");
        const i = Number(idx);
        if (i === removed) return;
        n[[prefix, i > removed ? i - 1 : i, ...rest].join(".")] = v;
      });
      return n;
    }),
  };

  const save = async (kind) => {
    if (!token && !NO_TOKEN) { setMessage({ error: true, text: "Enter the admin token first (printed by migrate serve, or PATCHMIG_ADMIN_TOKEN)." }); return; }
    if (!author.trim()) { setMessage({ error: true, text: "Enter your name: it is recorded with the change." }); return; }
    setBusy(true); setMessage(null);
    const body = kind === "yaml" ? { version: view.version, author, yaml: yamlText } : { version: view.version, author, values, secrets: pending };
    const res = await call("PUT", kind === "yaml" ? "settings/yaml" : "settings", token, body).catch((e) => ({ status: 0, data: { detail: e.message } }));
    setBusy(false);
    if (res.status === 200) {
      accept(res.data);
      window.dispatchEvent(new Event("patchmig-settings-saved"));
      setMessage(res.data.problems?.length
        ? { error: true, text: "Saved, but the settings are not complete yet: fill in the sections with a red dot and fix the problems below. Runs keep the last complete settings until then." }
        : res.data.reload_error ? { error: true, text: `Saved, but the server could not load it: ${res.data.reload_error}` } : { text: "Saved. New runs and commands use these settings." });
    } else if (res.status === 422) {
      setProblems(parseProblems(res.data.detail.problems));
      setMessage({ error: true, text: "Not saved: fix the problems below." });
    } else if (res.status === 401) {
      setMessage({ error: true, text: "Not saved: the admin token is wrong." });
    } else {
      const d = res.data.detail;
      setMessage({ error: true, text: `Not saved: ${d?.problems ? d.problems.join("; ") : d || `HTTP ${res.status}`}` });
    }
  };
  const discard = () => accept(view);

  return html`<${Ctx.Provider} value=${ctx}>
    <h1>Settings <span class="muted">· ${view.path}</span></h1>
    <p class="muted small">What each field means and what to enter: <a href=${`${GUIDE}#${(current && GUIDE_ANCHORS[current[0]]) || ""}`} target="_blank" rel="noopener">settings guide</a>.</p>
    ${view.editable ? html`<div class="panel signin">
        ${NO_TOKEN ? null : html`<label>Admin token <input type="password" autocomplete="off" value=${token} onInput=${(e) => { setToken(e.target.value); store(sessionStorage, TOKEN_KEY, e.target.value); }} /></label>`}
        <label>Your name <input type="text" value=${author} onInput=${(e) => { setAuthor(e.target.value); store(localStorage, AUTHOR_KEY, e.target.value); }} /></label>
        <span class="muted small">Saving rewrites ${view.exists ? "the file without its comments (the YAML tab keeps them)" : "a new file"}; the previous version is kept in .patchmig/config-history. Tokens go to ${view.secrets_dir}.</span>
      </div>` : html`<p class="muted">Read-only: this server was started with <code>--read-only</code>.</p>`}
    ${!view.exists ? html`<p class="muted">No config file yet: fill in the required fields (*) and save to create it.</p>` : null}
    <nav class="subtabs">
      ${sections.map(([id, name, keys]) => {
        const n = countFor(keys);
        const unset = missingIn(keys);
        return html`<a class=${current && current[0] === id ? "active" : ""} href=${`#/settings/${id}`}>${name}${unset.length
          ? html`<span class="dot" title=${`required fields not set: ${unset.join(", ")}`}></span>` : null}${n ? html` <span class="count">${n}</span>` : null}</a>`;
      })}
      <a class=${section === "yaml" ? "active" : ""} href="#/settings/yaml">YAML</a>
    </nav>
    ${message ? html`<p class=${message.error ? "field-error banner" : "ok banner"}>${message.text}</p>` : null}
    ${problems.length ? html`<div class="panel"><h2>Problems <span class="muted">(${problems.length})</span></h2><ul class="plain">${problems.map((p) => html`<li><code>${p.loc || "config"}</code> ${p.msg}</li>`)}</ul></div>` : null}
    ${current ? html`<fieldset class="form" disabled=${!view.editable} key=${`${formKey}-${current[0]}`}>
        ${current[2].map((key) => html`<div class="panel"><${Field} name=${key} schema=${schema.properties[key]} path=${[key]} value=${values[key]}
            required=${(schema.required || []).includes(key)} set=${(v) => setValues((old) => setIn(old, [key], v))} /></div>`)}
        <div class="actions">
          <button type="button" class="primary" onClick=${() => save("form")} disabled=${busy || !dirty}>Save</button>
          <button type="button" onClick=${discard} disabled=${busy || !dirty}>Discard changes</button>
          ${dirty ? html`<span class="muted">Unsaved changes (all sections)</span>` : null}
        </div>
      </fieldset>
      ${view.editable ? html`<${Checks} names=${current[3]} token=${token} dirty=${dirty} />` : null}`
    : html`<fieldset class="form" disabled=${!view.editable}><div class="panel">
        <p class="muted small">The whole file as text. Saving it keeps comments. Secrets must be <code>env:</code> or <code>file:</code> references here; use the forms to store a token.</p>
        <textarea class="yaml" spellcheck="false" value=${yamlText} onInput=${(e) => setYamlText(e.target.value)}></textarea>
        <div class="actions">
          <button type="button" class="primary" onClick=${() => save("yaml")} disabled=${busy || !yamlDirty}>Save YAML</button>
          <button type="button" onClick=${() => setYamlText(view.yaml || "")} disabled=${busy || !yamlDirty}>Discard changes</button>
          ${dirty ? html`<span class="muted">The forms have unsaved changes; saving here discards them.</span>` : null}
        </div></div></fieldset>`}
  <//>`;
}
