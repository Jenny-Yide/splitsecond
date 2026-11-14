
from uuid import UUID


def record_settlement(
    conn,
    group_id: int,
    from_user_id: int,
    to_user_id: int,
    amount_cents: int,
    idempotency_key: UUID | None = None,
):
    """Record a settlement without duplicating retries."""

    if type(amount_cents) is not int or amount_cents <= 0:
        raise ValueError("Amount must be positive integer cents")

    if from_user_id == to_user_id:
        raise ValueError("Cannot pay yourself")

    if idempotency_key is not None and not isinstance(
        idempotency_key, UUID
    ):
        raise ValueError("Invalid idempotency key")

    with conn.transaction():
        with conn.cursor() as cur:

            # Verify both users belong to the group.
            cur.execute(
                """
                SELECT user_id
                FROM group_members
                WHERE group_id = %s
                  AND user_id = ANY(%s)
                """,
                (group_id, [from_user_id, to_user_id]),
            )

            members = {row[0] for row in cur.fetchall()}

            if members != {from_user_id, to_user_id}:
                raise ValueError("Users must belong to the group")

            # Insert unless the idempotency key already exists.
            cur.execute(
                """
                INSERT INTO settlements (
                    group_id,
                    from_user_id,
                    to_user_id,
                    amount_cents,
                    idempotency_key
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                (
                    group_id,
                    from_user_id,
                    to_user_id,
                    amount_cents,
                    idempotency_key,
                ),
            )

            row = cur.fetchone()

            if row is not None:
                return row[0]

            # A record with this key already exists.
            cur.execute(
                """
                SELECT id, group_id, from_user_id,
                       to_user_id, amount_cents
                FROM settlements
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            )

            existing = cur.fetchone()

            if existing is None:
                raise RuntimeError("Settlement conflict")

            existing_id, g, sender, receiver, amount = existing

            if (
                g != group_id
                or sender != from_user_id
                or receiver != to_user_id
                or amount != amount_cents
            ):
                raise ValueError(
                    "Idempotency key reused with different data"
                )

            return existing_id
