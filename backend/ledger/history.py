
from datetime import datetime


def get_expenses_at(conn, group_id: int, at_time: datetime):
    """
    Get expense versions active at a given historical time.
    """
    if at_time.tzinfo is None or at_time.utcoffset() is None:
        raise ValueError("at_time must include a timezone")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                id,
                expense_id,
                version,
                payer_id,
                total_cents,
                description,
                created_at,
                voided_at
            FROM expenses
            WHERE group_id = %s
              AND created_at <= %s
              AND (
                  voided_at IS NULL
                  OR voided_at > %s
              )
            ORDER BY created_at, id
            """,
            (group_id, at_time, at_time),
        )

        return cur.fetchall()



def get_balances_at(conn, group_id: int, at_time: datetime) -> dict[int, int]:
    """
    Calculate group balances at a historical time.
    """

    if at_time.tzinfo is None or at_time.utcoffset() is None:
        raise ValueError("at_time must include a timezone")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_id
            FROM group_members
            WHERE group_id = %s
            """,
            (group_id,),
        )
        balances = {row[0]: 0 for row in cur.fetchall()}

        cur.execute(
            """
            SELECT
                e.payer_id,
                e.total_cents,
                s.user_id,
                s.amount_cents
            FROM expenses e
            JOIN expense_shares s
              ON s.expense_version_id = e.id
            WHERE e.group_id = %s
              AND e.created_at <= %s
              AND (
                  e.voided_at IS NULL
                  OR e.voided_at > %s
              )
            """,
            (group_id, at_time, at_time),
        )

        for payer_id, total_cents, user_id, amount in cur.fetchall():
            # Each expense may have multiple share rows.
            # Subtract each share once.
            balances[user_id] -= amount

            # Only credit the payer's total once per expense.
            # This is handled below instead of inside this loop.

        cur.execute(
            """
            SELECT payer_id, total_cents
            FROM expenses
            WHERE group_id = %s
              AND created_at <= %s
              AND (
                  voided_at IS NULL
                  OR voided_at > %s
              )
            """,
            (group_id, at_time, at_time),
        )

        for payer_id, total_cents in cur.fetchall():
            balances[payer_id] += total_cents


        # Apply settlements recorded by the historical time.
        cur.execute(
            """
            SELECT from_user_id, to_user_id, amount_cents
            FROM settlements
            WHERE group_id = %s
              AND created_at <= %s
            """,
            (group_id, at_time),
        )

        for from_user, to_user, amount in cur.fetchall():
            balances[from_user] += amount
            balances[to_user] -= amount

    return balances
