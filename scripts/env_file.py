"""Reading and writing .env files the way Docker Compose reads them (stdlib only)."""

import re
from pathlib import Path

_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def parse(text: str) -> dict[str, str]:
    """KEY=VALUE lines; single- or double-quoted values are unquoted. Comments are skipped."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line.strip())
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def read(path: Path) -> dict[str, str]:
    return parse(path.read_text())


def quote(value: str) -> str:
    """A value Compose takes literally: single quotes stop `$` interpolation."""
    if value and re.fullmatch(r"[A-Za-z0-9_.:/@,+=-]*", value):
        return value
    return "'" + value + "'"
