#!/usr/bin/env bash
# Runs scripts/check.sh in a throwaway Python container attached to the Compose
# network, for hosts without a local Python environment. Needs the stack's
# database running (docker compose up -d db). Extra arguments go to pytest.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"

docker run --rm \
    --network market-data-platform_internal \
    --env-file "$repo/.env" \
    -e POSTGRES_HOST=db \
    -e POSTGRES_PORT=5432 \
    -e PIP_ROOT_USER_ACTION=ignore \
    -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
    -v "$repo":/src:ro \
    python:3.12-slim-bookworm \
    sh -c 'cp -r /src /work && cd /work && pip install -q -r backend/requirements-dev.txt && bash scripts/check.sh "$@"' check "$@"
