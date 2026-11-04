# ToolNet ReelShort Metadata API — operator shortcuts.
# Secrets live in .env (never committed); see .env.example.

SHELL := /bin/bash
VENV  := .venv
PY    := $(VENV)/bin/python

.DEFAULT_GOAL := help
.PHONY: help venv deps migrate test test-live run dev lint build up down restart logs ps health backup restore fixtures shell

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the local virtualenv
	python3 -m venv $(VENV)

deps: ## Install runtime + dev dependencies
	$(PY) -m pip install -U pip
	$(PY) -m pip install -r requirements.txt -r requirements-dev.txt

migrate: ## Apply database migrations
	$(VENV)/bin/alembic upgrade head

test: ## Run the full test suite (no network)
	$(PY) -m pytest -p no:cacheprovider

test-live: ## Run opt-in tests against the real ReelShort site
	$(PY) -m pytest -m live -p no:cacheprovider

run: ## Run the API locally on 127.0.0.1:8077
	$(VENV)/bin/uvicorn app.main:app --host 127.0.0.1 --port 8077

build: ## Build the Docker image
	docker compose build

up: ## Start the stack (127.0.0.1:8077)
	docker compose up -d --build

down: ## Stop the stack
	docker compose down

restart: ## Restart the container
	docker compose restart reelshort-api

logs: ## Tail container logs
	docker compose logs -f --tail=100

ps: ## Show container status
	docker compose ps

health: ## Check localhost health + readiness
	@curl -fsS http://127.0.0.1:8077/health | head -c 400; echo
	@curl -fsS http://127.0.0.1:8077/health/ready | head -c 400; echo

backup: ## Back up the SQLite database
	DATA_DIR=$${DATA_DIR:-./data} DB_PATH=$${DB_PATH:-./data/reelshort.sqlite3} scripts/backup.sh

restore: ## Restore a backup: make restore ARCHIVE=./data/backups/xxx.sqlite3.gz
	DATA_DIR=$${DATA_DIR:-./data} DB_PATH=$${DB_PATH:-./data/reelshort.sqlite3} scripts/restore.sh $(ARCHIVE)

fixtures: ## Refresh recorded test fixtures from the live site
	$(PY) scripts/refresh_fixtures.py

shell: ## Open a shell inside the container
	docker compose exec reelshort-api /bin/sh
