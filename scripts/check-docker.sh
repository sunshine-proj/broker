#!/usr/bin/env bash
# Runs scripts/check.sh in a throwaway Python container, for hosts without a
# local Python environment. Needs the stack's database running
# (docker compose up -d db). Extra arguments go to pytest.
#
# The container starts on Docker's default network, to install the
# dependencies, and joins the stack's internal network, where the database is.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
image="$(sed -n 's/^FROM \(python:.*\)$/\1/p' "$repo/backend/Dockerfile")"

container="$(docker create \
    --env-file "$repo/.env" \
    -e POSTGRES_HOST=db \
    -e POSTGRES_PORT=5432 \
    -e PIP_ROOT_USER_ACTION=ignore \
    -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
    -v "$repo":/src:ro \
    "$image" \
    sh -c 'cp -r /src /work && cd /work && pip install -q -r backend/requirements-dev.txt && bash scripts/check.sh "$@"' check "$@")"
trap 'docker rm -f "$container" > /dev/null' EXIT
docker network connect --alias tests market-data-platform_internal "$container"
docker start --attach "$container"
