#!/bin/sh
# Creates the database the ledger tests connect to (they hardcode dbname=splitsecond_test_db).
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -c "CREATE DATABASE splitsecond_test_db"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname splitsecond_test_db \
    -f /docker-entrypoint-initdb.d/10-schema.sql
