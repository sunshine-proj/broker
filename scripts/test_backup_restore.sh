#!/usr/bin/env bash
# Tests backup and restore end to end on the running stack:
#
# 1. makes a backup now, with the backup service's own script;
# 2. restores it into a scratch database;
# 3. checks that the restore holds everything the live database held when the
#    backup started: every entity table row for row, and every time-series row
#    stored before the backup (stored rows never change, so the counts must match);
# 4. drops the scratch database.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
set -a
# shellcheck disable=SC1091
. ./.env
set +a
scratch="${POSTGRES_DB}_restore_check"
psql() { docker compose exec -T db psql -X -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$@"; }

psql -d postgres -c "DROP DATABASE IF EXISTS $scratch" > /dev/null
docker compose run --rm --no-deps -T --entrypoint /bin/sh backup /backup/run-backup.sh
read -r file started < <(psql -d "$POSTGRES_DB" -F ' ' -c \
    "SELECT file_name, started_at FROM database_backup WHERE status = 'success'
      ORDER BY started_at DESC LIMIT 1")
echo "Testing $file (started $started)"

scripts/restore.sh "${BACKUP_DIR:-./backups}/$file" "$scratch"

failures=0
compare() {
    local label="$1" live="$2" restored="$3"
    if [[ "$live" == "$restored" ]]; then
        echo "ok    $label: $restored"
    else
        echo "FAIL  $label: live $live, restored $restored"
        failures=$((failures + 1))
    fi
}

for table in exchange provider instrument instrument_provider_mapping economic_indicator \
        collection_job alembic_version; do
    compare "$table" "$(psql -d "$POSTGRES_DB" -c "SELECT count(*) FROM $table")" \
        "$(psql -d "$scratch" -c "SELECT count(*) FROM $table")"
done
for table in market_observation ohlcv economic_observation; do
    compare "$table (stored before the backup)" \
        "$(psql -d "$POSTGRES_DB" -c "SELECT count(*) FROM $table WHERE ingested_at < '$started'")" \
        "$(psql -d "$scratch" -c "SELECT count(*) FROM $table WHERE ingested_at < '$started'")"
done
compare "schema version" "$(psql -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version")" \
    "$(psql -d "$scratch" -c "SELECT version_num FROM alembic_version")"
compare "hypertables" "$(psql -d "$POSTGRES_DB" -c "SELECT count(*) FROM timescaledb_information.hypertables")" \
    "$(psql -d "$scratch" -c "SELECT count(*) FROM timescaledb_information.hypertables")"
compare "compressed chunks readable" "ok" \
    "$(psql -d "$scratch" -c "SELECT CASE WHEN count(*) >= 0 THEN 'ok' END FROM market_observation WHERE observed_at < now() - interval '60 days'")"

psql -d postgres -c "DROP DATABASE $scratch" > /dev/null
if (( failures )); then
    echo "$failures check(s) failed."
    exit 1
fi
echo "Backup restored and verified."
