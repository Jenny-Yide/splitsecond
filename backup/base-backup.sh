#!/bin/sh
# wal-g base backups. Runs next to PostgreSQL because wal-g reads the data directory directly.
#   base-backup.sh          loop: take a backup whenever the last successful one is older than the interval
#   base-backup.sh --once   take one backup now
# Connection settings come from the standard PG* variables, storage settings from WALG_*/AWS_*.
set -eu

INTERVAL_HOURS="${BASE_BACKUP_INTERVAL_HOURS:-24}"
RETAIN_FULL="${BASE_BACKUP_RETAIN_FULL:-7}"
CHECK_SECONDS="${BASE_BACKUP_CHECK_SECONDS:-300}"

sql() {
    psql -X -v ON_ERROR_STOP=1 -qtA -c "$1"
}

backup_once() {
    started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if ! wal-g backup-push "$PGDATA"; then
        sql "INSERT INTO backup_runs (kind, started_at, finished_at, status)
             VALUES ('base', '$started', now(), 'failed')" || true
        echo "base backup failed" >&2
        return 1
    fi

    name=$(wal-g backup-list 2>/dev/null | awk 'NR > 1 { name = $1 } END { print name }')
    sql "INSERT INTO backup_runs (kind, started_at, finished_at, status, object_key)
         VALUES ('base', '$started', now(), 'success', '${WALG_S3_PREFIX}/basebackups_005/${name:-unknown}')"
    echo "base backup ${name:-unknown} finished"

    wal-g delete retain FULL "$RETAIN_FULL" --confirm || echo "retention cleanup failed" >&2
}

backup_due() {
    sql "SELECT coalesce(max(finished_at) < now() - interval '$INTERVAL_HOURS hours', true)
         FROM backup_runs WHERE kind = 'base' AND status = 'success'"
}

until pg_isready -q; do
    sleep 2
done

if [ "${1:-}" = "--once" ]; then
    backup_once
    exit
fi

while true; do
    if [ "$(backup_due)" = "t" ]; then
        backup_once || true
    fi
    sleep "$CHECK_SECONDS"
done
