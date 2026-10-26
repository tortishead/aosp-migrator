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
| MS1 | Import + web UI (workspaces, backlog, item detail, settings, manual clustering) | Planned (below) |
| MS2 | Pre-scan (repo mapping, already-present check, Copilot evaluation, cluster suggestions) | Planned (below) |
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
- From the MS2 grilling: `git` / `plain` target trees, cheap-model-first Copilot tier, auto pre-scan after import, fetch/mirror cache for history lookups.

---

## MS1 — Import + web UI

### Decisions

- **Toolchain:** Python 3.12 (uv), Node >= 20 (pnpm; machine has Node 26), Vitest + Testing Library. Distributed as a wheel that bundles the built frontend.
- **Runtime:** Python (FastAPI) backend, React + TypeScript (Vite) frontend, SQLite. Single user, runs on the build machine. One command, `aosp-migrator serve`, serves the API and the prebuilt frontend bundle. No Docker.
- **Network:** binds `127.0.0.1` by default. Optional `--lan` flag binds `0.0.0.0` and requires a shared-token login.
- **Data dir:** `~/.aosp-migrator/` (overridable by `--data-dir` / `AOSP_MIGRATOR_DATA_DIR`). Holds `config.toml`, `db.sqlite`, `workspaces/<id>/attachments/`.
- **Secrets:** global Jira PAT (GitHub PAT field reserved for later) stored in `config.toml` with `0600` permissions. Env vars override the file. The API never returns secret values, only "set / not set". Settings has a test-connection button.
- **Jira:** Data Center, REST API v2, PAT as `Authorization: Bearer`. Read-only. Client sits behind an interface so a Cloud adapter can be added later.
- **Workspaces:** many per instance. Each has name, JQL, target tree path (required), source tree path (optional), relevant link types (optional). All editable. Path check: must exist and be a directory. `repo` is optional: tree type is detected and stored (`repo` if `.repo/` exists, `git` if `.git` exists, otherwise `plain`) for later milestones.
- **Imported per ticket:** key, summary, description (raw + `renderedFields` HTML), Jira status, labels, components, fix versions, issue links, updated, comments (rendered HTML).
- **Custom fields:** not imported. Standard fields only.
- **Linked tickets:** Jira issue links only (no remote links / web links). Followed one level deep. A workspace can limit which link types are followed: each entry matches the link type name (`Relates`) or direction (`relates to`, `is blocked by`), case-insensitive; empty follows all. Others are not fetched or linked (still listed in `jira_issue.issue_links`); a change applies on the next import. Stored with the same fields and attachments, shown inside the item, not added to the backlog. One `jira_issue` row per key per workspace: if a linked ticket is also a backlog item, both share that row.
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

- `workspace` — id, name, jql, target_tree, target_tree_type, source_tree, source_tree_type, link_types (JSON list), created_at, updated_at
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

**1.4 Fake Jira and Jira client** — done
- Fake Jira DC app serving fixtures: search with `startAt`/`maxResults`, `expand=renderedFields`, issue get, comments, issue links, attachment download, auth check.
- DC client behind an interface: paginated search, issue fetch, attachment download, retry on 429/5xx.
- Test: pagination over multiple pages, auth failure, retry, rendered fields present.
- As built: `aosp_migrator/jira/` has `base.py` (`JiraClient` protocol, normalized dataclasses `Issue` / `AttachmentMeta` / `IssueLink` / `Comment` / `SearchPage`, errors `JiraAuthError` / `JiraNotFoundError` / `JiraConnectionError`), `datacenter.py` (`DataCenterClient`) and `client_from_config`. Search is `POST /rest/api/2/search` with `expand=renderedFields` and advances by the number of issues returned (server may cap `maxResults`); comments come from `/issue/{key}/comment?expand=renderedBody`, paginated. Retries 429/500/502/503/504 and transport errors, 4 attempts, backoff 1/2/4 s, honors `Retry-After` (cap 60 s); `sleep` is injectable. `download_attachment(url, dest)` resolves relative URLs (as in rendered HTML) against the base URL, refuses other hosts so the PAT never leaves Jira, writes via `.part` + rename, returns sha256. Settings test-connection now uses the client with one attempt. Fake Jira in `backend/tests/fake_jira/`: FastAPI app over a mutable in-memory `FakeJiraStore` (no JQL parsing: search returns issues with `in_jql=True`; `fail_next` injects statuses; `requests` logs calls; `max_page_size` caps pages); fixtures PROJ-101..125 in JQL plus linked PROJ-201, patch files in `fake_jira/files/` (format-patch v1/v2, plain multi-file with new file and rename, diff named `.txt`, non-diff `.txt`, binary patch). Tests use one session-scoped real uvicorn server (`fake_jira_server`) and a fresh store per test (`fake_jira`, `jira_url` fixtures). Standalone: `cd backend && PYTHONPATH=tests uv run python -m fake_jira --port 8090` (PAT `fake-jira-pat`).

