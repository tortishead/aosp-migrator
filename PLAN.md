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
| MS2-1 | Pre-scan without Copilot (repo mapping, already-present check, apply check, triage labels) | Planned (below) |
| MS6 | Deterministic migration patch (levels 1 + 2) and build of affected modules | Planned ([PLAN-MS6.md](PLAN-MS6.md)), next |
| MS2-2 | Copilot evaluation on demand, dependencies, cluster suggestions | Needs grilling, after MS6 |
| MS5 | Migration level 3 (Copilot re-implementation), adds `ai` patches to the MS6 flow | Needs grilling |
| MS6-2 | Optional tests (CTS / atest) after a successful build | Needs grilling |
| MS7 | Human review | Needs grilling |
| MS8 | Push (Jira comment, GitHub branch + PR) | Needs grilling |

MS3 (level 1) and MS4 (level 2) are merged into MS6: the pre-scan apply check already does both, so MS6 keeps its result as a patch and builds it.

Parked questions (from the MS1 grilling), to settle when their milestone starts:

- How Copilot is invoked (Copilot CLI behind an adapter recommended). MS2-2/MS5.
- Token budget split: deterministic first, LLM only where needed. MS2-2/MS5.
- Push target: GitHub per-repo branch + PR, remote mapping per repo, Gerrit out of scope. MS8.
- Backlog "start migration level" action from spec Module 1: "Build selected" in MS6; level 3 in MS5.
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

Split after 2.4. MS2-1 is the deterministic pre-scan: no Copilot, no dependencies, no clustering. Every item gets a triage label from cheap git checks, so items that need no AI go straight on to migration and build. MS2-2 adds Copilot, started by hand per item or selection, and the dependency and cluster work. Its grilling happens when MS2-1 is done.

History: 2.1–2.5 were first built on branch `ms2`. Branch `ms2-1` (from `main`) took 2.1–2.4 and the git/plain tree fixes, then removed the candidate graph (`prescan/graph.py`, `dependency_edge`). The Copilot adapter (`ms2` commit `40cd8a2`, settings and `copilot.py`) waits there for MS2-2.

## MS2-1 — Pre-scan without Copilot

### Decisions

- **Goal:** per backlog item, find the affected repo(s), decide whether the patch is already present, and whether it applies as is or with a 3-way merge. Deterministic only (spec: save tokens).
- **Trigger:** user-started background job for the whole workspace, the filtered selection, or one item. Same mechanics as import (progress polling, cancel, one job per workspace, a failing item records an error and does not abort). Not run automatically after import.
- **Trees:** `repo` target trees: projects come from `.repo/project.list` plus the project lists of submanifests (`.repo/submanifests/<path>/project.list`, nested ones included; their projects prefixed with `<path>`); missing file fails the job with a hint to run `repo sync`. A `git` or `plain` tree has no project list: its projects are the directories holding a `.git` (nested ones included, `.` for the root; hidden dirs, symlinks and root `out/` skipped), found in job stage `projects`; with none, the tree is one project `.`. A diff with only new files maps to the only project of a single-project tree. A `plain` tree has no history, so HEAD is null and history lookups give `unknown` ("history unavailable"). Git never discovers a repository above the scanned directory.
- **Never mutate user trees:** no fetch, no deepen, no checkout, no writes to the index or object store. Checks that need git to write use a temp index and a temp object directory.
- **Repo mapping (per diff, not per item):**
  - Hint: paths in the description (`{VAR}/…`, `${VAR}/…`, `$VAR/…`, bare `frameworks/base/...`), longest project-list prefix wins. Built-in pattern, no per-workspace regex. Without any path hint, the description's `Repository: frameworks_opt_car_services` field is the hint (`_` → `/`, matched against the project list), so new-files-only diffs still map.
  - Check: a project matches when the diff's touched files (non-new) exist under it. Hint is verified the same way.
  - Exactly one match: mapped. Zero or several: `unmapped`, the user picks the project by hand on the item page (kept across re-runs).
  - A ticket may map to several repos.
- **Already present (per diff: `present` / `partial` / `likely_present` / `not_present` / `unknown`):**
  - First `git apply --reverse --check` per file in the mapped project. All files pass: `present`; some: `partial`.
  - When reverse-apply fails, history lookup per format-patch commit: Change-Id and `git patch-id --stable` for every author; exact subject only when the `From:` domain is in the upstream list (default `google.com`, `android.com`), since OEM subjects like "Fix crash" match too easily. A match gives `present`. Shallow history or a plain tree gives `unknown`, "history unavailable".
  - Content heuristic for context drift: when neither proves presence, a file is `likely_present` when its added lines are in the target file and its removed lines are gone (blank and trivial lines such as lone braces ignored). Shown with its evidence, never treated as proof.
