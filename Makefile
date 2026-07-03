.PHONY: lint lint-fix fmt fmt-check test all

PY := .venv/bin/python
RUFF := .venv/bin/ruff
PYTEST := .venv/bin/pytest

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
