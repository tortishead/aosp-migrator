import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";

import { modelUsage, premiumRequests } from "../copilot/usage";
import { api, type CopilotSettingsUpdate, type CopilotTestResult, type CopilotTokenCheck, type JiraTestResult, type Settings, type Source } from "../api";

const settingsKey = ["settings"];

type SectionId = "jira" | "prescan" | "copilot" | "build";

const SECTIONS: { id: SectionId; title: string; summary: string; icon: ReactNode }[] = [
  { id: "jira", title: "Jira", summary: "Server and token", icon: <IconTicket /> },
  { id: "prescan", title: "Pre-scan", summary: "Upstream detection", icon: <IconScan /> },
  { id: "copilot", title: "Copilot", summary: "CLI, models and limits", icon: <IconSpark /> },
  { id: "build", title: "Build", summary: "Dependent modules", icon: <IconHammer /> },
];

/** The search box text, lower-cased; settings that do not match it hide. */
const SearchContext = createContext("");

export function SettingsPage() {
  const settings = useQuery({ queryKey: settingsKey, queryFn: api.settings });
  const [query, setQuery] = useState("");
  const [active, setActive] = useState<SectionId>("jira");
  const [visible, setVisible] = useState<Record<SectionId, boolean>>({ jira: true, prescan: true, copilot: true, build: true });
  const panel = useRef<HTMLDivElement>(null);
  const needle = query.trim().toLowerCase();

  // A section shows while one of its settings matches the search.
  useLayoutEffect(() => {
    const root = panel.current;
    if (!root) return;
    const next = Object.fromEntries(
      SECTIONS.map(({ id }) => [id, !needle || root.querySelector(`#settings-${id} .setting:not([hidden])`) !== null]),
    ) as Record<SectionId, boolean>;
    setVisible((prev) => (SECTIONS.every(({ id }) => prev[id] === next[id]) ? prev : next));
  }, [needle, settings.data]);

  // Highlights the section being read, like VS Code's table of contents.
  useEffect(() => {
    const root = panel.current;
    if (!root || !settings.data || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        const top = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (top) setActive(top.target.id.replace("settings-", "") as SectionId);
      },
      { rootMargin: "-80px 0px -55% 0px" },
    );
    root.querySelectorAll(".settings-section").forEach((section) => observer.observe(section));
    return () => observer.disconnect();
  }, [settings.data]);

  function jump(id: SectionId) {
    setActive(id);
    document.getElementById(`settings-${id}`)?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }

  const nothingFound = needle !== "" && SECTIONS.every(({ id }) => !visible[id]);

  return (
    <div className="settings">
      <div className="settings-head">
        <h1>Settings</h1>
        <div className="settings-search">
          <IconSearch />
          <input
            type="search"
            aria-label="Search settings"
            placeholder="Search settings"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      </div>
      {settings.isPending && <p>Loading…</p>}
      {settings.isError && <p className="error">{settings.error.message}</p>}
      {settings.data && (
        <div className="settings-layout">
          <nav className="settings-toc" aria-label="Settings sections">
            {SECTIONS.map(({ id, title, summary, icon }) => (
              <button
                key={id}
                type="button"
                className={`settings-toc-item${active === id ? " active" : ""}`}
                aria-current={active === id ? "true" : undefined}
                disabled={!visible[id]}
                onClick={() => jump(id)}
              >
                <span className="settings-toc-icon">{icon}</span>
                <span className="settings-toc-text">
                  <span className="settings-toc-title">{title}</span>
                  <span className="settings-toc-summary">{summary}</span>
                </span>
              </button>
            ))}
          </nav>
          <div className="settings-panel" ref={panel}>
            <SearchContext.Provider value={needle}>
              <JiraSettingsForm settings={settings.data} hidden={!visible.jira} />
              <PrescanSettingsForm settings={settings.data} hidden={!visible.prescan} />
              <CopilotSettingsForm settings={settings.data} hidden={!visible.copilot} />
              <BuildSettingsForm settings={settings.data} hidden={!visible.build} />
            </SearchContext.Provider>
            {nothingFound && <p className="settings-empty">No settings found for “{query.trim()}”.</p>}
          </div>
        </div>
      )}
    </div>
  );
}

