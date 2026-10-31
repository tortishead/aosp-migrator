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

import { api, type JiraTestResult, type Settings, type Source } from "../api";

const settingsKey = ["settings"];

type SectionId = "jira" | "prescan";

const SECTIONS: { id: SectionId; title: string; summary: string; icon: ReactNode }[] = [
  { id: "jira", title: "Jira", summary: "Server and token", icon: <IconTicket /> },
  { id: "prescan", title: "Pre-scan", summary: "Upstream detection", icon: <IconScan /> },
];

/** The search box text, lower-cased; settings that do not match it hide. */
const SearchContext = createContext("");

export function SettingsPage() {
  const settings = useQuery({ queryKey: settingsKey, queryFn: api.settings });
  const [query, setQuery] = useState("");
  const [active, setActive] = useState<SectionId>("jira");
  const [visible, setVisible] = useState<Record<SectionId, boolean>>({ jira: true, prescan: true });
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