**1.5 Attachment processing** — done
- Classify (extension + sniff), parse diffs (format-patch, plain, multi-file, renames, binary hunks), download embedded images, rewrite image URLs in rendered HTML.
- Test: fixture patches of each kind; sniff catches a diff named `.txt`; non-diff text not misclassified; rewritten HTML points to local URLs.
- As built: `aosp_migrator/importer/` with three pure-ish modules, no DB yet (tables come with 1.6). `diffparse.py`: `looks_like_diff` (first 50 lines: `From <40-hex>` on line 1, `diff --git`, or `---`/`+++`/`@@` on consecutive lines) and `parse_diff` -> `ParsedDiff(format, files=[FileChange(path, old_path, status, added, removed, binary)])`; counts follow hunk header ranges (so the `-- ` signature is not a removal), handles patch series, plain diffs without git headers (timestamps, missing `a/`/`b/`), C-quoted and spaced paths, mode-only changes, copies, `GIT binary patch` and `Binary files ... differ`; `old_path` only set for renames/copies. `attachments.py`: `classify(meta)` -> `diff` (`.diff`/`.patch` or `text/x-diff|x-patch` mime) / `image` (image mime or extension) / `sniff` (`.txt`/`.log`/no extension, text or octet-stream mime, <= 10 MiB) / `other`; `process_attachments(client, metas, embedded_image_ids, attachments_dir)` downloads diffs, sniff candidates (first 64 KiB sniffed, non-diffs deleted again) and only embedded images to `<dir>/<jira id>/<basename>`, reuses files already on disk (Jira content per id is immutable), returns `ProcessedAttachment(meta, kind, local_path, sha256, parsed)`. `html.py`: `attachment_id(url, base_url)` matches `/secure/attachment|thumbnail/<id>/` on the Jira origin and context path only; `embedded_image_ids(bodies, base_url)` reads `<img src>` only; `rewrite_attachment_urls(body, base_url, {jira_id: local_url})` rewrites `src`/`href` (thumbnail and its wrapping link both go to the full image) and leaves everything else byte-identical. Local URL scheme is the caller's choice (1.6/1.8).

