.PHONY: lint lint-fix fmt fmt-check test all

# Prefer the project venv when present (typical local dev loop) but fall
# back to PATH-resolved commands so CI, systemd-launched shells, and any
# externally-managed environment work without adjustment.
VENV := .venv/bin
RUFF   := $(shell test -x $(VENV)/ruff   && echo $(VENV)/ruff   || echo ruff)
PYTEST := $(shell test -x $(VENV)/pytest && echo $(VENV)/pytest || echo pytest)

# `ruff check` — rule-based linter. Safe auto-fixes only.
lint:
	$(RUFF) check .

lint-fix:
	$(RUFF) check . --fix

# `ruff format` — whole-file formatter (≈ Black). Writes changes.
fmt:
	$(RUFF) format .

# CI gate: fail if any file needs reformatting. Prints a diff so it's
# obvious which files drifted.
fmt-check:
	$(RUFF) format --check .

test:
	$(PYTEST) -q

# Full check: formatting, linting, tests. Use this in CI.
all: fmt-check lint test
