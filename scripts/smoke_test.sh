#!/usr/bin/env bash
# Verifies a production deployment from the outside, as a user would reach it.
#
#     SMOKE_PASSWORD=... scripts/smoke_test.sh https://data.example.com USER
#
# USER is an existing account (python -m app.cli user add). Checks HTTPS and its
# headers; that the API, its documentation and Grafana need a signed-in user and
# send browsers to the login page; that a session opens all three and passes the
# user on to Grafana; that a forged user header is ignored; that metrics and
# unknown paths are not served; and that manual job triggers still need the
# administrator key. With a localhost URL, certificates from Caddy's own
# authority are accepted.
set -uo pipefail

base="${1:?usage: SMOKE_PASSWORD=... scripts/smoke_test.sh https://HOST[:PORT] USER}"
user="${2:?usage: SMOKE_PASSWORD=... scripts/smoke_test.sh https://HOST[:PORT] USER}"
password="${SMOKE_PASSWORD:?set SMOKE_PASSWORD}"
jar="$(mktemp)"
trap 'rm -f "$jar"' EXIT
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
where() { curl "${curl_opts[@]}" -o /dev/null -w '%{http_code} %{redirect_url}' "$@"; }
signed_in() { curl "${curl_opts[@]}" -b "$jar" "$@"; }

# Not signed in.
check "root redirects to Grafana" "308 */grafana/" "$(where "$base/")"
check "Grafana sends browsers to the login page" "303 */auth/login?next=/grafana/" \
    "$(where -H 'Accept: text/html' "$base/grafana/")"
check "a forged user header is ignored" "303 */auth/login*" \
    "$(where -H 'Accept: text/html' -H "X-WEBAUTH-USER: $user" -H 'X-WEBAUTH-ROLE: Admin' \
        "$base/grafana/")"
check "Grafana's API needs a user" "401" "$(status "$base/grafana/api/user")"
check "login page" "200" "$(status "$base/auth/login")"
check "health is public" "200" "$(status "$base/api/v1/health")"
check "API needs a user" "401" "$(status "$base/api/v1/instruments?limit=1")"
check "API documentation needs a user" "303 */auth/login?next=/docs" "$(where "$base/docs")"
check "a wrong password is refused" "401" \
    "$(status --data-urlencode "username=$user" --data-urlencode "password=not the password" \
        "$base/auth/login")"

# Signed in.
check "signing in" "303" \
    "$(curl "${curl_opts[@]}" -c "$jar" -o /dev/null -w '%{http_code}' \
        --data-urlencode "username=$user" --data-urlencode "password=$password" \
        "$base/auth/login")"
# Netscape cookie-jar format: "#HttpOnly_" marks HttpOnly, field 4 is Secure.
check "session cookie is HttpOnly and Secure" "HttpOnly Secure" \
    "$(awk '/marketdata_session/ {print ($1 ~ /^#HttpOnly_/ ? "HttpOnly" : "-"), \
        ($4 == "TRUE" ? "Secure" : "-")}' "$jar")"
check "who am I" "*\"username\":\"$user\"*" "$(signed_in "$base/api/v1/auth/me")"
check "API data" "200" "$(signed_in -o /dev/null -w '%{http_code}' "$base/api/v1/instruments?limit=1")"
check "API documentation" "200" "$(signed_in -o /dev/null -w '%{http_code}' "$base/docs")"
check "Grafana knows the user" "*\"login\":\"$user\"*" "$(signed_in "$base/grafana/api/user")"
check "manual trigger needs the admin key" "40[13]" \
    "$(signed_in -o /dev/null -w '%{http_code}' -X POST \
        "$base/api/v1/collectors/ecb_daily_quotes/run")"
check "metrics are not served" "404" "$(signed_in -o /dev/null -w '%{http_code}' "$base/metrics")"
check "unknown paths are not served" "404" "$(status "$base/prometheus/")"
check "signing out" "303" \
    "$(signed_in -o /dev/null -w '%{http_code}' -X POST "$base/auth/logout")"
check "the session has ended" "401" "$(signed_in -o /dev/null -w '%{http_code}' "$base/api/v1/auth/me")"

headers="$(curl "${curl_opts[@]}" -o /dev/null -D - "$base/auth/login" | tr -d '\r')"
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
