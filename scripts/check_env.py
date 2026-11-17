#!/usr/bin/env python3
"""Check a .env before deploying it to production.

    python3 scripts/check_env.py [.env]

Reports every problem, and exits with 1 if there is any:

- the production compose files are not selected (COMPOSE_FILE);
- a required setting is missing (domain, Let's Encrypt contact, API account);
- a password or key is empty, a development default, too short, or reused;
- the API password is not a bcrypt hash;
- the file can be read by other users.

Standard library only.
"""

import argparse
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env_file import read  # noqa: E402

PASSWORDS = (
    "POSTGRES_PASSWORD",
    "BACKEND_DB_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
    "GRAFANA_DB_PASSWORD",
)
REQUIRED = ("POSTGRES_DB", "POSTGRES_USER", "GRAFANA_ADMIN_USER", "DOMAIN", "ACME_EMAIL", "API_USERNAME")
MIN_LENGTH = 20
# Values that come from documentation or development and must never go live.
PLACEHOLDERS = re.compile(r"change_?me|password|secret|example|test|^admin$", re.IGNORECASE)


def problems(values: dict[str, str], mode: int | None) -> list[str]:
    found = []
    if values.get("COMPOSE_FILE") != "docker-compose.yml:docker-compose.prod.yml":
        found.append("COMPOSE_FILE must be docker-compose.yml:docker-compose.prod.yml")
    for key in REQUIRED:
        if not values.get(key):
            found.append(f"{key} is not set")

    domain = values.get("DOMAIN", "")
    if domain and (domain == "localhost" or "." not in domain or "/" in domain):
        found.append(f"DOMAIN must be a public host name, not {domain!r}")
    email = values.get("ACME_EMAIL", "")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        found.append("ACME_EMAIL is not an email address")
    if not values.get("API_PASSWORD_HASH", "").startswith(("$2a$", "$2b$", "$2y$")):
        found.append("API_PASSWORD_HASH must be a bcrypt hash (caddy hash-password)")

    seen: dict[str, str] = {}
    for key in PASSWORDS:
        value = values.get(key, "")
        if not value:
            found.append(f"{key} is empty")
            continue
        if PLACEHOLDERS.search(value):
            found.append(f"{key} is a placeholder or development value")
        elif len(value) < MIN_LENGTH:
            found.append(f"{key} is shorter than {MIN_LENGTH} characters")
        if value in seen:
            found.append(f"{key} is the same as {seen[value]}")
        seen[value] = key
    if PLACEHOLDERS.fullmatch(values.get("GRAFANA_ADMIN_USER", "")):
        found.append("GRAFANA_ADMIN_USER should not be the well-known 'admin'")

    admin_key = values.get("ADMIN_API_KEY", "")
    if admin_key and (len(admin_key) < 32 or PLACEHOLDERS.search(admin_key)):
        found.append("ADMIN_API_KEY must be empty (triggers disabled) or at least 32 random characters")
    for key, value in values.items():
        if key.endswith(("_API_KEY", "_TOKEN")) and PLACEHOLDERS.fullmatch(value or "-"):
            found.append(f"{key} holds a placeholder; leave it empty or set the real key")

    if values.get("BIND_ADDRESS", "127.0.0.1") not in ("127.0.0.1", "::1"):
        found.append("BIND_ADDRESS should stay 127.0.0.1; production publishes only the proxy")
    if mode is not None and mode & (stat.S_IRWXG | stat.S_IRWXO):
        found.append(f"the file is readable by others (mode {oct(mode & 0o777)}); chmod 600 it")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", nargs="?", default=".env", type=Path)
    args = parser.parse_args()
    if not args.path.exists():
        print(f"{args.path} does not exist", file=sys.stderr)
        return 1
    found = problems(read(args.path), stat.S_IMODE(args.path.stat().st_mode))
    for problem in found:
        print(f"FAIL {problem}")
    if not found:
        print(f"ok   {args.path} is ready for production")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
