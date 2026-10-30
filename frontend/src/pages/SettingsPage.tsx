import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";

import { api, type CopilotSettingsUpdate, type CopilotTestResult, type JiraTestResult, type Settings, type Source } from "../api";

const settingsKey = ["settings"];

export function SettingsPage() {
  const settings = useQuery({ queryKey: settingsKey, queryFn: api.settings });

  return (
    <>
      <h1>Settings</h1>
      {settings.isPending && <p>Loading…</p>}
      {settings.isError && <p className="error">{settings.error.message}</p>}
      {settings.data && (
        <>
          <JiraSettingsForm settings={settings.data} />
          <PrescanSettingsForm settings={settings.data} />
          <CopilotSettingsForm settings={settings.data} />
        </>
      )}
    </>
  );
}

function JiraSettingsForm({ settings }: { settings: Settings }) {
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
    <form onSubmit={submit} className="form" aria-label="Jira">
      <h2>Jira</h2>
      <label>
        Base URL
        <input
          type="url"
          value={baseUrl}
          placeholder="https://jira.example.com"
          disabled={base_url.source === "env"}
          onChange={(e) => setBaseUrl(e.target.value)}
        />
      </label>
      {base_url.source === "env" && <p className="hint">Set by AOSP_MIGRATOR_JIRA_BASE_URL.</p>}

      <label>
        Personal access token
        <input
          type="password"
          value={newPat}
          placeholder={patPlaceholder}
          disabled={pat.source === "env"}
          autoComplete="off"
          onChange={(e) => setNewPat(e.target.value)}
        />
      </label>
      {pat.source === "env" && <p className="hint">Set by AOSP_MIGRATOR_JIRA_PAT.</p>}
      {pat.source === "file" && (
        <button type="button" onClick={() => save.mutate({ jira: { pat: "" } })}>
          Clear token
        </button>
      )}

      <div className="actions">
        <button type="submit" disabled={save.isPending}>
          Save
        </button>
        <button type="button" onClick={runTest} disabled={test.isPending}>
          Test connection
        </button>
      </div>

      {save.isError && <p className="error">{save.error.message}</p>}
      {save.isSuccess && <p className="success">Saved.</p>}
      {test.isError && <p className="error">{test.error.message}</p>}
      {testResult?.ok && <p className="success">Connected as {testResult.user}.</p>}
      {testResult && !testResult.ok && <p className="error">{testResult.error}</p>}
    </form>
  );
}

function PrescanSettingsForm({ settings }: { settings: Settings }) {
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
    <form onSubmit={submit} className="form" aria-label="Pre-scan">
      <h2>Pre-scan</h2>
      <label>
        Upstream author domains
        <input
          value={domains}
          placeholder={upstream_domains.source === "unset" ? upstream_domains.value.join(", ") : undefined}
          disabled={upstream_domains.source === "env"}
          onChange={(e) => setDomains(e.target.value)}
        />
      </label>
      <p className="hint">
        {upstream_domains.source === "env"
          ? "Set by AOSP_MIGRATOR_UPSTREAM_DOMAINS."
          : "Comma-separated. A patch counts as already present by its exact subject only for authors from these domains. Leave empty for the defaults."}
      </p>
      <div className="actions">
        <button type="submit" disabled={save.isPending || upstream_domains.source === "env"}>
          Save
        </button>
      </div>
      {save.isError && <p className="error">{save.error.message}</p>}
      {save.isSuccess && <p className="success">Saved.</p>}
    </form>
  );
}

type CopilotText = "binary" | "model_resolve" | "model_port" | "model_reimpl";
type CopilotNumber = "timeout" | "item_cap" | "job_cap" | "build_rounds";

const MODEL_LABELS: [CopilotText, string][] = [
  ["model_resolve", "Model for resolving conflicts"],
  ["model_port", "Model for porting rejected hunks"],
  ["model_reimpl", "Model for re-implementing"],
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

function CopilotSettingsForm({ settings }: { settings: Settings }) {
  const queryClient = useQueryClient();
  const entries: Record<CopilotText | CopilotNumber, { source: Source; value: string | number | null }> = { ...settings.copilot, ...settings.rework };
  const stored = copilotForm(settings);
  const [values, setValues] = useState(stored);
  const [testResult, setTestResult] = useState<CopilotTestResult | null>(null);
  const storedKey = JSON.stringify(stored);

  useEffect(() => setValues(stored), [storedKey]);

  const save = useMutation({
    mutationFn: api.updateSettings,
    onSuccess: (data) => queryClient.setQueryData(settingsKey, data),
  });
  const test = useMutation({ mutationFn: api.testCopilot, onSuccess: setTestResult });

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
    test.mutate({ binary: values.binary || undefined, model: values.model_resolve || undefined });
  }

  function field(key: CopilotText | CopilotNumber, label: string, placeholder: string, hint?: string, number = false) {
    const env = entries[key].source === "env";
    return (
      <div key={key}>
        <label>
          {label}
          <input
            type={number ? "number" : "text"}
            value={values[key]}
            placeholder={placeholder}
            disabled={env}
            onChange={(e) => setValues({ ...values, [key]: e.target.value })}
          />
        </label>
        {(env || hint) && <p className="hint">{env ? "Set by the environment." : hint}</p>}
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="form" aria-label="Copilot">
      <h2>Copilot</h2>
      {field("binary", "Copilot CLI", settings.copilot.binary.source === "unset" ? settings.copilot.binary.value : "", "Path or command name of the standalone `copilot` CLI, logged in.")}
      {MODEL_LABELS.map(([key, label]) => field(key, label, "CLI default"))}
      {NUMBER_LABELS.map(([key, label, hint]) => field(key, label, entries[key].source === "unset" ? String(entries[key].value) : "", hint, true))}
      <div className="actions">
        <button type="submit" disabled={save.isPending}>
          Save
        </button>
        <button type="button" onClick={runTest} disabled={test.isPending}>
          {test.isPending ? "Testing…" : "Test Copilot"}
        </button>
      </div>
      <p className="hint">The test sends one short prompt with the conflict-resolving model: it uses one Copilot request.</p>
      {save.isError && <p className="error">{save.error.message}</p>}
      {save.isSuccess && <p className="success">Saved.</p>}
      {test.isError && <p className="error">{test.error.message}</p>}
      {testResult?.ok && (
        <p className="success">
          Copilot CLI {testResult.version} answered with {testResult.model ?? "its default model"}: {testResult.reply}
        </p>
      )}
      {testResult && !testResult.ok && <p className="error">{testResult.error}</p>}
    </form>
  );
}
