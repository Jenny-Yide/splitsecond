"""Connection pool and request transactions for the API.

The ledger functions take a plain psycopg connection and open their own
`conn.transaction()`. Each request runs inside an outer transaction (see
`tx()`), so a ledger call becomes a savepoint and its NOTIFY commits or rolls
back together with it.
"""
import json
import os
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

NOTIFY_CHANNEL = "splitsecond"

pool: ConnectionPool | None = None


def open_pool(dsn: str) -> ConnectionPool:
    global pool
    pool = ConnectionPool(
        dsn,
        min_size=1,
        max_size=int(os.environ.get("DB_POOL_MAX", "10")),
        # Hand out only live connections, so a PostgreSQL restart doesn't leave
        # the pool returning dead ones (the replica would stay unready forever).
        check=ConnectionPool.check_connection,
        max_lifetime=1800,
        open=False,
    )
    pool.open(wait=True, timeout=30)
    return pool


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None


@contextmanager
def tx():
    """One request = one transaction: commit on success, roll back on error.

    Rows come back as tuples, which is what the ledger functions expect;
    use `rows()`/`row()` below for dicts.
    """
    with pool.connection() as conn:
        with conn.transaction():
            yield conn


def rows(conn, sql, params=None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def row(conn, sql, params=None) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def notify(conn, group_id: int, event: str, **extra) -> None:
    """Queue a realtime event. PostgreSQL delivers it only when the transaction
    commits, so clients never hear about a rolled-back change. Payloads are
    capped at 8000 bytes, so send IDs only; clients re-fetch over HTTP."""
    payload = json.dumps({"group_id": group_id, "type": event, **extra}, default=str)
    conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, payload))
