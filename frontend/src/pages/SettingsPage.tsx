import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";

import { api, type JiraTestResult, type Settings } from "../api";

const settingsKey = ["settings"];

export function SettingsPage() {
  const settings = useQuery({ queryKey: settingsKey, queryFn: api.settings });

  return (
    <>
      <h1>Settings</h1>
      {settings.isPending && <p>Loading…</p>}
      {settings.isError && <p className="error">{settings.error.message}</p>}
      {settings.data && <JiraSettingsForm settings={settings.data} />}
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
    <form onSubmit={submit} className="form">
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