- **Apply check (per diff, when not `present`):**
  - `git apply --check` against the working tree: `applies`.
  - Else `git apply --3way --cached` against HEAD in a temp `GIT_INDEX_FILE`, with `GIT_OBJECT_DIRECTORY` set to a temp dir and the real object store as alternate: clean result gives `3way`.
  - Else `conflicts`, with the failing files. Missing preimage blobs (common for OEM-only files) or a plain tree give `conflicts` with that reason.
  - Each item is checked on its own. A patch that only fails because a prerequisite is missing shows `conflicts`; dependency-ordered checks come with the dependency work in MS2-2.
- **Triage label (per item):** machine facts are labels, not local state. Local state (`new` / `not_relevant` / `closed`) stays the user's decision and the pre-scan never changes it. The item label is the worst diff result, in this order: `error`, `unmapped`, `unknown`, `conflicts`, `partial`, `3way`, `applies`, `likely_present`, `present`. An item with no diff gets no label. Later milestones add their own labels next to it (e.g. build result in MS6). MS6 builds only `applies` and `3way` items.
- **Overview:** backlog column "Pre-scan" with the label as badge (tooltip: reason, e.g. matched commit or conflicting files) and a stale marker; count bar above the backlog ("12 present · 3 partial · 30 applies · …"), a click filters by that label; filters for label and stale. Closing present items is the existing bulk state change, logged in history.
- **Staleness:** scan stores inputs (current diff attachment ids, target project HEAD SHAs). A new diff version or a moved HEAD marks the item stale (shown, filterable). No automatic re-run.
- **Settings (global):** upstream author domains.

### Data model (additions, sketch)

- `prescan_job` — like `import_job` (workspace_id, scope, selection, status, stage, done, total, errors, started_at, finished_at, cancel_requested)
- `diff_scan` — attachment_id, project (nullable), mapping_source (`hint` / `files` / `manual`), candidates (JSON), presence, presence_detail (JSON per file / history match / heuristic evidence), apply (`applies` / `3way` / `conflicts`, nullable), apply_detail (JSON), target_head, scanned_at
- `item` — triage label (nullable) and the time it was computed, so the backlog can count and filter without re-reading every scan

### Steps

**2.1 Tree fixtures, project list, repo mapping** — done
- Pytest helper that builds a synthetic `repo` tree in tmp (`.repo/project.list`, several small git projects with Change-Ids). Project list reader, description path hint parser, file-match mapping per diff. Migration adds `diff_scan`.
- Test: hint parsing variants, longest prefix, unique / zero / multiple matches, multi-repo ticket, missing `project.list` error.
- As built: `aosp_migrator/prescan/mapping.py`, pure, no git calls: `read_project_list(tree)` (raises `ProjectListMissing` with the `repo sync` hint), `path_hints(text, projects)` (`{VAR}/`, `${VAR}/`, `$VAR/` or bare paths; longest whole-segment project prefix; order of appearance, deduped), `map_diff(tree, projects, files, hints) -> DiffMapping(project, source, candidates)`. Evidence = non-new files (rename/copy source path), all must exist under the project; `..` paths ignored. One verified hint wins (`hint`), else one file match (`files`), else unmapped with candidates = verified hints or all matches. A diff with only new files takes a single hint on trust. Migration `0007` adds `diff_scan` with all sketch columns (presence columns nullable until 2.2; `attachment_id` unique). Test helper `tests/repo_tree.py`: `RepoTree` (`add_project`, `commit` with Change-Id, `git`; fixed identity/dates, global git config ignored) and `default_repo_tree` (nested `frameworks/base/packages/SystemUI`, `Android.bp` in two projects).

