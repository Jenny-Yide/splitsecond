"""Steady write load for a disaster-recovery drill.

    python backup/drill_writer.py --interval 15 > writer.jsonl

Creates a dedicated group with three members, then commits one equal-split expense every
--interval seconds until stopped. After each commit it prints one JSON line with the write
number, the commit time, and the group's balances, so `verify_balances.py drill-report`
can tell exactly which writes a restored database contains.
"""

import argparse
import json
import os
import sys
import time
from uuid import uuid4

import psycopg

from backend.ledger.balances import get_group_balances
from backend.ledger.expenses import create_expense


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--count", type=int, default=0, help="stop after N writes (0 = forever)")
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        run = uuid4().hex[:8]
        users = [
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, 'drill') RETURNING id",
                (f"drill_{run}_{i}",),
            ).fetchone()[0]
            for i in range(3)
        ]
        group_id = conn.execute(
            "INSERT INTO groups (name) VALUES (%s) RETURNING id", (f"Restore drill {run}",)
        ).fetchone()[0]
        for user_id in users:
            conn.execute(
                "INSERT INTO group_members (group_id, user_id) VALUES (%s, %s)",
                (group_id, user_id),
            )
        print(f"drill group {group_id}", file=sys.stderr)

        n = 0
        while args.count == 0 or n < args.count:
            n += 1
            create_expense(
                conn, group_id, users[n % 3], 1000 + 7 * n, "equal", users, f"drill expense {n}"
            )
            committed_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
            balances = get_group_balances(conn, group_id)
            print(
                json.dumps(
                    {
                        "n": n,
                        "at": committed_at.isoformat(),
                        "groups": {
                            str(group_id): {str(u): c for u, c in sorted(balances.items())}
                        },
                    }
                ),
                flush=True,
            )
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