function Section({
  id,
  description,
  hidden,
  onSubmit,
  footer,
  children,
}: {
  id: SectionId;
  description: string;
  hidden: boolean;
  onSubmit: (event: FormEvent) => void;
  footer: ReactNode;
  children: ReactNode;
}) {
  const { title, icon } = SECTIONS.find((s) => s.id === id)!;
  return (
    <form id={`settings-${id}`} className="settings-section" aria-label={title} onSubmit={onSubmit} hidden={hidden}>
      <header className="settings-section-head">
        <span className="settings-section-icon">{icon}</span>
        <div>
          <h2>{title}</h2>
          <p className="hint">{description}</p>
        </div>
      </header>
      <div className="settings-list">{children}</div>
      <footer className="settings-section-foot">{footer}</footer>
    </form>
  );
}

/** One setting, VS Code style: category and name, description, then the control. */
function Setting({
  category,
  label,
  description,
  source,
  children,
}: {
  category: string;
  label: string;
  description?: ReactNode;
  source?: Source;
  children: (id: string) => ReactNode;
}) {
  const query = useContext(SearchContext);
  const id = useId();
  const text = `${category} ${label} ${typeof description === "string" ? description : ""}`.toLowerCase();
  const hidden = query !== "" && !text.includes(query);
  return (
    <div className={`setting${source === "file" ? " modified" : ""}`} hidden={hidden} title={source === "file" ? "Saved in the settings file" : undefined}>
      <div className="setting-title">
        <span className="setting-category">{category}: </span>
        <label htmlFor={id}>{label}</label>
        {source === "env" && <span className="setting-badge">Environment</span>}
      </div>
      {description && <div className="setting-description">{description}</div>}
      <div className="setting-control">{children(id)}</div>
    </div>
  );
}

function SaveStatus({ mutation }: { mutation: { isError: boolean; isSuccess: boolean; error: Error | null } }) {
  if (mutation.isError) return <span className="error">{mutation.error?.message}</span>;
  if (mutation.isSuccess) return <span className="success">Saved.</span>;
  return null;
}

function JiraSettingsForm({ settings, hidden }: { settings: Settings; hidden: boolean }) {
  const queryClient = useQueryClient();
  const { base_url, pat } = settings.jira;
  const [baseUrl, setBaseUrl] = useState(base_url.value ?? "");
  // Empty means "keep the stored PAT"; the stored value is never sent to the browser.
  const [newPat, setNewPat] = useState("");
  const [testResult, setTestResult] = useState<JiraTestResult | null>(null);

  useEffect(() => setBaseUrl(base_url.value ?? ""), [base_url.value]);

  const save = useMutation({
    mutationFn: api.updateSettings,
    onSuccess: (data) => {
      queryClient.setQueryData(settingsKey, data);
      setNewPat("");
    },
  });
  const test = useMutation({ mutationFn: api.testJira, onSuccess: setTestResult });

  function submit(event: FormEvent) {
    event.preventDefault();
    const jira: { base_url?: string; pat?: string } = {};
    if (base_url.source !== "env") jira.base_url = baseUrl;
    if (newPat) jira.pat = newPat;
    save.mutate({ jira });
  }

  function runTest() {
    setTestResult(null);
    test.mutate({ base_url: baseUrl || undefined, pat: newPat || undefined });
  }

  const patPlaceholder = pat.is_set ? "Set (leave empty to keep)" : "Not set";

  return (
    <Section
      id="jira"
      description="Where tickets are imported from."
      hidden={hidden}
      onSubmit={submit}
      footer={
        <>
          <div className="actions">
            <button type="submit" disabled={save.isPending}>
              Save
            </button>
            <button type="button" onClick={runTest} disabled={test.isPending}>
              {test.isPending ? "Testing…" : "Test connection"}
            </button>
            <SaveStatus mutation={save} />
          </div>
          {test.isPending && <ResultCard tone="pending" title="Connecting to Jira…" />}
          {test.isError && (
            <ResultCard tone="error" title="Connection failed">
              <p className="multiline">{test.error.message}</p>
            </ResultCard>
          )}
          {testResult?.ok && <ResultCard tone="success" title={`Connected as ${testResult.user}.`} />}
          {testResult && !testResult.ok && (
            <ResultCard tone="error" title="Connection failed">
              <p className="multiline">{testResult.error}</p>
            </ResultCard>
          )}
        </>
      }
    >
      <Setting
        category="Jira"
        label="Base URL"
        source={base_url.source}
        description={base_url.source === "env" ? "Set by AOSP_MIGRATOR_JIRA_BASE_URL." : "Root URL of the Jira server."}
      >
        {(id) => (
          <input
            id={id}
            type="url"
            value={baseUrl}
            placeholder="https://jira.example.com"
            disabled={base_url.source === "env"}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
        )}
      </Setting>
      <Setting
        category="Jira"
        label="Personal access token"
        source={pat.source}
        description={pat.source === "env" ? "Set by AOSP_MIGRATOR_JIRA_PAT." : "Stored on the server and never shown again."}
      >
        {(id) => (
          <div className="setting-row">
            <input
              id={id}
              type="password"
              value={newPat}
              placeholder={patPlaceholder}
              disabled={pat.source === "env"}
              autoComplete="off"
              onChange={(e) => setNewPat(e.target.value)}
            />
            {pat.source === "file" && (
              <button type="button" onClick={() => save.mutate({ jira: { pat: "" } })}>
                Clear token
              </button>
            )}
          </div>
        )}
      </Setting>
    </Section>
  );
}

