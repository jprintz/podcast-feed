.PHONY: help test lint

help: ## Show the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "%-16s %s\n", $$1, $$2}'

test: ## Tests
	uv run pytest

lint: ## Ruff check and format check
	uv run ruff check . && uv run ruff format --check .
