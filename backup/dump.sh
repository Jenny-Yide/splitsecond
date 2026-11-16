#!/bin/bash
# Daily logical backup: pg_dump streamed to object storage, recorded in backup_runs, old dumps pruned.
# Independent of the WAL archive, and restorable into any PostgreSQL 16 without wal-g.
# Connection settings come from the standard PG* variables, storage settings from AWS_* and S3_BUCKET.
set -euo pipefail

RETAIN_DAYS="${DUMP_RETAIN_DAYS:-30}"

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
key="dumps/$(date -u +%Y-%m-%d)/$(date -u +%H%M%S).dump"
url="s3://${S3_BUCKET}/${key}"

s5() {
    s5cmd --endpoint-url "$AWS_ENDPOINT" "$@"
}

sql() {
    psql -X -v ON_ERROR_STOP=1 -qtA -c "$1"
}

if ! pg_dump --format=custom | s5 pipe "$url"; then
    sql "INSERT INTO backup_runs (kind, started_at, finished_at, status)
         VALUES ('dump', '$started', now(), 'failed')" || true
    echo "dump failed" >&2
    exit 1
fi

size=$(s5 ls "$url" | awk '{ print $3 }')
sql "INSERT INTO backup_runs (kind, started_at, finished_at, status, object_key, size_bytes)
     VALUES ('dump', '$started', now(), 'success', '$url', ${size:-NULL})"
echo "dump $url finished (${size:-?} bytes)"

cutoff=$(date -u -d "$RETAIN_DAYS days ago" +%Y-%m-%d)
s5 ls "s3://${S3_BUCKET}/dumps/" 2>/dev/null | awk '$1 == "DIR" { print $2 }' | while read -r dir; do
    day="${dir%/}"
    if [[ "$day" < "$cutoff" ]]; then
        s5 rm "s3://${S3_BUCKET}/dumps/${day}/*" && echo "pruned dumps from $day"
    fi
done
