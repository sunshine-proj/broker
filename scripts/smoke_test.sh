#!/usr/bin/env bash
# Verifies a production deployment from the outside, as a user would reach it.
#
#     API_PASSWORD=... scripts/smoke_test.sh https://data.example.com [api-user]
#
# Checks HTTPS and its headers, that Grafana answers and asks for a login, that
# the API needs its basic-auth account, that metrics and unknown paths are not
# served, and that manual job triggers still need the administrator key. With
# a localhost URL, certificates from Caddy's own authority are accepted.
set -uo pipefail

base="${1:?usage: API_PASSWORD=... scripts/smoke_test.sh https://HOST[:PORT] [API_USER]}"
user="${2:-api}"
password="${API_PASSWORD:?set API_PASSWORD}"
curl_opts=(--silent --show-error --max-time 20)
[[ "$base" == https://localhost* ]] && curl_opts+=(--insecure)
failures=0

check() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$actual" == $expected ]]; then
        echo "ok    $label ($actual)"
    else
        echo "FAIL  $label: expected $expected, got $actual"
        failures=$((failures + 1))
    fi
}
status() { curl "${curl_opts[@]}" -o /dev/null -w "%{http_code}" "$@"; }

check "root redirects to Grafana" "308 */grafana/" \
    "$(curl "${curl_opts[@]}" -o /dev/null -w '%{http_code} %{redirect_url}' "$base/")"
check "Grafana is healthy" '*"database"*"ok"*' "$(curl "${curl_opts[@]}" "$base/grafana/api/health" | tr -d ' \n')"
check "Grafana asks for a login" "302 */grafana/login" \
    "$(curl "${curl_opts[@]}" -o /dev/null -w '%{http_code} %{redirect_url}' "$base/grafana/")"
check "API without credentials" "401" "$(status "$base/api/v1/health")"
check "API with a wrong password" "401" "$(status -u "$user:wrong" "$base/api/v1/health")"
check "API with its account" "200" "$(status -u "$user:$password" "$base/api/v1/health")"
check "API data" "200" "$(status -u "$user:$password" "$base/api/v1/instruments?limit=1")"
check "API documentation" "200" "$(status -u "$user:$password" "$base/docs")"
check "manual trigger needs the admin key" "40[13]" \
    "$(status -u "$user:$password" -X POST "$base/api/v1/collectors/ecb_daily_quotes/run")"
check "metrics are not served" "404" "$(status -u "$user:$password" "$base/metrics")"
check "unknown paths are not served" "404" "$(status "$base/prometheus/")"

headers="$(curl "${curl_opts[@]}" -o /dev/null -D - "$base/grafana/login" | tr -d '\r')"
check "HSTS" "*max-age=31536000*" "$(grep -i '^strict-transport-security:' <<< "$headers")"
check "no MIME sniffing" "*nosniff*" "$(grep -i '^x-content-type-options:' <<< "$headers")"
check "server not named" "" "$(grep -i '^server:' <<< "$headers")"

http_base="${base/https:/http:}"
if [[ "$http_base" =~ ^(http://[^/:]+):([0-9]+)$ && -n "${HTTP_PORT:-}" ]]; then
    http_base="${BASH_REMATCH[1]}:$HTTP_PORT"
fi
check "plain HTTP redirects to HTTPS" "30[18]" "$(status "$http_base/")"

if (( failures )); then
    echo "$failures check(s) failed."
    exit 1
fi
echo "Deployment verified."
