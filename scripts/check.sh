#!/usr/bin/env bash
# Lint, format check and tests for the backend. Extra arguments go to pytest,
# for example: scripts/check.sh -m "not integration"
set -euo pipefail

cd "$(dirname "$0")/../backend"

ruff check .
ruff format --check .
pytest "$@"