**1.6 Import job** — done
- Background runner, progress, cancel, per-ticket errors, one job per workspace.
- Upsert rules, dropped flag, attachment versions, linked tickets one level deep, history events, single-ticket reimport.
- API: start import (all / single key), job status, cancel.
- Test: first import; reimport with changed summary keeps local state and notes; ticket leaves JQL and is flagged dropped; ticket returns and is unflagged; new attachment version; one ticket fails and job completes; cancel mid-job; second job on same workspace rejected.
- As built: migration `0002` adds all remaining MS1 tables, every FK `ON DELETE CASCADE` (workspace delete removes everything plus `<data>/workspaces/<id>/`; refused with 409 while an import runs). Deviations from the data model: `import_job.scope` is `all` / `keys` with `issue_keys` (JSON list, so 1.7 bulk reimport is one job) instead of `single` + `issue_key`; `import_job.error` holds why a whole job failed; `attachment.url` keeps the Jira URL; `item_link` has `direction` and `description` ("is blocked by"). `importer/store.py`: `fetch_issue` (network only: comments, attachments, HTML rewrite) and `store_issue` (one transaction: upsert issue, comments, attachments, `is_current` = newest `created` per filename; returns changes). `importer/runner.py`: `ImportRunner` on `app.state.import_runner`, daemon thread per job (`executor` and `client_factory` injectable; `create_app(jira_client_factory=...)`), lock + DB check for one running job per workspace. Job statuses `running` / `done` / `failed` / `canceled`; jobs still `running` at startup become `failed` ("Interrupted by a server restart"). Cancel is checked before each ticket. Per-ticket errors go to `job.errors` `[{key, message}]` and `item.last_import_error`; a new ticket that fails still gets an item (fields from search, no comments/attachments); `JiraAuthError` / `JiraConnectionError` fail the whole job. Linked tickets are fetched once per job; a failing linked ticket is reported as `Linked from <key>: ...` and does not fail the item. Dropped is only computed after a complete `all` run (never on cancel/failure); seen again in the JQL restores it; `keys` reimport leaves the flag alone (JQL membership is not checked). History event types: `imported`, `updated` (payload `fields: {name: {old, new}}` for summary/description_raw/status/labels/components/fix_versions/issue_links, plus `comments_added|edited|removed`, `attachments_added|removed`), `dropped`, `restored`, `import_failed`; every payload has `job_id`. Changes to linked-only tickets are not in any history. API: `POST /api/workspaces/{id}/import` `{keys?}` (202; 422 without Jira settings, 404 for keys not in the backlog, 409 if running), `GET /api/workspaces/{id}/import-jobs` (newest first), `GET /api/import-jobs/{id}`, `POST /api/import-jobs/{id}/cancel`. Downloaded attachments are served at `/api/workspaces/{id}/attachments/{jira_id}/{filename}` (with `CSP: sandbox`, `nosniff`); rewritten HTML uses these URLs. Fake Jira gained `fail_paths` (path -> status on every request).

**1.7 Backlog UI** — done
- Items API with filters and pagination, free-text search via SQLite FTS5 (key, summary, description, diff paths), facet values for filter dropdowns.
- Backlog table, filters in URL, import button with live progress and error list, multi-select bulk actions (state change, reimport).
- Test: API filter combinations; component tests for filter-to-URL round trip and bulk action calls.
- As built: migration `0003` adds FTS5 table `issue_fts` (key, summary, raw description, diff paths incl. rename sources; rowid = `jira_issue.id`, `trigram` tokenizer for substring matches), kept in sync by triggers on `jira_issue` and `attachment`; Alembic autogenerate and the drift test skip it via `db.include_name`. Search terms (whitespace split) combine with AND, are quoted (FTS syntax taken literally), terms under 3 chars fall back to `LIKE`; only the item's own issue is searched, not linked tickets. API: `GET /api/workspaces/{id}/items` with `q`, repeated `state` / `status` / `label` / `component` / `fix_version` (OR within one filter), `dropped`, `has_diff`, `sort` (`key` natural order, `updated`, `status`, `state`, `-` prefix for desc), `offset` / `limit` (max 500) -> `{items, total, offset, limit}`, each item with `diff_count` (current diffs); `GET .../items/facets` (statuses, labels, components, fix versions of backlog items only); `POST .../items/state` `{keys, state}` -> `{changed}` (404 if any key is not a backlog item, nothing changed then; history `state_changed` `{old, new}` only for real changes). UI: URL params = API params plus `page` (50 per page); search debounced 300 ms with `replace`; multi-filters as checkbox dropdowns; selection is per page and cleared on any URL change; import panel polls the newest job every second while running and refreshes items and facets on each progress step; heading uses the workspace name from the list. `mockApi` keys without a query string also match requests with one; `renderRoute` returns the router.

