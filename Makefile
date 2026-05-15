SHELL := /bin/zsh

.PHONY: help setup setup-identity setup-dashboard init ingest run run-identity run-dashboard dev test test-identity test-dashboard clean build-css watch-css

help:
	@echo "applyd root commands"
	@echo "  make setup           - install identity + dashboard dependencies"
	@echo "  make init            - initialize dashboard DB"
	@echo "  make ingest          - run one dashboard ingest cycle"
	@echo "  make run-identity    - start identity-service on :8100"
	@echo "  make run-dashboard   - start dashboard-service on :8000"
	@echo "  make run             - alias for make run-dashboard"
	@echo "  make build-css       - rebuild Tailwind CSS for dashboard + identity"
	@echo "  make watch-css       - rebuild CSS on template changes (Ctrl-C to stop)"
	@echo "  make dev             - setup + init + ingest"
	@echo "  make test            - run identity + dashboard tests"
	@echo "  make test-identity   - run identity-service tests"
	@echo "  make test-dashboard  - run dashboard tests"

setup:
	cd identity-service && uv sync
	cd dashboard && uv sync

setup-identity:
	cd identity-service && uv sync

setup-dashboard:
	cd dashboard && uv sync

init:
	cd dashboard && uv run python -m app.cli init-db

ingest:
	cd dashboard && uv run python -m app.cli ingest

run:
	cd dashboard && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run-dashboard:
	cd dashboard && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run-identity:
	cd identity-service && uv run uvicorn app.main:app --reload --reload-dir app --reload-dir templates --reload-dir static --reload-exclude "data/*" --host 0.0.0.0 --port 8100

dev: setup init ingest

# Build the shared Tailwind stylesheet from dashboard/static/css/input.css
# (which @sources both dashboard/ and identity-service/ templates), then
# mirror it into identity-service so both apps stay byte-identical.
build-css:
	cd dashboard && tailwindcss -i static/css/input.css -o static/css/app.css --minify
	cp dashboard/static/css/app.css identity-service/static/css/app.css

# Same as build-css but stays running and rebuilds on template/JS changes.
# Note: only writes dashboard/static/css/app.css; identity-service won't pick
# up changes until you rerun `make build-css`.
watch-css:
	cd dashboard && tailwindcss -i static/css/input.css -o static/css/app.css --watch

test:
	cd identity-service && uv run --group dev pytest
	cd dashboard && uv run --group dev pytest tests

test-identity:
	cd identity-service && uv run --group dev pytest

test-dashboard:
	cd dashboard && uv run --group dev pytest tests

clean:
	rm -f dashboard/.pytest_cache
