#!/usr/bin/env bash
# Lint, formatage, typage et tests unitaires : ce que la CI exécute.
set -euo pipefail

uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