**1.8 Item detail UI** — done
- Item detail API (issue, comments, links, attachments, notes, history).
- Rendered Jira HTML (sanitized, styled tables/code/panels), comments, linked tickets, diff viewer, other-attachments list, history timeline, notes, state change, single reimport, Jira link.
- Test: sanitizer strips scripts but keeps tables; diff viewer renders fixture patch in unified and split mode; notes append and show in history.
- As built: `GET /api/workspaces/{id}/items/{key}` -> item fields, `jira_base_url`, `issue` (fields, `browse_url`, comments, attachments with `jira_url` / `local_url` / `is_current` / `parsed`, oldest first), `links` (link fields, `item_id` if the linked ticket is also a backlog item, full linked `issue`), `notes`, `history` (oldest first); 404 for keys that are not backlog items (linked-only included). `POST .../items/{key}/notes` `{text}` (201, stripped, 422 if empty) also writes history `note_added` `{note_id, text}`. State change and single reimport reuse `POST .../items/state` and `POST .../import` `{keys: [key]}`; the page polls `GET /api/import-jobs/{id}` and reloads when done. `gitdiff-parser` (inside `react-diff-view`) cannot read format-patch or plain diffs without git headers, so `GET /api/workspaces/{id}/diffs/{jira_id}` returns the server's `parse_diff` with raw hunk text per file (`FileChange.hunks`, excluded from the stored `parsed` JSON); the frontend only feeds hunk bodies to `parseDiff`, path/status/binary come from the server. Highlighting via `refractor` 5 wrapped to return `.children` (react-diff-view expects refractor 3); `.h` is C++, `.bp` uses JavaScript, `.mk`/`Makefile` makefile. View type in `localStorage` (`aosp-migrator.diffViewType`). DOMPurify on its own instance: forbids style/form/input/button/textarea/select, site-relative URLs go to Jira (`/api/` kept local, dropped when no base URL), links open in a new tab. Linked tickets render inside collapsible `<details>` with their own diffs, attachments and comments. Item route is lazy-loaded (own chunk). Diff CSS: Prism token classes collide with app classes (`.tag`, a card class named `comment`), so Jira comment cards are `.jira-comment` and `.diff-code .token.tag` resets the pill style.
- As built (later): `parse_diff` also keeps the mail header of each format-patch commit as `commits` (`sha`, `author`, `date` ISO, `subject` as written, `[PATCH ...]` tag included, `message` up to `---`; folded and RFC 2047 headers decoded). The diff viewer shows it above the files; backlog items carry `diff_subjects` (distinct subjects of current diffs) shown under the summary. Diffs stored before this have no `commits` until reimported.

**1.9 End-to-end and acceptance** — smoke test done, manual acceptance pending
- Playwright smoke: create workspace, set Jira settings against fake Jira, import, filter, open item, view diff, add note, mark not relevant, reimport, state kept.
- Manual acceptance by user against real Jira DC with a real JQL.
- As built: `e2e/` is its own pnpm package (`@playwright/test`, Chromium only, one worker). `playwright.config.ts` starts the fake Jira on 8791 and `aosp-migrator serve` on 8792 with a fresh `e2e/.data/` (data dir + empty target tree; gitignored), with `AOSP_MIGRATOR_JIRA_*` unset so the UI settings apply; the app serves the prebuilt bundle, so `make test-e2e` runs `pnpm build` first. `make test` now includes `test-e2e`; `make install` installs the e2e deps and Chromium. One test, `tests/smoke.spec.ts`, walks the whole flow and also checks the label facet, diff-path search, the description table/code block, unified/split diff, note and state in history, and the state filter on the backlog after reimport. Filter checkboxes follow the URL, so the test clicks and then asserts `toBeChecked` instead of `check()`.

