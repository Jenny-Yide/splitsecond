
from uuid import uuid4


def allocate_cents(total_cents, weights):
    """
    Allocate cents proportionally using the
    largest-remainder method.
    """
    if not weights or any(w < 0 for w in weights.values()):
        raise ValueError("Invalid weights")

    weight_sum = sum(weights.values())

    if weight_sum <= 0 or total_cents < 0:
        raise ValueError("Invalid allocation")

    result = {}
    remainders = []

    for uid, weight in weights.items():
        numerator = total_cents * weight
        base, remainder = divmod(numerator, weight_sum)

        result[uid] = base
        remainders.append((remainder, uid))

    remaining = total_cents - sum(result.values())

    # Largest remainder first; user ID breaks ties.
    remainders.sort(key=lambda x: (-x[0], x[1]))

    for _, uid in remainders[:remaining]:
        result[uid] += 1

    return result


def finalize_bill(conn, bill_id):
    """
    Convert a fully claimed bill into an expense.
    All database changes happen in one transaction.
    """

    with conn.transaction():
        with conn.cursor() as cur:

            # Lock the bill to prevent double finalization.
            cur.execute(
                """
                SELECT group_id, payer_id, tax_cents,
                       tip_cents, status, expense_version_id
                FROM bills
                WHERE id = %s
                FOR UPDATE
                """,
                (bill_id,),
            )

            bill = cur.fetchone()

            if bill is None:
                raise ValueError("Bill not found")

            group_id, payer_id, tax, tip, status, existing = bill

            if status == "finalized":
                return existing

            if status != "open":
                raise ValueError("Bill is not open")

            # Load all items and claims.
            cur.execute(
                """
                SELECT
                    i.id, i.price_cents, c.user_id
                FROM bill_items i
                LEFT JOIN item_claims c
                  ON i.id = c.item_id
                WHERE i.bill_id = %s
                ORDER BY i.id, c.user_id
                """,
                (bill_id,),
            )

            items = {}

            for item_id, price, uid in cur.fetchall():
                if item_id not in items:
                    items[item_id] = {
                        "price": price,
                        "claimants": [],
                    }

                if uid is not None:
                    items[item_id]["claimants"].append(uid)

            if not items:
                raise ValueError("Bill has no items")

            if any(
                not item["claimants"]
                for item in items.values()
            ):
                raise ValueError("Some items are unclaimed")

            # Verify group membership.
            participants = {
                uid
                for item in items.values()
                for uid in item["claimants"]
            }
            participants.add(payer_id)

            cur.execute(
                """
                SELECT user_id
                FROM group_members
                WHERE group_id = %s
                  AND user_id = ANY(%s)
                """,
                (group_id, list(participants)),
            )

            members = {row[0] for row in cur.fetchall()}

            if members != participants:
                raise ValueError("Invalid group member")

            # Split each item equally among claimants.
            subtotals = {}

            for item in items.values():
                claimants = sorted(item["claimants"])
                price = item["price"]

                base, remainder = divmod(
                    price, len(claimants)
                )

                for i, uid in enumerate(claimants):
                    amount = base + (1 if i < remainder else 0)
                    subtotals[uid] = (
                        subtotals.get(uid, 0) + amount
                    )

            # Allocate tax and tip proportionally.
            tax_shares = allocate_cents(tax, subtotals)
            tip_shares = allocate_cents(tip, subtotals)

            shares = {
                uid: subtotal
                + tax_shares[uid]
                + tip_shares[uid]
                for uid, subtotal in subtotals.items()
            }

            total = (
                sum(item["price"] for item in items.values())
                + tax + tip
            )

            if sum(shares.values()) != total:
                raise ValueError("Shares do not match total")

            # Create the official expense.
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
                    uuid4(), group_id, payer_id,
                    total, "Finalized bill", uuid4(),
                ),
            )

            expense_version_id = cur.fetchone()[0]

            for uid, amount in shares.items():
                cur.execute(
                    """
                    INSERT INTO expense_shares (
                        expense_version_id,
                        user_id,
                        amount_cents
                    )
                    VALUES (%s, %s, %s)
                    """,
                    (expense_version_id, uid, amount),
                )

            cur.execute(
                """
                UPDATE bills
                SET status = 'finalized',
                    expense_version_id = %s
                WHERE id = %s
                """,
                (expense_version_id, bill_id),
            )

    return expense_version_id
