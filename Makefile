SHELL := /bin/bash
PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: help dev-local dev-lakebase refresh-lakebase url dev-down dev-destroy hooks-install hooks-uninstall check
.DEFAULT_GOAL := help

help: ## Show available commands
	@awk -F ':.*## ' '/^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

dev-local: ## Start native frontend/backend with session-scoped in-memory PGlite
	@$(PYTHON) scripts/dev.py local

dev-lakebase: ## Start native frontend/backend with this Git branch's Lakebase branch
	@$(PYTHON) scripts/dev.py lakebase

refresh-lakebase: ## Refresh credentials and restart only the running Lakebase backend
	@$(PYTHON) scripts/dev.py refresh

url: ## Print this worktree's running storefront URL
	@$(PYTHON) scripts/dev.py url

dev-down: ## Stop this checkout's session and discard its in-memory database
	@$(PYTHON) scripts/dev.py down

dev-destroy: ## Stop development and remove saved session logs and metadata
	@$(PYTHON) scripts/dev.py destroy

hooks-install: ## Install standard Git hooks, preserving existing hooks
	@./scripts/manage-post-checkout-hook.sh install

hooks-uninstall: ## Remove DataCart hooks and restore previous Git settings
	@./scripts/manage-post-checkout-hook.sh uninstall

check: ## Validate hooks, workshop setup, native runtime, and PGlite behavior
	@$(PYTHON) scripts/dev.py setup
	@.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
	@bash ./scripts/dev-context-id.sh --self-test
	@./scripts/lakebase-branch.sh --self-test
	@cd frontend && npm run build
