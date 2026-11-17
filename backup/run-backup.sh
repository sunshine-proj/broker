#!/bin/sh
# Makes one backup of the database and records it in the database_backup table.
#
# Runs in the backup service (TimescaleDB image, so pg_dump matches the server).
# The dump is PostgreSQL's custom format, without owners or privileges: the
# migrate service recreates the roles and grants after a restore. It is
# written as .partial, checked with pg_restore --list, then renamed, with a
# SHA-256 checksum beside it. Only the newest BACKUP_KEEP dumps are kept.
#
# Connection: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD.
set -eu

dir=/backups
keep="${BACKUP_KEEP:-7}"
interval=$(( ${BACKUP_INTERVAL_HOURS:-24} * 3600 ))
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
name="${PGDATABASE}-$(date -u +%Y%m%dT%H%M%SZ).dump"
# Backups hold every stored value: readable by their owner only.
umask 077

record() {
    # status, file name, size in bytes, error
    psql -X -q -v ON_ERROR_STOP=1 -v started="$started" -v status="$1" -v file="$2" \
        -v size="$3" -v interval="$interval" -v error="$4" <<'SQL'
INSERT INTO database_backup
       (started_at, finished_at, status, file_name, size_bytes, interval_seconds, error)
VALUES (:'started', now(), :'status', NULLIF(:'file', ''), NULLIF(:'size', '')::bigint,
        :interval, NULLIF(:'error', ''));
SQL
}

dump() {
    pg_dump --format=custom --no-owner --no-privileges --file="$dir/$name.partial"
    pg_restore --list "$dir/$name.partial" > /dev/null
    mv "$dir/$name.partial" "$dir/$name"
    (cd "$dir" && sha256sum "$name" > "$name.sha256")
}

log="$(mktemp)"
if ! dump > "$log" 2>&1; then
    rm -f "$dir/$name.partial"
    error="$(tail -c 2000 "$log")"
    echo "Backup failed: $error" >&2
    record failed "" "" "$error" || true
    exit 1
fi
rm -f "$log"

size="$(wc -c < "$dir/$name" | tr -d ' ')"
record success "$name" "$size" ""
echo "Backup written: $name ($size bytes)"

# Keep the newest $keep dumps.
ls -1t "$dir/$PGDATABASE"-*.dump 2>/dev/null | tail -n +"$((keep + 1))" | while read -r old; do
    rm -f "$old" "$old.sha256"
    echo "Old backup removed: $(basename "$old")"
done