**1.10 Manual clustering** — done
- A cluster is a manual, local group of backlog items that later milestones migrate together. Import and reimport never create or change clusters (like notes and state).
- Rules: an item is in at most one cluster; members are backlog items only (not linked-only tickets); a cluster has at least 2 members, so removing the second-to-last member or moving it away dissolves the cluster after a confirm; members are ordered (default: oldest current diff `created`, then key, items without a diff last; reorder with up/down); not_relevant / closed / dropped members stay and get a warning badge. Moving an item to another cluster removes it from the old one.
- Cluster fields: name (default: first member's key + summary), description, append-only notes, history (created, renamed, description changed, members added/removed, reordered, note added). Item history gets `cluster_joined` / `cluster_left`.
- Data: migration `0005` adds `cluster` (id, workspace_id, name, description, created_at, updated_at), `cluster_member` (cluster_id, item_id unique, position), `cluster_note`, `cluster_event`, all `ON DELETE CASCADE`.
- API: `GET/POST /api/workspaces/{id}/clusters`, `GET/PATCH/DELETE .../clusters/{cid}` (DELETE dissolves, items stay), `POST .../clusters/{cid}/members` `{keys}`, `DELETE .../clusters/{cid}/members/{key}`, `PUT .../clusters/{cid}/order` `{keys}`, `POST .../clusters/{cid}/notes`. A change that would leave another cluster with one member answers 409 unless confirmed. Cluster detail has members and a combined changed-file list with `touched_by` keys. Items API: `cluster` (id, name) per item, filter `cluster` = `any` / `none` / id. Item detail: its cluster with siblings; each link says the linked item's cluster.
- UI: backlog cluster column and filter, bulk "New cluster from selected" and "Add to cluster…"; Clusters page; cluster page (rename, description, ordered members with state and diff count, warning badges, remove, set state for all, combined files with overlaps highlighted, notes, history, dissolve); item page cluster badge with siblings, remove from cluster, "Add to my cluster" on linked backlog items.
- Test: exclusivity on move, dissolve on fewer than 2 members (409 without confirm), reorder validation, reimport leaves clusters alone, workspace delete cascade, items filter, file overlap; component tests for bulk actions, cluster page and item badge; Playwright smoke creates a cluster from 2 items, reorders it and checks the item badge.
- As built: `api/clusters.py`; `natural_key` and `ClusterRef` live in `api/items.py`. Item history: `cluster_joined` `{cluster_id, name}`, `cluster_left` `{cluster_id, name}` plus `moved_to` (name) or `dissolved: true`. Cluster history: `created` `{keys}`, `renamed` / `description_changed` `{old, new}`, `members_added` `{keys}`, `member_removed` `{key, moved_to?}`, `reordered` `{keys}`, `note_added`. Dissolving deletes the cluster with its notes and history; the item histories keep the trace. Confirmation is `confirm_dissolve` (body for create / add members, query param for member removal); the frontend `confirmingDissolve` asks with `window.confirm` on a 409 and retries. Added members are appended in default order; members already in the target cluster are ignored. Combined files come from current diffs' parsed paths incl. rename sources. The items list filter uses an aliased `cluster_member` (the list query also outer-joins it). Routes `/workspaces/:id/clusters` and `/workspaces/:id/clusters/:clusterId`; `HistoryTimeline` takes an optional `describe` for cluster events.

---

## MS2 — Pre-scan

### Decisions

- **Goal:** per backlog item, find the affected repo(s), decide whether the patch is already present, still required and makes sense, and suggest dependencies/clusters. Deterministic first; Copilot only where judgment is needed (spec: save tokens).
- **Trigger:** user-started background job for the whole workspace, the filtered selection, or one item. Same mechanics as import (progress polling, cancel, one job per workspace, a failing item records an error and does not abort). Not run automatically after import.
- **Order:** deterministic stages (repo mapping, already-present, candidate graph) run for the whole selection first, then Copilot works through the items that still need it. An item proven present skips Copilot. Copilot calls sequential; parallelism setting (max 4) later.
- **Trees:** `repo` target trees: projects come from `.repo/project.list`; missing file fails the job with a hint to run `repo sync`. A `git` or `plain` tree has no project list: its projects are the directories holding a `.git` (nested ones included, `.` for the root; hidden dirs, symlinks and root `out/` skipped), found in job stage `projects`; with none, the tree is one project `.`. A diff with only new files maps to the only project of a single-project tree. A `plain` tree has no history, so HEAD is null and history lookups give `unknown` ("history unavailable"). Git never discovers a repository above the scanned directory.
- **Never mutate user trees:** no fetch, no deepen, no checkout. Read-only git commands only.
- **Repo mapping (per diff, not per item):**
  - Hint: paths in the description (`{VAR}/…`, `${VAR}/…`, `$VAR/…`, bare `frameworks/base/...`), longest project-list prefix wins. Built-in pattern, no per-workspace regex.
  - Check: a project matches when the diff's touched files (non-new) exist under it. Hint is verified the same way.
  - Exactly one match: mapped. Zero or several: Copilot asked (`repo` field in the verdict), then manual picker as last resort.
  - A ticket may map to several repos.
- **Already present (per diff: `present` / `not_present` / `partial` / `unknown`):**
  - Every diff: `git apply --reverse --check` in the mapped target project; per-file results give `partial`.
  - Fallback only when reverse-apply fails and the format-patch `From:` domain is in the global upstream list (default `google.com`, `android.com`): look up existing history by Change-Id, then subject, then `git patch-id`. Shallow history means `unknown` with reason "history unavailable".
  - Item is present only when all its diffs are present. `partial` is flagged and still goes to Copilot.
- **Candidate graph (workspace-wide, deterministic):**
  - Same project: overlapping files or hunks; one diff touches lines another adds.
  - Any project: Jira links between backlog items, same `[PATCH n/m]` series, symbol match (identifier added in one diff — method/class, AIDL/HIDL interface, resource name, sysprop, permission string — appears in another item's diff; text search over imported diffs).
  - Copilot gets each item's candidate list (key, summary, edge reason, overlapping files) and confirms or rejects each edge.
- **Copilot adapter:**
  - Standalone `copilot` CLI in non-interactive prompt mode, behind an interface `evaluate(bundle) -> Verdict`. Binary path and model in Settings, "test Copilot" button. Missing or unauthenticated CLI fails the job up front. Exact flags/output format verified in step 2.5.
  - One configurable model. One call per item (all its diffs/repos in one bundle).
  - No tools by default. When the verdict says "insufficient context", an opt-in setting allows one retry with read-only tree access.
  - Invalid JSON or CLI error: one retry with a "valid JSON only" nudge, then item error.
- **Context bundle:** built deterministically, fixed priority, cap ~8k tokens (configurable), dropped from the bottom:
  1. migration ticket summary + description
  2. current diff(s)
  3. target code ±20 lines around each hunk, or "file missing in target"
  4. deterministic findings (repo mapping, already-present results, candidate edges)
  5. linked implementation ticket text (description + last few comments; already imported via links)
  6. source commit message (source tree first by Change-Id/subject/patch-id; GitHub only when the ticket has a commit/PR URL and the GitHub PAT is set)
  7. PR discussion (off by default)
  Content sent once (ticket diff and source commit are not both sent). Bundle stored with the verdict and shown in the UI.
- **Verdict (strict JSON per item):** `makes_sense`, `still_required`, `already_present` each `{value: yes|no|unsure, reason}`; `repo` (only when asked); `depends_on: [{key, reason}]` (confirmed edges); `confidence`; `summary` (≤3 sentences on what the patch does, reused by MS5).
- **Advisory only:** pre-scan never changes local state or clusters. User accepts or overrides per item; accepting "already present" / "not required" goes through the existing state flow (`not_relevant` / `closed`), logged in item history.
- **Cluster suggestions:** connected components of confirmed edges. Shown as suggestions; accept creates/extends an MS1 cluster, dismiss is remembered across re-runs.
- **Staleness:** verdict stores inputs (current diff attachment ids, bundle hash, target project HEAD SHAs). Change marks it stale (shown, filterable). No automatic re-run.
- **Settings (global):** Copilot binary path, model, token cap, read-only escalation on/off, upstream author domains, PR discussion on/off.

### Data model (additions, sketch)

- `prescan_job` — like `import_job` (workspace_id, scope, selection, status, stage, done, total, errors, started_at, finished_at, cancel_requested)
- `diff_scan` — attachment_id, project (nullable), mapping_source (`hint` / `files` / `copilot` / `manual`), candidates (JSON), presence, presence_detail (JSON per file / history match), target_head, scanned_at
- `prescan_verdict` — item_id, verdict (JSON), bundle (text), inputs (JSON: attachment ids, bundle hash, heads), accepted (nullable: `accepted` / `overridden`), error, created_at
- `dependency_edge` — workspace_id, from_item_id, to_item_id, reason (`files` / `hunks` / `link` / `series` / `symbol`), detail (JSON), copilot (`confirmed` / `rejected` / null)
- `cluster_suggestion_dismissal` — workspace_id, member key set hash

### Steps

**2.1 Tree fixtures, project list, repo mapping** — done
- Pytest helper that builds a synthetic `repo` tree in tmp (`.repo/project.list`, several small git projects with Change-Ids). Project list reader, description path hint parser, file-match mapping per diff. Migration adds `diff_scan`.
- Test: hint parsing variants, longest prefix, unique / zero / multiple matches, multi-repo ticket, missing `project.list` error.
- As built: `aosp_migrator/prescan/mapping.py`, pure, no git calls: `read_project_list(tree)` (raises `ProjectListMissing` with the `repo sync` hint), `path_hints(text, projects)` (`{VAR}/`, `${VAR}/`, `$VAR/` or bare paths; longest whole-segment project prefix; order of appearance, deduped), `map_diff(tree, projects, files, hints) -> DiffMapping(project, source, candidates)`. Evidence = non-new files (rename/copy source path), all must exist under the project; `..` paths ignored. One verified hint wins (`hint`), else one file match (`files`), else unmapped with candidates = verified hints or all matches. A diff with only new files takes a single hint on trust. Migration `0007` adds `diff_scan` with all sketch columns (presence columns nullable until 2.2; `attachment_id` unique). Test helper `tests/repo_tree.py`: `RepoTree` (`add_project`, `commit` with Change-Id, `git`; fixed identity/dates, global git config ignored) and `default_repo_tree` (nested `frameworks/base/packages/SystemUI`, `Android.bp` in two projects).

**2.2 Already-present check** — done
- Reverse-apply per diff and per file, upstream-author history fallback (Change-Id, subject, patch-id), shallow history means `unknown`.
- Test: present, partial, not present, upstream patch found by Change-Id after drift, shallow repo gives "history unavailable", tree unchanged after scan.
- As built: `aosp_migrator/prescan/presence.py`: `check_presence(project_dir, diff_text, upstream_domains) -> Presence(status, detail)`, `combine(statuses)` for the item level (all present -> present; any present/partial -> partial; any unknown -> unknown). Each file is reverse-checked on its own patch: `FileChange.raw` (new, not stored) holds each file's section; a series' sections for one file are concatenated in series order (`git apply -R` undoes them newest first); `-p1` when `a/`/`b/` prefixes are present, else `-p0`. A file that applies neither in reverse nor forward (context drifted) is retried with `--reverse -C1` and is `present` if that applies. `git apply --include` was not usable (exit 0 when nothing matches). "Binary files differ" sections are `unknown` per file. History fallback only when not every checkable file is present and every format-patch commit's author domain is upstream (domain or subdomain): per commit `git log HEAD` by Change-Id, then exact subject (`[PATCH ...]` tag stripped), then `git patch-id --stable` against `git log -p` limited to the diff's paths. Not found in a shallow clone, or any git error during lookup (e.g. missing blobs in a partial clone), gives `unknown` "history unavailable", unless every file still applies forward cleanly (then `not_present`). Detail: `files` `{path: present|not_present|unknown}`, `history` `[{subject, match, sha}]`, `reason`. `prescan/git.py` runs every git call with `GIT_NO_LAZY_FETCH=1`, `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`; `head()`, `is_shallow()`. Upstream domains: config field `[prescan] upstream_domains` / `AOSP_MIGRATOR_UPSTREAM_DOMAINS` (comma-separated), `Config.upstream_domains()`; API/UI with 2.5. `RepoTree` gained `commit(..., author=)`, deletes via `None`, and `format_patch`.

**2.3 Candidate graph** — done
- Edges from files/hunks, Jira links, patch series, symbol match. Migration adds `dependency_edge`.
- Test: each edge kind, cross-repo symbol pair, no self edges, rebuild replaces old unconfirmed edges.
- As built: `aosp_migrator/prescan/graph.py`. Pure `build_edges(GraphItem[])` plus DB side `graph_items` / `rebuild_graph(session, workspace_id)` over non-dropped items, current downloaded diffs (re-parsed from disk for hunks) and `ItemLink`s between backlog items. `files`/`hunks` only between diffs mapped to the same project; `hunks` (instead of `files`) when hunk ranges of a shared file meet old/old, or old against the other's new side ("touches lines another adds"). `link`: `is blocked by`/`depends on`/`requires`/`is caused by` and reverses are directed, others undirected. `series`: `[PATCH n/m]`, m>1, same author email and m; later n depends on earlier; identical patch sets skipped. `symbol`: definitions on added lines (types incl. AIDL `parcelable`/HIDL `union`, method/function declarations, `#define`, `static final`/`const` constants, resource `name=`, manifest `<permission>`, sysprop and `*.permission.*` string literals), minus names also on removed lines, ≥4 chars, small stop list; matched against all hunk tokens of other items, any project; user depends on definer. Directed edges stored as from=dependent; undirected once with lower item id first and `detail.directed` False. Rebuild deletes edges with `copilot` null and refreshes detail of confirmed/rejected ones. Migration `0008`: `dependency_edge` (unique `from_item_id, to_item_id, reason`).

**2.4 Pre-scan job and API (deterministic stages)** — done
- `prescan_job` runner, start (all / selection / single), progress, cancel, item errors. Staleness inputs recorded. Fake Jira fixtures extended (`{AOSP_HOME}/...` paths, links between items, cross-repo symbol pair, `[PATCH n/m]` series).
- Test: job over fixture workspace, cancel, one job per workspace, error isolation, stale after reimport with new diff version and after target HEAD moves.
- As built: `aosp_migrator/prescan/runner.py` `PrescanRunner` (same thread/executor pattern as imports). Stage `scan` per item in its own transaction: hints from summary + description, `map_diff`, `check_presence`, `git.head` into the attachment's `diff_scan` row (mapping from `manual`/`copilot` kept on re-scan); stage `graph` rebuilds the workspace graph. Job fails up front for non-`repo` trees, missing tree or `project.list`; refused (409) while another pre-scan or an import runs; workspace delete refused while one runs; `running` jobs failed at startup. Staleness computed on read (`stale_reasons`): a current diff without a scan ("new diff version"), or a mapped project's HEAD ≠ `target_head` ("<project> HEAD moved"). API (`api/prescan.py`): `POST /workspaces/{id}/prescan {keys?}`, `GET /workspaces/{id}/prescan-jobs`, `GET /prescan-jobs/{id}`, `POST /prescan-jobs/{id}/cancel`, `GET /workspaces/{id}/items/{key}/prescan` (per current diff scan, combined presence, stale reasons, edges as `depends_on`/`depended_on_by`/`related`). Migration `0009`: `prescan_job`. Fixtures: `tests/fake_jira/prescan.py` `prescan_store(target)` builds a matching `repo` tree (patches made by git in scratch projects) and reworks PROJ-101, PROJ-105..110 (outcomes listed in its docstring); `python -m fake_jira --prescan-tree DIR` serves it for manual tests. Minimal UI ahead of 2.8/2.9, for manual testing: backlog "Pre-scan backlog" panel (progress, cancel, item errors), "Pre-scan selected" bulk action, read-only item "Pre-scan" section with "Pre-scan this item".

**2.5 Copilot adapter and settings**
- Verify the `copilot` CLI non-interactive flags and output; adapter with fake implementation for tests; Settings fields and "test Copilot" button.
- Test: fake adapter contract, missing binary error, settings round-trip; optional live smoke test (skipped by default).

**2.6 Context bundle**
- Builder with priority order and token cap, target excerpts around hunks, linked implementation ticket text, source commit lookup (source tree, then GitHub when URL + PAT).
- Test: drop order under a small cap, dedupe of diff vs source commit, file missing in target, source commit found by Change-Id, GitHub path against a fake.

**2.7 Copilot stage and verdicts**
- Job runs Copilot after deterministic stages for items that need it; strict JSON parse, one retry, item error; optional read-only escalation; edges confirmed/rejected; migration adds `prescan_verdict`.
- Test: present items skipped, invalid JSON retry then error, ambiguous repo resolved via verdict, edge confirmation, verdict stored with bundle and inputs.

**2.8 Item page Pre-scan tab**
- Stage results per diff, verdict, bundle viewer, manual repo picker, accept/override (state change through existing flow, history logged), stale badge, re-run single item.
- Test: component tests for tab states, accept sets state and logs history, override kept across re-run.

**2.9 Backlog and cluster suggestions**
- Backlog columns/filters: repo, verdict, stale, pre-scan error; bulk "Pre-scan selected". Clusters page "Suggested" section: accept into cluster, dismiss (remembered).
- Test: filters, suggestion from confirmed edges, accept creates cluster, dismiss survives re-run.

**2.10 End-to-end and acceptance**
- Playwright: import, pre-scan with fake Copilot, open item, accept verdict, accept a suggested cluster.
- Manual acceptance by user against a real `repo` tree and real tickets with live Copilot.
