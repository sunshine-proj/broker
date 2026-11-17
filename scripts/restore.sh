#!/usr/bin/env bash
# Restores a backup made by the backup service into a new database.
#
#     scripts/restore.sh backups/marketdata-20261117T030000Z.dump [TARGET_DATABASE]
#
# TARGET_DATABASE defaults to <POSTGRES_DB>_restore and must not exist yet. To
# replace the live database, follow "Restoring the Live Database" in
# docs/deployment.md. Needs the Compose stack's db service running. The
# restored database has the same TimescaleDB version as the server.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
set -a
# shellcheck disable=SC1091
. ./.env
set +a

file="${1:?usage: scripts/restore.sh BACKUP_FILE [TARGET_DATABASE]}"
target="${2:-${POSTGRES_DB}_restore}"
if [[ ! "$target" =~ ^[a-z_][a-z0-9_]{0,62}$ ]]; then
    echo "Invalid database name: $target" >&2
    exit 1
fi
if [[ -f "$file.sha256" ]]; then
    (cd "$(dirname "$file")" && sha256sum --check --quiet "$(basename "$file").sha256")
    echo "Checksum verified."
fi

psql() { docker compose exec -T db psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$@"; }

if [[ -n "$(psql -d postgres -A -t -c "SELECT 1 FROM pg_database WHERE datname = '$target'")" ]]; then
    echo "Database $target already exists; choose another name or drop it first." >&2
    exit 1
fi

echo "Restoring $file into $target..."
psql -d postgres -c "CREATE DATABASE $target"
# TimescaleDB's documented restore: the extension first, in restoring mode.
psql -d "$target" -c "CREATE EXTENSION IF NOT EXISTS timescaledb" \
    -c "SELECT timescaledb_pre_restore()" > /dev/null
# The dump re-creates the extension it already has; that one error is expected.
log="$(mktemp)"
if ! docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$target" \
        --no-owner --no-privileges < "$file" 2> "$log"; then
    if grep -v -e 'extension "timescaledb" already exists' -e 'errors ignored on restore' \
            -e 'while PROCESSING TOC' -e 'from TOC entry' -e 'Command was: CREATE EXTENSION' \
            -e '^$' "$log" | grep -q .; then
        cat "$log" >&2
        echo "Restore failed; $target is incomplete. Drop it before trying again." >&2
        exit 1
    fi
fi
rm -f "$log"
psql -d "$target" -c "SELECT timescaledb_post_restore()" -c "ANALYZE" > /dev/null
echo "Restored into $target."
