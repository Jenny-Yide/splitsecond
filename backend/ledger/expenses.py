from backend.ledger.split import split_exact, split_by_shares
from uuid import uuid4

import psycopg

from backend.ledger.split import split_equally


def create_equal_expense(
    conn,
    group_id: int,
    payer_id: int,
    total_cents: int,
    user_ids: list[int],
    description: str,
):
    """Create an equally split expense in PostgreSQL."""

    shares = split_equally(total_cents, user_ids)
    expense_uuid = uuid4()

    with conn.transaction():
        # Check that the payer and participants belong to the group.
        required_ids = set(user_ids) | {payer_id}

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id
                FROM group_members
                WHERE group_id = %s
                  AND user_id = ANY(%s)
                """,
                (group_id, list(required_ids)),
            )
            members = {row[0] for row in cur.fetchall()}

            if members != required_ids:
                raise ValueError("Some users are not group members")

            # Save the expense.
            cur.execute(
                """
                INSERT INTO expenses (
                    expense_id, group_id, payer_id,
                    total_cents, description, version,
                    idempotency_key
                )
                VALUES (%s, %s, %s, %s, %s, 1, %s)
                RETURNING id
                """,
                (
                    expense_uuid, group_id, payer_id,
                    total_cents, description, uuid4(),
                ),
            )
            expense_version_id = cur.fetchone()[0]

            # Save each person's share.
            for user_id, amount in shares.items():
                cur.execute(
                    """
                    INSERT INTO expense_shares (
                        expense_version_id,
                        user_id,
                        amount_cents
                    )
                    VALUES (%s, %s, %s)
                    """,
                    (expense_version_id, user_id, amount),
                )

    return expense_version_id



def create_expense(
    conn,
    group_id: int,
    payer_id: int,
    total_cents: int,
    split_mode: str,
    split_data,
    description: str,
):
    """Create an expense using equal, exact, or shares split."""

    # 1. Calculate the shares.
    if split_mode == "equal":
        shares = split_equally(total_cents, split_data)
    elif split_mode == "exact":
        shares = split_exact(total_cents, split_data)
    elif split_mode == "shares":
        shares = split_by_shares(total_cents, split_data)
    else:
        raise ValueError("Unsupported split mode")

    expense_uuid = uuid4()

    # 2. Save everything in one transaction.
    with conn.transaction():
        with conn.cursor() as cur:

            # Verify payer and participants are in the group.
            required_ids = set(shares) | {payer_id}

            cur.execute(
                """
                SELECT user_id
                FROM group_members
                WHERE group_id = %s
                  AND user_id = ANY(%s)
                """,
                (group_id, list(required_ids)),
            )

            members = {row[0] for row in cur.fetchall()}

            if members != required_ids:
                raise ValueError("Some users are not group members")

            # 3. Create the expense.
            cur.execute(
                """
                INSERT INTO expenses (
                    expense_id,
                    group_id,
                    payer_id,
                    total_cents,
                    description,
                    version,
                    idempotency_key
                )
                VALUES (%s, %s, %s, %s, %s, 1, %s)
                RETURNING id
                """,
                (
                    expense_uuid,
                    group_id,
                    payer_id,
                    total_cents,
                    description,
                    uuid4(),
                ),
            )

            expense_version_id = cur.fetchone()[0]

            # 4. Save everyone's share.
            for user_id, amount in shares.items():
                cur.execute(
                    """
                    INSERT INTO expense_shares (
                        expense_version_id,
                        user_id,
                        amount_cents
                    )
                    VALUES (%s, %s, %s)
                    """,
                    (expense_version_id, user_id, amount),
                )

    return expense_version_id
