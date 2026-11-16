#!/bin/bash
# Prepares an empty PGDATA for point-in-time recovery from the wal-g archive.
# PostgreSQL then replays WAL on startup and promotes itself when it reaches the target.
#
#   RESTORE_TARGET=latest                  replay everything that was archived
#   RESTORE_TARGET=2026-11-16T01:30:00Z    stop just before this moment (UTC, this exact format)
#
#   restore-pitr.sh                  restore into PGDATA, which must be empty
#   restore-pitr.sh --if-requested   init-container mode: do nothing unless RESTORE_TARGET is set
#                                    and PGDATA has never been initialized
set -euo pipefail

target="${RESTORE_TARGET:-}"

if [[ "${1:-}" == "--if-requested" ]]; then
    [[ -n "$target" ]] || exit 0
    if [[ -s "$PGDATA/PG_VERSION" ]]; then
        echo "PGDATA is already initialized; not restoring over it"
        exit 0
    fi
fi

if [[ -z "$target" ]]; then
    echo "set RESTORE_TARGET to 'latest' or a UTC time like 2026-11-16T01:30:00Z" >&2
    exit 2
fi
if [[ "$target" != latest && ! "$target" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
    echo "RESTORE_TARGET must be 'latest' or YYYY-MM-DDTHH:MM:SSZ, got '$target'" >&2
    exit 2
fi
if [[ -d "$PGDATA" && -n "$(ls -A "$PGDATA")" ]]; then
    echo "$PGDATA is not empty" >&2
    exit 1
fi

if [[ "$target" == latest ]]; then
    base=LATEST
else
    # backup-list columns: name, modified (finish time, same UTC format), ...
    base=$(wal-g backup-list 2>/dev/null | awk -v t="$target" 'NR > 1 && $2 < t { name = $1 } END { print name }')
    if [[ -z "$base" ]]; then
        echo "no base backup finished before $target" >&2
        exit 1
    fi
fi

echo "restoring base backup $base, recovery target: $target"
mkdir -p "$PGDATA"
wal-g backup-fetch "$PGDATA" "$base"

# A base backup taken after an earlier restore still carries that restore's settings;
# a leftover recovery_target_time would stop this recovery at the old target.
sed -i -E '/^# Added by restore-pitr\.sh/d; /^(restore_command|recovery_target[a-z_]*) = /d' \
    "$PGDATA/postgresql.auto.conf"
{
    echo "# Added by restore-pitr.sh; ignored once recovery has finished"
    echo "restore_command = 'wal-g wal-fetch %f %p'"
    echo "recovery_target_timeline = 'latest'"
    if [[ "$target" != latest ]]; then
        # The setting does not accept the ISO 'T...Z' form.
        pg_time="${target/T/ }"
        echo "recovery_target_time = '${pg_time%Z}+00'"
        echo "recovery_target_inclusive = false"
    fi
    echo "recovery_target_action = 'promote'"
} >> "$PGDATA/postgresql.auto.conf"
touch "$PGDATA/recovery.signal"

if [[ "$(id -u)" == 0 ]]; then
    chown -R postgres:postgres "$PGDATA"
fi
chmod 700 "$PGDATA"
echo "PGDATA prepared; PostgreSQL will recover on startup"