function PrescanSettingsForm({ settings, hidden }: { settings: Settings; hidden: boolean }) {
  const queryClient = useQueryClient();
  const { upstream_domains } = settings.prescan;
  // While unset the defaults are in effect; they show as the placeholder so saving does not pin them.
  const stored = upstream_domains.source === "unset" ? "" : upstream_domains.value.join(", ");
  const [domains, setDomains] = useState(stored);

  useEffect(() => setDomains(stored), [stored]);

  const save = useMutation({
    mutationFn: api.updateSettings,
    onSuccess: (data) => queryClient.setQueryData(settingsKey, data),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate({ prescan: { upstream_domains: domains } });
  }

  return (
    <Section
      id="prescan"
      description="How the pre-scan decides a patch is already upstream."
      hidden={hidden}
      onSubmit={submit}
      footer={
        <div className="actions">
          <button type="submit" disabled={save.isPending || upstream_domains.source === "env"}>
            Save
          </button>
          <SaveStatus mutation={save} />
        </div>
      }
    >
      <Setting
        category="Pre-scan"
        label="Upstream author domains"
        source={upstream_domains.source}
        description={
          upstream_domains.source === "env"
            ? "Set by AOSP_MIGRATOR_UPSTREAM_DOMAINS."
            : "Comma-separated. A patch counts as already present by its exact subject only for authors from these domains. Leave empty for the defaults."
        }
      >
        {(id) => (
          <input
            id={id}
            value={domains}
            placeholder={upstream_domains.source === "unset" ? upstream_domains.value.join(", ") : undefined}
            disabled={upstream_domains.source === "env"}
            onChange={(e) => setDomains(e.target.value)}
          />
        )}
      </Setting>
    </Section>
  );
}

type CopilotText = "binary" | "model_resolve" | "model_port" | "model_reimpl";
type CopilotNumber = "timeout" | "item_cap" | "job_cap" | "build_rounds";

const MODEL_LABELS: [CopilotText, string, string][] = [
  ["model_resolve", "Model for resolving conflicts", "Used by rung 2 and by the test below."],
  ["model_port", "Model for porting rejected hunks", "Used by rung 3b."],
  ["model_reimpl", "Model for re-implementing", "Used by rung 3c, for moved, rewritten or gone files."],
];

const NUMBER_LABELS: [CopilotNumber, string, string][] = [
  ["timeout", "Timeout per call (s)", "Seconds one Copilot call may take."],
  ["item_cap", "Copilot calls per item", "Across rungs, build-fix rounds and retries."],
  ["job_cap", "Copilot calls per rework job", "When reached, the remaining items get only the rungs without Copilot."],
  ["build_rounds", "Build-fix rounds", "Build errors fed back to Copilot per attempt; 0 for none."],
];

/** Stored values only: while unset the default shows as the placeholder, so saving does not pin it. */
function copilotForm(settings: Settings): Record<CopilotText | CopilotNumber, string> {
  const entries = { ...settings.copilot, ...settings.rework };
  return Object.fromEntries(
    Object.entries(entries).map(([key, entry]) => [key, entry.source === "unset" || entry.value === null ? "" : String(entry.value)]),
  ) as Record<CopilotText | CopilotNumber, string>;
}

function CopilotSettingsForm({ settings, hidden }: { settings: Settings; hidden: boolean }) {
  const queryClient = useQueryClient();
  const entries: Record<CopilotText | CopilotNumber, { source: Source; value: string | number | null }> = { ...settings.copilot, ...settings.rework };
  const stored = copilotForm(settings);
  const [values, setValues] = useState(stored);
  const [testResult, setTestResult] = useState<CopilotTestResult | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);
  const started = useRef(0);
  const storedKey = JSON.stringify(stored);

  useEffect(() => setValues(stored), [storedKey]);

  const save = useMutation({
    mutationFn: api.updateSettings,
    onSuccess: (data) => queryClient.setQueryData(settingsKey, data),
  });
  const test = useMutation({
    mutationFn: api.testCopilot,
    onSuccess: setTestResult,
    onSettled: () => setElapsed((performance.now() - started.current) / 1000),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const copilot: NonNullable<CopilotSettingsUpdate["copilot"]> = {};
    const rework: NonNullable<CopilotSettingsUpdate["rework"]> = {};
    for (const key of ["binary", "model_resolve", "model_port", "model_reimpl"] as const) {
      if (entries[key].source !== "env" && values[key] !== stored[key]) copilot[key] = values[key];
    }
    for (const [key] of NUMBER_LABELS) {
      if (entries[key].source === "env" || values[key] === stored[key]) continue;
      // Empty restores the default.
      const value = values[key] === "" ? (key === "build_rounds" ? -1 : 0) : Number(values[key]);
      if (key === "timeout") copilot.timeout = value;
      else rework[key] = value;
    }
    save.mutate({ copilot, rework });
  }

  function runTest() {
    setTestResult(null);
    setElapsed(null);
    started.current = performance.now();
    test.mutate({ binary: values.binary || undefined, model: values.model_resolve || undefined });
  }

  function field(key: CopilotText | CopilotNumber, label: string, placeholder: string, hint?: string, number = false) {
    const env = entries[key].source === "env";
    return (
      <Setting key={key} category="Copilot" label={label} source={entries[key].source} description={env ? "Set by the environment." : hint}>
        {(id) => (
          <input
            id={id}
            className={number ? "input-number" : undefined}
            type={number ? "number" : "text"}
            value={values[key]}
            placeholder={placeholder}
            disabled={env}
            onChange={(e) => setValues({ ...values, [key]: e.target.value })}
          />
        )}
      </Setting>
    );
  }

  return (
    <Section
      id="copilot"
      description="The Copilot CLI used by the rework rungs, and how much of it a job may use."
      hidden={hidden}
      onSubmit={submit}
      footer={
        <>
          <div className="actions">
            <button type="submit" disabled={save.isPending}>
              Save
            </button>
            <button type="button" onClick={runTest} disabled={test.isPending}>
              {test.isPending ? "Testing…" : "Test Copilot"}
            </button>
            <SaveStatus mutation={save} />
          </div>
          <p className="hint">The test sends one short prompt with the conflict-resolving model: it uses one Copilot request.</p>
          {test.isPending && <ResultCard tone="pending" title="Sending a test prompt to Copilot…" />}
          {test.isError && (
            <ResultCard tone="error" title="Copilot test failed">
              <p className="multiline">{test.error.message}</p>
            </ResultCard>
          )}
          {testResult && <CopilotResult result={testResult} elapsed={elapsed} />}
        </>
      }
    >
      {field("binary", "Copilot CLI", settings.copilot.binary.source === "unset" ? settings.copilot.binary.value : "", "Path or command name of the standalone `copilot` CLI, logged in.")}
      {MODEL_LABELS.map(([key, label, hint]) => field(key, label, "CLI default", hint))}
      {NUMBER_LABELS.map(([key, label, hint]) => field(key, label, entries[key].source === "unset" ? String(entries[key].value) : "", hint, true))}
    </Section>
  );
}

const TOKEN_LABELS: Record<CopilotTokenCheck["status"], string> = {
  valid: "Valid",
  missing: "Missing",
  invalid: "Invalid",
  unverified: "Unverified",
};

function CopilotResult({ result, elapsed }: { result: CopilotTestResult; elapsed: number | null }) {
  const token = result.token;
  const tokenOk = !token || token.status === "valid";
  const models = modelUsage(result.usage);
  const premium = premiumRequests(result.usage);
  const took = elapsed === null ? null : `${elapsed < 10 ? elapsed.toFixed(1) : Math.round(elapsed)} s`;

  if (!result.ok) {
    return (
      <ResultCard tone="error" title="Copilot test failed" meta={took}>
        <p className="multiline">{result.error}</p>
        {token && token.message !== result.error && <TokenRow token={token} />}
      </ResultCard>
    );
  }

  // With no model set, the models in the usage file are the CLI's default.
  const served = models.map((m) => m.model).join(", ");
  return (
    <ResultCard tone={tokenOk ? "success" : "warning"} title={tokenOk ? "Copilot works" : "Copilot works, but check the token"} meta={took}>
      <dl className="result-facts">
        <div>
          <dt>CLI version</dt>
          <dd className="mono">{result.version}</dd>
        </div>
        <div>
          <dt>Model</dt>
          {result.model ? (
            <dd className="mono">{result.model}</dd>
          ) : served ? (
            <dd>
              <span className="mono">{served}</span>
              <span className="result-fact-note">CLI default</span>
            </dd>
          ) : (
            <dd title="The CLI wrote no usage file, so the model it picked is not known.">CLI default</dd>
          )}
        </div>
        {premium !== null && (
          <div>
            <dt>Premium requests</dt>
            <dd className="mono">{premium}</dd>
          </div>
        )}
      </dl>
      {models.length > 0 && (
        <ul className="result-models" aria-label="Models used">
          {models.map((m) => (
            <li key={m.model}>
              <span className="mono">{m.model}</span>
              <span className="hint">
                {[
                  m.requests !== null && `${m.requests} ${m.requests === 1 ? "call" : "calls"}`,
                  m.inputTokens !== null && `${m.inputTokens.toLocaleString()} tokens in`,
                  m.outputTokens !== null && `${m.outputTokens.toLocaleString()} out`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="result-reply">
        <span className="result-reply-label">Reply</span>
        <blockquote>{result.reply}</blockquote>
      </div>
      {token && <TokenRow token={token} />}
    </ResultCard>
  );
}

function TokenRow({ token }: { token: CopilotTokenCheck }) {
  const tone = token.status === "valid" ? "success" : token.status === "invalid" ? "error" : "warning";
  return (
    <div className="result-token">
      <span className={`result-pill ${tone}`}>
        {token.variable ?? "Token"} · {TOKEN_LABELS[token.status]}
      </span>
      <span className="result-token-message">{token.message}</span>
    </div>
  );
}

type Tone = "success" | "warning" | "error" | "pending";

function ResultCard({ tone, title, meta, children }: { tone: Tone; title: string; meta?: string | null; children?: ReactNode }) {
  return (
    <div className={`result-card ${tone}`} role="status" aria-label={title}>
      <div className="result-card-head">
        <span className="result-card-icon">{tone === "pending" ? <span className="spinner" /> : tone === "success" ? <IconCheck /> : tone === "warning" ? <IconAlert /> : <IconCross />}</span>
        <strong>{title}</strong>
        {meta && <span className="result-card-meta">{meta}</span>}
      </div>
      {children && <div className="result-card-body">{children}</div>}
    </div>
  );
}

function BuildSettingsForm({ settings, hidden }: { settings: Settings; hidden: boolean }) {
  const queryClient = useQueryClient();
  const { dependents_cap } = settings.build;
  // While unset the default shows as the placeholder, so saving does not pin it.
  const stored = dependents_cap.source === "unset" ? "" : String(dependents_cap.value);
  const [cap, setCap] = useState(stored);

  useEffect(() => setCap(stored), [stored]);

  const save = useMutation({
    mutationFn: api.updateSettings,
    onSuccess: (data) => queryClient.setQueryData(settingsKey, data),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    // Empty restores the default.
    save.mutate({ build: { dependents_cap: cap === "" ? -1 : Number(cap) } });
  }

  const env = dependents_cap.source === "env";
  return (
    <Section
      id="build"
      description="What a build of a reworked item covers."
      hidden={hidden}
      onSubmit={submit}
      footer={
        <div className="actions">
          <button type="submit" disabled={save.isPending || env}>
            Save
          </button>
          <SaveStatus mutation={save} />
        </div>
      }
    >
      <Setting
        category="Build"
        label="Dependent modules per build"
        source={dependents_cap.source}
        description={
          env
            ? "Set by AOSP_MIGRATOR_BUILD_DEPENDENTS_CAP."
            : "A change to a header, an Android.bp / .mk, an AIDL file or a public Java API also builds the modules using the changed ones, at most this many; 0 for none. Needs module-info.json."
        }
      >
        {(id) => (
          <input
            id={id}
            className="input-number"
            type="number"
            value={cap}
            placeholder={dependents_cap.source === "unset" ? String(dependents_cap.value) : ""}
            disabled={env}
            onChange={(e) => setCap(e.target.value)}
          />
        )}
      </Setting>
    </Section>
  );
}

/* Icons: 16px strokes in currentColor. */

function Svg({ children }: { children: ReactNode }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

function IconTicket() {
  return (
    <Svg>
      <path d="M2 5.5V3.75C2 3.34 2.34 3 2.75 3h10.5c.41 0 .75.34.75.75V5.5a2.5 2.5 0 0 0 0 5v1.75c0 .41-.34.75-.75.75H2.75A.75.75 0 0 1 2 12.25V10.5a2.5 2.5 0 0 0 0-5Z" />
      <path d="M10 3v10" strokeDasharray="1.5 1.5" />
    </Svg>
  );
}

function IconScan() {
  return (
    <Svg>
      <circle cx="7" cy="7" r="4.25" />
      <path d="m10.25 10.25 3.5 3.5M5.25 7l1.25 1.25L9 5.75" />
    </Svg>
  );
}

function IconSpark() {
  return (
    <Svg>
      <path d="M8 1.75 9.4 6.6l4.85 1.4L9.4 9.4 8 14.25 6.6 9.4 1.75 8 6.6 6.6Z" />
    </Svg>
  );
}

function IconHammer() {
  return (
    <Svg>
      <path d="m8.5 5.5-6 6a1.4 1.4 0 0 0 2 2l6-6" />
      <path d="m7 4 2.5-2.25 4.75 4.75L12 9Z" />
    </Svg>
  );
}

function IconSearch() {
  return (
    <Svg>
      <circle cx="7" cy="7" r="4.5" />
      <path d="m10.5 10.5 3.5 3.5" />
    </Svg>
  );
}

function IconCheck() {
  return (
    <Svg>
      <path d="m3.5 8.5 3 3 6-7" />
    </Svg>
  );
}

function IconCross() {
  return (
    <Svg>
      <path d="m4 4 8 8M12 4l-8 8" />
    </Svg>
  );
}

function IconAlert() {
  return (
    <Svg>
      <path d="M8 5.5v3.5M8 11.5v.01" />
      <path d="M7.13 2.5a1 1 0 0 1 1.74 0l5.5 9.75a1 1 0 0 1-.87 1.5H2.5a1 1 0 0 1-.87-1.5Z" />
    </Svg>
  );
}
