#!/bin/sh
# The backup service: a backup every BACKUP_INTERVAL_HOURS. The first one is
# timed from the last successful backup, so a restart does not make an extra
# one. A failed backup is retried after an hour.
set -u

interval=$(( ${BACKUP_INTERVAL_HOURS:-24} * 3600 ))

# The database_backup table exists once the migrate service has run.
until psql -X -q -c 'SELECT 1 FROM database_backup LIMIT 1' > /dev/null 2>&1; do
    echo "Waiting for the database..."
    sleep 10
done

since="$(psql -X -A -t -c "SELECT coalesce(extract(epoch FROM now() - max(finished_at))::int, -1)
                           FROM database_backup WHERE status = 'success'")"
if [ "$since" -ge 0 ] && [ "$since" -lt "$interval" ]; then
    echo "Last backup $since seconds ago; next in $(( interval - since )) seconds."
    sleep $(( interval - since ))
fi

while true; do
    if /bin/sh /backup/run-backup.sh; then
        sleep "$interval"
    else
        sleep 3600
    fi
done
