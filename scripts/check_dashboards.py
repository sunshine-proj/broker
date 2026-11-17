#!/usr/bin/env python3
"""Check the dashboards against a running Grafana.

For every dashboard in grafana/dashboards/ this checks that Grafana has loaded
it, then runs each variable query and each panel query (SQL or Prometheus)
through Grafana's own query API, over the dashboard's default time range. Grafana expands its time
macros itself; dashboard variables are filled in here as Grafana's browser
code would, with every value of a multi-value variable and the first value of
a single-value one.

    GRAFANA_ADMIN_USER=admin GRAFANA_ADMIN_PASSWORD=... python3 scripts/check_dashboards.py

GRAFANA_URL defaults to http://localhost:3000. Exits with 1 if anything fails.
Standard library only.
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
DATASOURCE = {"type": "grafana-postgresql-datasource", "uid": "marketdata"}
PROMETHEUS = {"type": "prometheus", "uid": "prometheus"}
_VARIABLE = re.compile(r"\$\{(\w+):sqlstring\}")
_REGEX_VARIABLE = re.compile(r"\$\{(\w+):regex\}")


class Grafana:
    def __init__(self, url: str, user: str, password: str) -> None:
        self.url = url.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.url + path, data=data, headers=self.headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload)
            except ValueError:
                return exc.code, payload.decode(errors="replace")

    def query(self, target: dict[str, Any], time_from: str) -> tuple[str | None, int]:
        """Run one panel query (SQL or Prometheus); returns (error, number of rows)."""
        status, body = self.call(
            "POST",
            "/api/ds/query",
            {"queries": [target | {"refId": "A"}], "from": time_from, "to": "now"},
        )
        result = body.get("results", {}).get("A", {}) if isinstance(body, dict) else {}
        error = result.get("error") or (None if status == 200 else f"HTTP {status}: {body}")
        if error:
            return str(error), 0
        rows = 0
        for frame in result.get("frames", []):
            values = frame.get("data", {}).get("values", [])
            rows += len(values[0]) if values else 0
        return None, rows

    def values(self, sql: str, time_from: str) -> list[str]:
        """The values a variable query offers (its __value column, or its first)."""
        status, body = self.call(
            "POST",
            "/api/ds/query",
            {
                "queries": [
                    {"refId": "A", "datasource": DATASOURCE, "rawSql": sql, "format": "table"}
                ],
                "from": time_from,
                "to": "now",
            },
        )
        frames = body.get("results", {}).get("A", {}).get("frames", []) if status == 200 else []
        if not frames:
            return []
        fields = [f["name"] for f in frames[0]["schema"]["fields"]]
        column = fields.index("__value") if "__value" in fields else 0
        return [str(v) for v in frames[0]["data"]["values"][column]]


def expand(sql: str, values: dict[str, list[str]]) -> str:
    def variable(match: re.Match[str]) -> str:
        chosen = values.get(match.group(1)) or ["__no_value__"]
        return ", ".join("'" + value.replace("'", "''") + "'" for value in chosen)

    def regex(match: re.Match[str]) -> str:
        chosen = values.get(match.group(1)) or ["__no_value__"]
        return "(" + "|".join(re.escape(value) for value in chosen) + ")"

    return _REGEX_VARIABLE.sub(regex, _VARIABLE.sub(variable, sql))


def main() -> int:
    grafana = Grafana(
        os.environ.get("GRAFANA_URL", "http://localhost:3000"),
        os.environ.get("GRAFANA_ADMIN_USER", "admin"),
        os.environ.get("GRAFANA_ADMIN_PASSWORD", ""),
    )
    failures = 0

    for uid in ("marketdata", "prometheus"):
        status, health = grafana.call("GET", f"/api/datasources/uid/{uid}/health")
        ok = status == 200 and isinstance(health, dict) and health.get("status") == "OK"
        print(f"{'ok  ' if ok else 'FAIL'} datasource {uid}: {health}")
        failures += not ok

    for path in sorted(DASHBOARDS.glob("*.json")):
        board = json.loads(path.read_text())
        time_from = board["time"]["from"]
        status, loaded = grafana.call("GET", f"/api/dashboards/uid/{board['uid']}")
        if status != 200:
            print(f"FAIL {board['uid']}: not loaded by Grafana (HTTP {status})")
            failures += 1
            continue
        print(f"ok   {board['uid']}: loaded as '{loaded['dashboard']['title']}'")

        values: dict[str, list[str]] = {}
        for variable in board["templating"]["list"]:
            options = grafana.values(expand(variable["query"], values), time_from)
            values[variable["name"]] = options if variable["multi"] else options[:1]
            print(f"       variable {variable['name']}: {len(options)} options")

        for item in board["panels"]:
            for target in item.get("targets", []):
                if "expr" in target:
                    query = target | {"expr": expand(target["expr"], values)}
                else:
                    query = {
                        "datasource": DATASOURCE,
                        "rawSql": expand(target["rawSql"], values),
                        "format": target["format"],
                        "rawQuery": True,
                    }
                error, rows = grafana.query(query, time_from)
                if error:
                    failures += 1
                    print(f"FAIL   {item['title']}: {error}")
                else:
                    print(f"       {item['title']}: {rows} rows")

    print("All dashboard queries ran." if not failures else f"{failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
