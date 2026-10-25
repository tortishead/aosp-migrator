# AOSP Migrator — Implementation Plan

Source of requirements: [spec.md](spec.md).

## Working rules

- One milestone at a time. A milestone is done only when its exit criteria pass. Then start the next.
- Each milestone starts with a grilling session for its open questions, then this file gets a detailed step list for it.
- Each step inside a milestone ends with passing tests. No step starts while the previous one is red.
- `make test` runs all backend and frontend tests. It must be green at the end of every step.
- Each finished step is committed and marked "— done" below, so a fresh session can resume from this file and `git log`.

## Milestones

| MS | Workflow step | Status |
|----|---------------|--------|
| MS1 | Import + web UI (workspaces, backlog, item detail, settings) | Planned (below) |
| MS2 | Pre-processing (repo mapping, relevance, clustering, cluster UI) | Needs grilling |
| MS3 | Migration level 1 (plain patch apply) | Needs grilling |
| MS4 | Migration level 2 (3-way / other merge strategies) | Needs grilling |
| MS5 | Migration level 3 (Copilot re-implementation) | Needs grilling |
| MS6 | Build and verify (affected modules, optional CTS) | Needs grilling |
| MS7 | Human review | Needs grilling |
| MS8 | Push (Jira comment, GitHub branch + PR) | Needs grilling |

Open question for MS3: spec says a migration is done only after build succeeds. Consider pulling MS6 forward to directly after MS3, so level 1 is testable end to end.

Parked questions (from the MS1 grilling), to settle when their milestone starts:

- How Copilot is invoked (Copilot CLI behind an adapter recommended). MS2/MS5.
- Token budget split: deterministic first, LLM only where needed. MS2/MS5.
- Patch handling across multiple repos in one ticket. MS2/MS3.
- Push target: GitHub per-repo branch + PR, remote mapping per repo, Gerrit out of scope. MS8.
- Reimport brings a new diff version after migration has started: invalidate, flag, or ignore? MS3.
- Backlog "start migration level" action from spec Module 1: lands in MS3, not MS1.

---

## MS1 — Import + web UI

### Decisions

- **Toolchain:** Python 3.12 (uv), Node >= 20 (pnpm; machine has Node 26), Vitest + Testing Library. Distributed as a wheel that bundles the built frontend.
- **Runtime:** Python (FastAPI) backend, React + TypeScript (Vite) frontend, SQLite. Single user, runs on the build machine. One command, `aosp-migrator serve`, serves the API and the prebuilt frontend bundle. No Docker.
- **Network:** binds `127.0.0.1` by default. Optional `--lan` flag binds `0.0.0.0` and requires a shared-token login.
- **Data dir:** `~/.aosp-migrator/` (overridable by `--data-dir` / `AOSP_MIGRATOR_DATA_DIR`). Holds `config.toml`, `db.sqlite`, `workspaces/<id>/attachments/`.
- **Secrets:** global Jira PAT (GitHub PAT field reserved for later) stored in `config.toml` with `0600` permissions. Env vars override the file. The API never returns secret values, only "set / not set". Settings has a test-connection button.
- **Jira:** Data Center, REST API v2, PAT as `Authorization: Bearer`. Read-only. Client sits behind an interface so a Cloud adapter can be added later.
- **Workspaces:** many per instance. Each has name, JQL, target tree path (required), source tree path (optional). All editable. Path check: must exist and be a directory. `repo` is optional: tree type is detected and stored (`repo` if `.repo/` exists, `git` if `.git` exists, otherwise `plain`) for later milestones.
- **Imported per ticket:** key, summary, description (raw + `renderedFields` HTML), Jira status, labels, components, fix versions, issue links, updated, comments (rendered HTML).
- **Custom fields:** not imported. Standard fields only.
- **Linked tickets:** Jira issue links only (no remote links / web links). Followed one level deep. Stored with the same fields and attachments, shown inside the item, not added to the backlog. One `jira_issue` row per key per workspace: if a linked ticket is also a backlog item, both share that row.
- **Attachments:**
  - Diffs come from attachments only. Diffs pasted into description or comments are not detected.
  - Downloaded: diff/patch files (extension `.diff`/`.patch`, or content sniff: first lines match `diff --git`, `---`/`+++`, or `From <sha>`), and images embedded in description/comments.
  - Not downloaded: everything else, including archives (`.zip`, `.tar.gz`). Listed in the UI with name, size and link to Jira.
  - Diffs are parsed at import: file list, added/removed lines per file, format (`format-patch` vs plain). No repo mapping (that is MS2).
  - Embedded image URLs in rendered HTML are rewritten to the local copies.
  - Versions: attachments with the same filename are versions of one diff; different filenames are separate diffs. All versions kept; newest (by Jira `created`) marked current.
