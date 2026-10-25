# aosp-migrator

Work in progress. Needs Python 3.12 + `uv`, Node >= 20 + `pnpm`.

## Test

```sh
make install   # deps + Playwright Chromium
make test      # backend, frontend, e2e smoke test
```

## Try it

```sh
cd frontend && pnpm build && cd ..
cd backend && uv run aosp-migrator serve   # http://localhost:8000
```

Without a real Jira, run the fake one in a second terminal and use
`http://localhost:8090` with PAT `fake-jira-pat` in Settings:

```sh
cd backend && PYTHONPATH=tests uv run python -m fake_jira --port 8090
```
