.PHONY: help test lint smoke

# The image ships for linux/amd64 only (the VPS); Docker on an arm64 Mac emulates it.
SMOKE_PLATFORM ?= linux/amd64

help: ## Show the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "%-16s %s\n", $$1, $$2}'

test: ## Tests
	uv run pytest

lint: ## Ruff check and format check
	uv run ruff check . && uv run ruff format --check .

smoke: ## Build the image and smoke-test it as CI does (Docker)
	docker build --platform $(SMOKE_PLATFORM) -t podcast-feed:smoke .
	SMOKE_PLATFORM=$(SMOKE_PLATFORM) scripts/smoke.sh podcast-feed:smoke
