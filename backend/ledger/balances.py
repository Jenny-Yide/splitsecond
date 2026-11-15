
def get_group_balances(conn, group_id: int) -> dict[int, int]:
    """
    Calculate balances from expenses and settlements.

    Positive: should receive money.
    Negative: owes money.
    """

    with conn.cursor() as cur:
        # 1. Initialize balances
        cur.execute(
            """
            SELECT user_id
            FROM group_members
            WHERE group_id = %s
            """,
            (group_id,),
        )

        balances = {row[0]: 0 for row in cur.fetchall()}

        # 2. Calculate active expenses
        cur.execute(
            """
            SELECT id, payer_id, total_cents
            FROM expenses
            WHERE group_id = %s
              AND voided_at IS NULL
            """,
            (group_id,),
        )

        expenses = cur.fetchall()

        for expense_id, payer_id, total_cents in expenses:
            balances[payer_id] += total_cents

            cur.execute(
                """
                SELECT user_id, amount_cents
                FROM expense_shares
                WHERE expense_version_id = %s
                """,
                (expense_id,),
            )

            for user_id, amount in cur.fetchall():
                balances[user_id] -= amount

        # 3. Apply settlements
        cur.execute(
            """
            SELECT from_user_id, to_user_id, amount_cents
            FROM settlements
            WHERE group_id = %s
            """,
            (group_id,),
        )

        for from_user, to_user, amount in cur.fetchall():
            balances[from_user] += amount
            balances[to_user] -= amount

    return balances
