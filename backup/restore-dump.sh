#!/bin/bash
# Restores a pg_dump from object storage into a database, creating it if needed.
# Existing objects in the target are dropped and recreated, all in one transaction.
#
#   restore-dump.sh latest restore_check
#   restore-dump.sh s3://splitsecond-backups/dumps/2026-11-16/030000.dump restore_check
#
# Connection settings come from the standard PG* variables, storage settings from AWS_* and S3_BUCKET.
set -euo pipefail

usage="usage: restore-dump.sh <s3-url|latest> <database>"
src="${1:?$usage}"
db="${2:?$usage}"

s5() {
    s5cmd --endpoint-url "$AWS_ENDPOINT" "$@"
}

if [[ "$src" == latest ]]; then
    newest=$(s5 ls "s3://${S3_BUCKET}/dumps/*" | awk '{ print $NF }' | sort | tail -1)
    if [[ -z "$newest" ]]; then
        echo "no dumps found in s3://${S3_BUCKET}/dumps/" >&2
        exit 1
    fi
    src="s3://${S3_BUCKET}/dumps/${newest}"
fi

if ! psql -X -qtA -d postgres -c "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1; then
    createdb "$db"
fi

echo "restoring $src into database $db"
s5 cat "$src" | pg_restore --clean --if-exists --no-owner --single-transaction --exit-on-error -d "$db"
echo "restore finished"
