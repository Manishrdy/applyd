SHELL := /bin/zsh

.PHONY: help setup setup-dashboard init ingest run run-dashboard dev test test-dashboard clean build-css watch-css

help:
	@echo "applyd root commands"
	@echo "  make setup           - install dashboard dependencies"
	@echo "  make init            - initialize dashboard DB"
	@echo "  make ingest          - run one dashboard ingest cycle"
	@echo "  make run-dashboard   - start dashboard-service on :8000"
	@echo "  make run             - alias for make run-dashboard"
	@echo "  make build-css       - rebuild Tailwind CSS for dashboard"
	@echo "  make watch-css       - rebuild CSS on template changes (Ctrl-C to stop)"
	@echo "  make dev             - setup + init + ingest"
	@echo "  make test            - run dashboard tests"
	@echo "  make test-dashboard  - run dashboard tests"

setup:
	cd dashboard && uv sync

setup-dashboard:
	cd dashboard && uv sync

init:
	cd dashboard && uv run python -m app.cli init-db

ingest:
	cd dashboard && uv run python -m app.cli ingest

run:
	cd dashboard && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run-app:
	cd dashboard && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev: setup init ingest

# Build Tailwind stylesheet from dashboard/static/css/input.css.
build-css:
	cd dashboard && tailwindcss -i static/css/input.css -o static/css/app.css --minify

# Same as build-css but stays running and rebuilds on template/JS changes.
watch-css:
	cd dashboard && tailwindcss -i static/css/input.css -o static/css/app.css --watch

test:
	cd dashboard && uv run --group dev pytest tests

test-dashboard:
	cd dashboard && uv run --group dev pytest tests

clean:
	rm -f dashboard/.pytest_cache