**2.2 Already-present check** — done
- Reverse-apply per diff and per file, upstream-author history fallback (Change-Id, subject, patch-id), shallow history means `unknown`.
- Test: present, partial, not present, upstream patch found by Change-Id after drift, shallow repo gives "history unavailable", tree unchanged after scan.
- As built: `aosp_migrator/prescan/presence.py`: `check_presence(project_dir, diff_text, upstream_domains) -> Presence(status, detail)`, `combine(statuses)` for the item level (all present -> present; any present/partial -> partial; any unknown -> unknown). Each file is reverse-checked on its own patch: `FileChange.raw` (new, not stored) holds each file's section; a series' sections for one file are concatenated in series order (`git apply -R` undoes them newest first); `-p1` when `a/`/`b/` prefixes are present, else `-p0`. `git apply --include` was not usable (exit 0 when nothing matches). "Binary files differ" sections are `unknown` per file. History fallback only when not every checkable file is present and every format-patch commit's author domain is upstream (domain or subdomain): per commit `git log HEAD` by Change-Id, then exact subject (`[PATCH ...]` tag stripped), then `git patch-id --stable` against `git log -p` limited to the diff's paths. Not found in a shallow clone, or any git error during lookup (e.g. missing blobs in a partial clone), gives `unknown` "history unavailable". Detail: `files` `{path: present|not_present|unknown}`, `history` `[{subject, match, sha}]`, `reason`. `prescan/git.py` runs every git call with `GIT_NO_LAZY_FETCH=1`, `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`; `head()`, `is_shallow()`. Upstream domains: config field `[prescan] upstream_domains` / `AOSP_MIGRATOR_UPSTREAM_DOMAINS` (comma-separated), `Config.upstream_domains()`; API/UI with 2.5. `RepoTree` gained `commit(..., author=)`, deletes via `None`, and `format_patch`.

**2.3 Candidate graph** — built, then removed
- Moved to MS2-2 with the dependency work. Code and its tests are in `ms2` commit `6f61833` (`prescan/graph.py`).

**2.4 Pre-scan job and API (deterministic stages)** — done
- `prescan_job` runner, start (all / selection / single), progress, cancel, item errors. Staleness inputs recorded. Fake Jira fixtures extended (`{AOSP_HOME}/...` paths, links between items, cross-repo symbol pair, `[PATCH n/m]` series).
- Test: job over fixture workspace, cancel, one job per workspace, error isolation, stale after reimport with new diff version and after target HEAD moves.
- As built: `aosp_migrator/prescan/runner.py` `PrescanRunner` (same thread/executor pattern as imports). Stage `scan` per item in its own transaction: hints from summary + description, `map_diff`, `check_presence`, `git.head` into the attachment's `diff_scan` row (mapping from `manual`/`copilot` kept on re-scan). Job fails up front for a missing tree, or a `repo` tree without `project.list`; refused (409) while another pre-scan or an import runs; workspace delete refused while one runs; `running` jobs failed at startup. Staleness computed on read (`stale_reasons`): a current diff without a scan ("new diff version"), or a mapped project's HEAD ≠ `target_head` ("<project> HEAD moved"). API (`api/prescan.py`): `POST /workspaces/{id}/prescan {keys?}`, `GET /workspaces/{id}/prescan-jobs`, `GET /prescan-jobs/{id}`, `POST /prescan-jobs/{id}/cancel`, `GET /workspaces/{id}/items/{key}/prescan` (per current diff scan, combined presence, stale reasons). Migration `0009`: `prescan_job`. Fixtures: `tests/fake_jira/prescan.py` `prescan_store(target)` builds a matching `repo` tree (patches made by git in scratch projects) and reworks PROJ-101, PROJ-105..110 (outcomes listed in its docstring); `python -m fake_jira --prescan-tree DIR` serves it for manual tests. Minimal UI ahead of 2.9/2.10, for manual testing: backlog "Pre-scan backlog" panel (progress, cancel, item errors), "Pre-scan selected" bulk action, read-only item "Pre-scan" section with "Pre-scan this item".

