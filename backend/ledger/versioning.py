
from uuid import uuid4

from backend.ledger.split import split_equally


class VersionConflictError(Exception):
    """The expense was changed by another request."""


def update_equal_expense(
    conn,
    expense_id,
    expected_version: int,
    total_cents: int,
    user_ids: list[int],
    description: str,
):
    """Create a new version without deleting the old one."""

    shares = split_equally(total_cents, user_ids)

    with conn.transaction():
        with conn.cursor() as cur:
            # Lock the logical expense's current version.
            cur.execute(
                """
                SELECT id, group_id, payer_id, version
                FROM expenses
                WHERE expense_id = %s
                  AND voided_at IS NULL
                FOR UPDATE
                """,
                (expense_id,),
            )
            old = cur.fetchone()

            if old is None:
                raise VersionConflictError(
                    "Expense no longer has an active version"
                )

            old_id, group_id, payer_id, current_version = old

            if current_version != expected_version:
                raise VersionConflictError(
                    "Expense has already been modified"
                )

            # Validate all participants belong to this group.
            required_ids = set(user_ids) | {payer_id}
            cur.execute(
                """
                SELECT user_id
                FROM group_members
                WHERE group_id = %s AND user_id = ANY(%s)
                """,
                (group_id, list(required_ids)),
            )
            members = {row[0] for row in cur.fetchall()}

            if members != required_ids:
                raise ValueError("Some users are not group members")

            # Void the old version.
            cur.execute(
                """
                UPDATE expenses
                SET voided_at = NOW(),
                    void_reason = 'updated'
                WHERE id = %s
                """,
                (old_id,),
            )

            # Insert the new version.
            cur.execute(
                """
                INSERT INTO expenses (
                    expense_id, group_id, payer_id,
                    total_cents, description, version,
                    idempotency_key
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    expense_id, group_id, payer_id,
                    total_cents, description,
                    current_version + 1, uuid4(),
                ),
            )
            new_id = cur.fetchone()[0]

            for user_id, amount in shares.items():
                cur.execute(
                    """
                    INSERT INTO expense_shares (
                        expense_version_id, user_id, amount_cents
                    )
                    VALUES (%s, %s, %s)
                    """,
                    (new_id, user_id, amount),
                )

    return new_id



def delete_expense(conn, expense_id, expected_version: int):
    """Soft-delete an expense while keeping its history."""

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, version
                FROM expenses
                WHERE expense_id = %s
                  AND voided_at IS NULL
                FOR UPDATE
                """,
                (expense_id,),
            )

            row = cur.fetchone()

            if row is None:
                raise VersionConflictError(
                    "Expense is already deleted or missing"
                )

            current_id, current_version = row

            if current_version != expected_version:
                raise VersionConflictError(
                    "Expense version conflict"
                )

            cur.execute(
                """
                UPDATE expenses
                SET voided_at = NOW(),
                    void_reason = 'deleted'
                WHERE id = %s
                """,
                (current_id,),
            )

    return current_id



def restore_expense(conn, expense_id):
    """Restore a deleted expense as a new version."""

    with conn.transaction():
        with conn.cursor() as cur:
            # Serialize operations on this logical expense.
            cur.execute(
                """
                SELECT id, version, group_id, payer_id,
                       total_cents, description, void_reason
                FROM expenses
                WHERE expense_id = %s
                ORDER BY version DESC
                LIMIT 1
                FOR UPDATE
                """,
                (expense_id,),
            )

            old = cur.fetchone()

            if old is None:
                raise ValueError("Expense not found")

            (old_id, version, group_id, payer_id,
             total, description, reason) = old

            if reason != "deleted":
                raise ValueError("Expense is not deleted")

            # Check that no active version exists.
            cur.execute(
                """
                SELECT id FROM expenses
                WHERE expense_id = %s
                  AND voided_at IS NULL
                """,
                (expense_id,),
            )

            if cur.fetchone() is not None:
                raise VersionConflictError(
                    "Expense already has an active version"
                )

            # Copy the old shares.
            cur.execute(
                """
                SELECT user_id, amount_cents
                FROM expense_shares
                WHERE expense_version_id = %s
                """,
                (old_id,),
            )

            shares = cur.fetchall()

            if not shares or sum(a for _, a in shares) != total:
                raise ValueError("Invalid historical shares")

            cur.execute(
                """
                INSERT INTO expenses (
                    expense_id, group_id, payer_id,
                    total_cents, description, version,
                    idempotency_key
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    expense_id, group_id, payer_id,
                    total, description, version + 1, uuid4(),
                ),
            )

            new_id = cur.fetchone()[0]

            for user_id, amount in shares:
                cur.execute(
                    """
                    INSERT INTO expense_shares (
                        expense_version_id,
                        user_id,
                        amount_cents
                    )
                    VALUES (%s, %s, %s)
                    """,
                    (new_id, user_id, amount),
                )

    return new_id
