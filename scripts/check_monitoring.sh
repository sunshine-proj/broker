#!/usr/bin/env bash
# Checks the Prometheus configuration, unit-tests the alert rules and checks
# the Alertmanager configuration, with the tools from the images Compose uses.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
prometheus="$(sed -n 's/^ *image: \(prom\/prometheus:.*\)$/\1/p' "$repo/docker-compose.yml")"
alertmanager="$(sed -n 's/^ *image: \(prom\/alertmanager:.*\)$/\1/p' "$repo/docker-compose.yml")"

docker run --rm -v "$repo/prometheus":/etc/prometheus:ro --entrypoint promtool "$prometheus" \
    check config /etc/prometheus/prometheus.yml
docker run --rm -v "$repo/prometheus":/etc/prometheus:ro -w /etc/prometheus/tests \
    --entrypoint promtool "$prometheus" test rules alerts_test.yml
docker run --rm -v "$repo/alertmanager":/etc/alertmanager:ro --entrypoint amtool "$alertmanager" \
    check-config /etc/alertmanager/alertmanager.yml