**2.5 History lookup for every author** — done
- Change-Id and patch-id lookup for every format-patch commit; subject only for upstream domains. Runs only after reverse-apply fails.
- Test: OEM patch already cherry-picked found by Change-Id, OEM patch with drifted context found by patch-id, OEM subject alone not a match, upstream subject match kept, shallow clone and plain tree `unknown`.
- As built: a diff that only adds files (every file's first change is `added`, none deleted at the end) skips the presence check: `not_present` with `reason` "new files only, presence not checked", then apply-checked as usual.
- As built: `check_presence` searches history whenever the diff has format-patch headers and not every checkable file reverse-applies; `_find_in_history(..., upstream)` skips the subject lookup for non-upstream authors. A plain diff without headers still gets no history lookup. A plain tree (no `.git`) fails `git log` and gives `unknown` "history unavailable". `history` in `presence_detail` now lists every commit, not only upstream ones. A history match still wins over the files: a commit found in history whose change was later partly reverted shows `present`.

**2.6 Likely-present heuristic** — done
- Per file: added lines present in the target file, removed lines absent, trivial lines ignored. Diff `likely_present` when every file is present or likely and at least one is only likely. Evidence in `presence_detail`.
- Test: drifted context gives `likely_present`, one added line missing gives `not_present`, removed line still there gives `not_present`, brace-only hunks do not count.
- As built: `presence._heuristic` runs on every file still `not_present` after reverse-apply and history (binary sections skipped) and marks it `likely_present` in `files`. Lines compare with whitespace runs collapsed; only lines with a letter or digit count; over a series a line added then removed (or removed then re-added, e.g. re-indented) cancels out. Added lines are looked up in the working-tree file, removed lines must be absent (a removed line that also exists elsewhere in the file counts as remaining: conservative). A file without significant lines gets no evidence and stays `not_present`. Evidence: `presence_detail.heuristic` `{path: {added: {found, total, missing[:5]}, removed: {gone, total, remaining[:5]}}}`. Diff `likely_present` when every checkable file is present or likely; it beats "history unavailable" (shallow or plain tree; the reason stays in the detail). A mix of likely and not present files stays `not_present` (no proof), present + likely + not present is `partial`. `combine`: all present -> present, all present or likely -> `likely_present`, otherwise likely counts as not present. Frontend: `Presence` type, "Likely present" label (warn tag); history lines say "Commit" instead of "Upstream" since 2.5.

**2.7 Apply check** — done
- `git apply --check`, then 3-way on temp index and temp object dir, else `conflicts`. Migration adds `apply` / `apply_detail` to `diff_scan`.
- Test: clean apply, 3-way after drift, real conflict, missing preimage blob, plain tree, user tree and object store unchanged after the scan (no new loose objects, index untouched).
- As built: `aosp_migrator/prescan/apply.py`: `check_apply(project_dir, diff_text, present) -> ApplyCheck(status, detail)`. Per file, with the same series sections as presence (`split_commits`, `file_sections`, `is_binary`, `strip_option` now public in `presence`). `git apply --check` in the working tree; failing files go through `git apply --3way --cached` against HEAD with `GIT_INDEX_FILE` (one temp index from `read-tree HEAD` for all files), `GIT_OBJECT_DIRECTORY` (temp) and `GIT_ALTERNATE_OBJECT_DIRECTORIES` (the project's real object dir, resolved). `prescan.git.git()` gained `env=`. Diff status is the worst file. Files presence found `present` are left out (they would only conflict). Conflict reasons per file in `apply_detail.errors`: "3-way merge conflicts", "preimage blob not in the target history", "no git history for a 3-way merge" (plain tree or unborn HEAD), "binary file without patch data", else git's first error line. Runner checks every mapped diff that is not `present`; `apply`/`apply_detail` null otherwise. Migration `0010`. API `DiffScanOut.apply`, `apply_detail`. Item "Pre-scan" section has an "Applies?" column (per-file results and reasons unless the diff applies). Fixture PROJ-104's binary data is not valid base85, so its png section conflicts with "corrupt binary patch".

**2.8 Item triage label and API** — done
- Label from the diff results in the fixed order, stored on `item` by the job; workspace endpoint with counts per label; backlog API returns label and stale flag and filters by both. Upstream domains in the settings API.
- Test: label order, item with mixed diffs, counts, filters, label refreshed on re-run, local state never touched.
- As built: `aosp_migrator/prescan/triage.py`: `ORDER` (worst first), `diff_triage(scan, filename) -> Triage(label, reason)`, `item_triage(diffs)` (worst label; reasons of every diff with it joined by "; "; None without diffs). Per diff: no project `unmapped`; presence `present`, `likely_present` and `unknown` stand on their own (a likely present patch would only conflict); `partial` takes the worse of itself and the apply result; `not_present` takes the apply result (none: `unknown`). Reasons are short tooltip texts naming up to 3 paths/commits/candidates ("+n more"). Migration `0011`: `item.triage_label` (indexed), `triage_reason`, `triaged_at`. Runner `triage_item` after each item's scan; an item error sets `error` with the message as reason. `stale_reasons` replaced by `stale_items(session, workspace_id, tree, item_ids=None) -> {item_id: reasons}` (HEADs read once per project). Items API: `triage_label`, `triage_reason`, `stale` (reasons) per item; filters `triage` (labels, `none` for unlabeled; unknown label 422) and `stale` (bool; computes every scanned item's staleness). `GET /workspaces/{id}/prescan-summary` -> `{labels: {every label in order: n}, unlabeled, stale}` over non-dropped items. Item prescan endpoint adds `label`, `label_reason`. Settings: `prescan.upstream_domains` `{source, value: effective list}`; PUT `{prescan: {upstream_domains: "a, b"}}` lower-cases, drops `@`, dedupes, 422 on non-domains, "" restores the defaults. Frontend: types only (`TriageLabel`, `TRIAGE_LABELS`, `PrescanSummary`, `api.prescanSummary`).

**2.9 Backlog overview** — done
- "Pre-scan" column with badge, tooltip and stale marker; count bar with click-to-filter; label and stale filters; bulk close of a filtered selection uses the existing flow. Upstream domains field in Settings.
- Test: component tests for column, count bar filter, stale marker.
- As built: `prescan/triage.ts` (`TRIAGE_TEXT`, `triageClass`, `prescanSummaryKey` under the items key, so every backlog refresh refetches the counts). Backlog column "Pre-scan": label tag (reason as tooltip; error/conflicts red, applies/present plain, the rest yellow) and a "stale" tag (reasons as tooltip). `prescan/TriageBar.tsx` above the filters: non-zero labels worst first, then "n not scanned" (`triage=none`) and "n stale" (`stale=true`); a click sets that one label filter, a second click clears it; hidden until any item has a label. Filters: "Pre-scan" multi-select (`triage`, labels + "Not scanned") and "Stale" yes/no; both URL params, cleared by "Clear filters". `useLatestPrescan` now also invalidates the items key as a job moves, so labels and counts update live. Closing present items: count bar "present", select all on the page, set state Closed (no new bulk action). Settings: "Pre-scan" form with "Upstream author domains" (defaults as placeholder while unset, so saving empty keeps them unset; locked when set by `AOSP_MIGRATOR_UPSTREAM_DOMAINS`); each settings form has its own Save.

**2.10 Item page Pre-scan section** — done
- Per diff: project and mapping source, presence with evidence (files, matched commit, heuristic lines), apply result with conflicting files; manual project picker for unmapped diffs; stale badge; re-run this item.
- Test: component tests for each label, picker saves and survives re-run.
- As built: `PrescanRunner.pick_project(workspace_id, item_id, attachment_id, project)` under the start lock (refused while a pre-scan or import runs): sets `project` + `mapping_source` `manual` (None: clears both, so the scan maps again), logs history `project_picked` `{filename, old, new}`, then re-scans and re-labels the item right away in a savepoint (a failing scan keeps the pick and labels the item `error`). API: `GET /workspaces/{id}/target-projects` (422 with the `repo sync` hint, or missing tree), `PUT /workspaces/{id}/items/{key}/diffs/{attachment_id}/project {project|null}` -> item pre-scan (404 not a current diff, 409 job running, 422 not a project of the tree). Item "Pre-scan" section: header shows the triage label and its reason instead of the combined presence; "Line evidence" list per heuristic file (found/total added, gone/total removed, missing and remaining lines); per diff a project picker (select: candidates, then all projects, loaded on demand; "Use project"), always shown while unmapped, behind "Change" when mapped, "Map automatically" for manual picks; disabled while a pre-scan runs. A pick refreshes the backlog labels and the item history ("Target project for f: p").

**2.11 End-to-end and acceptance** — Playwright done; manual acceptance pending
- Playwright: import, pre-scan, count bar filter, open item, pick a project, close present items.
- Manual acceptance by user against a real `repo` tree and real tickets.
- As built: `e2e/tests/prescan.spec.ts` against a second fake Jira (port 8793, `--prescan-tree e2e/.prescan/aosp`, rebuilt every run): settings, workspace on the `repo` tree, import, "Pre-scan backlog", count bar texts, "1 unmapped" filter, PROJ-110 picks `system/core` (label Applies, history entry), full re-run keeps the pick, "2 present" filter, select page, bulk Closed. The smoke spec no longer expects an empty workspace list (specs share one app; `prescan` runs first).

**2.12 Apply over presence, cleanup** — done
- Triage rule: apply over presence. A diff that is not `present` takes its apply result (`applies` / `3way` / `conflicts`) whenever it was apply-checked, whatever presence said (`unknown`, `partial`, `likely_present`); the presence finding is appended to the reason ("<project>: a.c conflict; b.c already in"). Only a diff without an apply result keeps its presence label. Since the runner apply-checks every mapped diff that is not present, `partial` / `likely_present` / `unknown` labels are now rare. The backlog shows the label alone (`prescan/TriageTags.tsx`); the items API `apply` field added for a second tag is gone.
- `relabel_items` at startup labels every scanned item again from its stored scans (`error` kept), so a triage rule change needs no re-scan.
- Cleanup: `prescan/patch.py` (`Patch.parse`: commits, per-file `FileSections` with `patch` and `binary`; `strip_option`) shared by presence and apply; `git.apply()`; `apply.ORDER` / `apply.worst` (items API uses them); `triage.worst`; runner `diff_scan` lookup and one error-label path; `Presence.files(result)`. Item-level combined `presence` and `presence.combine` removed (unused since 2.10: the item page shows the label).

## MS6 — Migration patch and build

Sub-plan: [PLAN-MS6.md](PLAN-MS6.md).

## Backlog: UI refinement (backlog page)

Done. Playwright specs updated but not yet run after the change.

- Done as: header shows `ImportButton`, `PrescanButton` (status line as `title`) and `JobsPopup` ("Job status" button: all three status lines as tooltip, a running / error dot, popup with `ImportPanel`, `PrescanPanel`, `BuildPanel`; Escape or backdrop closes). Status texts in `importStatusText` / `prescanStatusText` / `buildStatusText`. `TriageBar` + `BuildBar` in one `.count-bars` row. "Import error" and "Stale" filters gone (URL params still work; "Clear filters" keeps `sort` and `group`). Sort and Group in a "View" box right of the filters. Group: items API `group` (`project` from the earlier target-project grouping, plus `cluster`, `triage`, `build`, `status`, `state`) orders by it ahead of the sort (labels and build statuses worst first, none last); the table puts a collapsible heading row with its page count before each group. Summary cell links to the item. Bulk toolbar is sticky under the app header.

- **Job status off the header line:** the import / pre-scan / build panels show only their buttons ("Import from Jira", "Pre-scan backlog"; build has none). The latest job's status line moves into the button's tooltip.
- **Job status popup:** a job's full status (progress, cancel, failed items, build log tail) opens in a popup window from the header, one each for import, pre-scan and build.
- **One count bar line:** the build count badges (`BuildBar`) sit on the same line as the pre-scan count badges (`TriageBar`).
- **Filters:** drop the "Import error" and "Stale" filters (stale stays reachable from the count bar). Sort and group move to the right of the filters, in their own area. (Group is new: by cluster, pre-scan label or build status, to settle when started.)
- **Summary clickable:** the summary cell links to the item page, like the key.
- **Floating bulk toolbar:** with a selection, the bulk actions toolbar stays on top (sticky) while scrolling the list.

## MS2-2 — Copilot on demand, dependencies, clusters

Needs grilling. Input from the first MS2 plan, to revisit:

- **Trigger:** manual, per item or selection ("Ask Copilot"), not an automatic stage. Candidates: `unmapped`, `partial`, `likely_present`, `conflicts`, or any item the user wants checked.
- **Adapter:** standalone `copilot` CLI behind an interface, built on `ms2` (commit `40cd8a2`): `copilot -p PROMPT -s --no-ask-user [--model=M]` with tools denied; read-only escalation as opt-in; settings for binary, model, token cap; "test Copilot" button. Live test still pending.
- **Context bundle:** fixed priority under a token cap (ticket text, diffs, target code around hunks, MS2-1 findings, linked implementation tickets, source commit, PR discussion off by default). Stored with the verdict.
- **Verdict:** strict JSON: `makes_sense`, `still_required`, `already_present` (`yes|no|unsure` + reason), `repo` when unmapped, `depends_on`, `confidence`, `summary` (reused by MS5). Advisory; user accepts or overrides.
- **Dependencies:** candidate graph from 2.3 (files/hunks, Jira links, `[PATCH n/m]` series, symbols); which edges count without Copilot; dependency-ordered apply check on a temp index.
- **Clusters:** suggestions from edges; accept creates/extends an MS1 cluster, dismiss remembered.
