"""Snapshot and verify group balances using the ledger's own balance functions.

    python backup/verify_balances.py snapshot [--at 2026-11-16T01:30:00Z] > expected.json
    python backup/verify_balances.py check expected.json        # '-' reads stdin
    python backup/verify_balances.py drill-report writer.jsonl  # see backup/drill_writer.py

Connects with DATABASE_URL. `check` and `drill-report` exit 1 on any mismatch.
"""

import argparse
import json
import os
import sys
from datetime import datetime

import psycopg

from backend.ledger.balances import get_group_balances
from backend.ledger.history import get_balances_at


def as_json(balances):
    return {str(user_id): cents for user_id, cents in sorted(balances.items())}


def snapshot(conn, at):
    if at is None:
        at = conn.execute("SELECT now()").fetchone()[0]
        group_ids = [row[0] for row in conn.execute("SELECT id FROM groups ORDER BY id")]
        groups = {gid: get_group_balances(conn, gid) for gid in group_ids}
    else:
        group_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM groups WHERE created_at <= %s ORDER BY id", (at,)
            )
        ]
        groups = {gid: get_balances_at(conn, gid, at) for gid in group_ids}
    return {"at": at.isoformat(), "groups": {str(g): as_json(b) for g, b in groups.items()}}


def compare(conn, expected):
    mismatches = []
    for group_id, want in expected["groups"].items():
        got = as_json(get_group_balances(conn, int(group_id)))
        if got != want:
            mismatches.append((group_id, want, got))
    return mismatches


def check(conn, expected):
    mismatches = compare(conn, expected)
    print(
        f"checked {len(expected['groups'])} groups against the snapshot at {expected['at']}: "
        f"{len(mismatches)} mismatched"
    )
    for group_id, want, got in mismatches:
        print(f"  group {group_id}: expected {want}, got {got}")
    return 1 if mismatches else 0


def drill_report(conn, lines):
    """Match the restored drill group to the last write it contains, then verify its balances."""
    writes = [json.loads(line) for line in lines if line.strip()]
    if not writes:
        print("drill log is empty")
        return 1
    group_id = next(iter(writes[0]["groups"]))
    restored = conn.execute(
        "SELECT count(*) FROM expenses WHERE group_id = %s", (int(group_id),)
    ).fetchone()[0]
    last = writes[-1]
    print(f"drill group {group_id}: {last['n']} expenses written, {restored} restored")

    if restored == 0:
        print("  no drill expenses survived")
        return 1
    match = next((w for w in writes if w["n"] == restored), None)
    if match is None:
        print("  restored count matches no write in the log")
        return 1

    lost_seconds = (
        datetime.fromisoformat(last["at"]) - datetime.fromisoformat(match["at"])
    ).total_seconds()
    print(f"  last restored write committed at {match['at']}")
    print(f"  last write before the disaster at  {last['at']}")
    print(f"  lost: {last['n'] - restored} expenses, {lost_seconds:.0f} seconds of writes")
    return check(conn, match)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="print every group's balances as JSON")
    snap.add_argument("--at", type=datetime.fromisoformat, help="historical time with timezone")
    chk = sub.add_parser("check", help="compare current balances with a snapshot")
    chk.add_argument("expected", type=argparse.FileType("r"))
    drill = sub.add_parser("drill-report", help="verify a restore against a drill_writer log")
    drill.add_argument("log", type=argparse.FileType("r"))
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if args.command == "snapshot":
            json.dump(snapshot(conn, args.at), sys.stdout, indent=2)
            print()
            return 0
        if args.command == "check":
            return check(conn, json.load(args.expected))
        return drill_report(conn, args.log)


if __name__ == "__main__":
    sys.exit(main())
