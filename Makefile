.PHONY: install dev build test test-backend test-frontend test-e2e

install:
	cd backend && uv sync
	cd frontend && pnpm install
	cd e2e && pnpm install && pnpm exec playwright install chromium

# Backend with reload on :8000, Vite dev server on :5173 proxying /api.
dev:
	(cd backend && uv run uvicorn aosp_migrator.app:create_app --factory --reload --port 8000) & \
	(cd frontend && pnpm dev); \
	kill %1

# Frontend bundle goes into backend/aosp_migrator/static, then into the wheel.
build:
	cd frontend && pnpm build
	cd backend && uv build

test: test-backend test-frontend test-e2e

test-backend:
	cd backend && uv run pytest -q

test-frontend:
	cd frontend && pnpm typecheck && pnpm test

# Playwright smoke test against the fake Jira, on the freshly built bundle.
test-e2e:
	cd frontend && pnpm build
	cd e2e && pnpm typecheck && pnpm test