- **Import job:** background job, paginated Jira search, progress via polling, cancelable, one job per workspace at a time. A failing ticket records an error and does not abort the job.
- **Reimport:** whole workspace or single ticket. Upserts Jira fields, never touches local state. Tickets no longer matched by the JQL are flagged "dropped", not deleted. Every change is written to the item history.
- **Local state:** `new` / `not_relevant` / `closed`, reversible, each change logged. Notes are append-only with timestamps.
- **Backlog:** filters for local state, Jira status, labels, components, fix version, dropped flag, has-diff, and free text (key, summary, description, diff file paths). Filters combine with AND and live in the URL. Bulk actions: change state, reimport selected.
- **Item detail:** sanitized (DOMPurify) and styled Jira HTML with proper tables and code blocks; comments; linked tickets; GitHub-style diff viewer (`react-diff-view`, unified/split toggle remembered, file sidebar with +/- counts, collapsible files, syntax highlight for Java, C/C++, Kotlin, XML, `.bp`, `.mk`, one tab per diff attachment with version picker, no context expansion); other attachments list; history timeline (imports, state changes, notes); link to Jira.
- **Tests:** backend tests against a fake Jira DC server with synthetic fixtures (no real tickets available) (no live Jira in tests); frontend component tests; one Playwright end-to-end smoke test against the fake Jira.

### Data model (initial)

- `workspace` — id, name, jql, target_tree, target_tree_type, source_tree, source_tree_type, created_at, updated_at
- `jira_issue` — id, workspace_id, key (unique per workspace), summary, description_raw, description_html, status, labels, components, fix_versions, issue_links, jira_updated, fetched_at
- `item` — id, workspace_id, issue_id, local_state, dropped, last_import_error, created_at (backlog wrapper with local state)
- `item_link` — item_id, issue_id (linked ticket), link_type
- `comment` — id, issue_id, jira_id, author, created, body_html
- `attachment` — id, issue_id, jira_id, filename, size, mime, kind (`diff` / `image` / `other`), local_path, sha256, is_current, created, parsed (JSON: format, files with path/added/removed)
- `note` — id, item_id, text, created_at
- `history_event` — id, item_id, type, payload (JSON), created_at
- `import_job` — id, workspace_id, scope (`all` / `single`), issue_key, status, done, total, errors (JSON), started_at, finished_at, cancel_requested

Schema migrations via Alembic from day one, because later milestones add tables.

### Layout

```
backend/aosp_migrator/
  cli.py            # `aosp-migrator serve`
  config.py         # data dir, config.toml, env override
  db/               # SQLAlchemy models, Alembic migrations
  jira/             # client interface + Data Center client
  importer/         # job runner, upsert, attachment classify/sniff/parse, HTML rewrite
  api/              # FastAPI routers
backend/tests/
  fake_jira/        # fake Jira DC app + fixtures
frontend/           # Vite + React + TS, TanStack Query, React Router
e2e/                # Playwright smoke test
Makefile            # dev, build, test
```

### Steps

Each step lists what is built and what proves it works.

**1.1 Scaffold** — done
- Python package (uv), FastAPI app with `/api/health`, `aosp-migrator serve` serving the built frontend, Vite React app with routing shell, Makefile (`dev`, `build`, `test`).
- Test: pytest hits `/api/health`; `serve` returns `index.html` for `/` and for a deep link; `make test` green.

**1.2 Config, settings, access** — done
- Data dir resolution, `config.toml` read/write with `0600`, env override, settings API (secrets as set/not set), `--lan` flag with token login.
- Settings page: Jira base URL, Jira PAT, test connection (`GET /rest/api/2/myself`).
- Test: file permissions, env override precedence, secret never in API responses, token required with `--lan` and not without.
- As built: env vars `AOSP_MIGRATOR_JIRA_BASE_URL`, `AOSP_MIGRATOR_JIRA_PAT`, `AOSP_MIGRATOR_GITHUB_PAT`, `AOSP_MIGRATOR_LAN_TOKEN`. LAN token is auto-generated into `[server] lan_token` on first `--lan` start; login sets an HttpOnly cookie, `Authorization: Bearer` also accepted. Cookie is not `Secure` (plain HTTP on LAN). Test connection accepts unsaved form values. Outgoing HTTP uses an injectable `httpx2` transport (`create_app(http_transport=...)`) for tests.

**1.3 Database and workspaces** — done
- SQLAlchemy models + first Alembic migration, workspace CRUD API with path validation.
- UI: workspace list, create/edit form, workspace switcher.
- Test: CRUD, path validation errors, tree type detection (`repo` / `git` / `plain`), migration runs on a fresh data dir.
- As built: migration `0001` creates only `workspace`; each later step adds its tables in its own migration. `create_app` runs `alembic upgrade head` on start; SQLite with `foreign_keys=ON` and WAL. Datetimes stored as naive UTC, returned as aware UTC (`UTCDateTime`). A test fails if models drift from migrations; new revision: `AOSP_MIGRATOR_DATA_DIR=<scratch> uv run alembic upgrade head && ... alembic revision --autogenerate` from `backend/`. Tree paths must be absolute (`~` expanded); `.git` file (worktree) counts as `git`. Workspace names unique (409). `DELETE /api/workspaces/{id}` exists (UI confirms first); cascade to items comes with 1.6. Routes `/workspaces/new` and `/workspaces/:id/edit`; header switcher navigates to the backlog.

**1.4 Fake Jira and Jira client**
- Fake Jira DC app serving fixtures: search with `startAt`/`maxResults`, `expand=renderedFields`, issue get, comments, issue links, attachment download, auth check.
- DC client behind an interface: paginated search, issue fetch, attachment download, retry on 429/5xx.
- Test: pagination over multiple pages, auth failure, retry, rendered fields present.

**1.5 Attachment processing**
- Classify (extension + sniff), parse diffs (format-patch, plain, multi-file, renames, binary hunks), download embedded images, rewrite image URLs in rendered HTML.
- Test: fixture patches of each kind; sniff catches a diff named `.txt`; non-diff text not misclassified; rewritten HTML points to local URLs.

**1.6 Import job**
- Background runner, progress, cancel, per-ticket errors, one job per workspace.
- Upsert rules, dropped flag, attachment versions, linked tickets one level deep, history events, single-ticket reimport.
- API: start import (all / single key), job status, cancel.
- Test: first import; reimport with changed summary keeps local state and notes; ticket leaves JQL and is flagged dropped; ticket returns and is unflagged; new attachment version; one ticket fails and job completes; cancel mid-job; second job on same workspace rejected.

**1.7 Backlog UI**
- Items API with filters and pagination, free-text search via SQLite FTS5 (key, summary, description, diff paths), facet values for filter dropdowns.
- Backlog table, filters in URL, import button with live progress and error list, multi-select bulk actions (state change, reimport).
- Test: API filter combinations; component tests for filter-to-URL round trip and bulk action calls.

**1.8 Item detail UI**
- Item detail API (issue, comments, links, attachments, notes, history).
- Rendered Jira HTML (sanitized, styled tables/code/panels), comments, linked tickets, diff viewer, other-attachments list, history timeline, notes, state change, single reimport, Jira link.
- Test: sanitizer strips scripts but keeps tables; diff viewer renders fixture patch in unified and split mode; notes append and show in history.

**1.9 End-to-end and acceptance**
- Playwright smoke: create workspace, set Jira settings against fake Jira, import, filter, open item, view diff, add note, mark not relevant, reimport, state kept.
- Manual acceptance by user against real Jira DC with a real JQL.

### MS1 exit criteria

- `make test` green, including the Playwright smoke test.
- User imports a real JQL from their Jira DC, checks tables and diffs render correctly, and signs off.
