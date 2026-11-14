
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg

from backend.ledger.finalize import finalize_bill


DB_NAME = "splitsecond_test_db"


class TestFinalizeConcurrency(unittest.TestCase):

    def setUp(self):
        self.conn = psycopg.connect(f"dbname={DB_NAME}")
        self.users = []
        self.group_id = None
        self.bill_id = None

        try:
            for _ in range(3):
                uid = self.conn.execute(
                    """
                    INSERT INTO users (username, password_hash)
                    VALUES (%s, 'test_hash')
                    RETURNING id
                    """,
                    (f"finalize_concurrent_{uuid4().hex}",),
                ).fetchone()[0]
                self.users.append(uid)

            self.group_id = self.conn.execute(
                """
                INSERT INTO groups (name)
                VALUES ('Finalize Concurrency Test')
                RETURNING id
                """
            ).fetchone()[0]

            for uid in self.users:
                self.conn.execute(
                    """
                    INSERT INTO group_members (group_id, user_id)
                    VALUES (%s, %s)
                    """,
                    (self.group_id, uid),
                )

            self.bill_id = self.conn.execute(
                """
                INSERT INTO bills (
                    group_id, payer_id, tax_cents, tip_cents
                )
                VALUES (%s, %s, 700, 700)
                RETURNING id
                """,
                (self.group_id, self.users[0]),
            ).fetchone()[0]

            items = []
            for name, price in [
                ("Burger", 2000),
                ("Steak", 3500),
                ("Fries", 1500),
            ]:
                item_id = self.conn.execute(
                    """
                    INSERT INTO bill_items (
                        bill_id, name, price_cents
                    )
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (self.bill_id, name, price),
                ).fetchone()[0]
                items.append(item_id)

            claims = [
                (items[0], self.users[0]),
                (items[1], self.users[1]),
                (items[2], self.users[0]),
                (items[2], self.users[2]),
            ]

            for item_id, uid in claims:
                self.conn.execute(
                    """
                    INSERT INTO item_claims (item_id, user_id)
                    VALUES (%s, %s)
                    """,
                    (item_id, uid),
                )

            # Other connections must see the fixtures.
            self.conn.commit()

        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def tearDown(self):
        try:
            self.conn.rollback()

            # Remove expense shares and official expense first.
            self.conn.execute(
                """
                DELETE FROM expense_shares
                WHERE expense_version_id IN (
                    SELECT expense_version_id
                    FROM bills WHERE id = %s
                )
                """,
                (self.bill_id,),
            )

            # Break the bill-to-expense reference.
            self.conn.execute(
                """
                UPDATE bills
                SET expense_version_id = NULL
                WHERE id = %s
                """,
                (self.bill_id,),
            )

            self.conn.execute(
                """
                DELETE FROM expenses
                WHERE group_id = %s
                """,
                (self.group_id,),
            )

            self.conn.execute(
                """
                DELETE FROM item_claims
                WHERE item_id IN (
                    SELECT id FROM bill_items WHERE bill_id = %s
                )
                """,
                (self.bill_id,),
            )

            self.conn.execute(
                "DELETE FROM bill_items WHERE bill_id = %s",
                (self.bill_id,),
            )
            self.conn.execute(
                "DELETE FROM bills WHERE id = %s",
                (self.bill_id,),
            )
            self.conn.execute(
                "DELETE FROM group_members WHERE group_id = %s",
                (self.group_id,),
            )
            self.conn.execute(
                "DELETE FROM groups WHERE id = %s",
                (self.group_id,),
            )
            self.conn.execute(
                "DELETE FROM users WHERE id = ANY(%s)",
                (self.users,),
            )

            self.conn.commit()

        finally:
            self.conn.close()

    def test_concurrent_finalization(self):
        barrier = Barrier(2)

        def worker():
            with psycopg.connect(
                f"dbname={DB_NAME}",
                autocommit=True,
            ) as conn:
                barrier.wait(timeout=10)
                return finalize_bill(conn, self.bill_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [f.result(timeout=15) for f in futures]

        # Both requests must return the same expense ID.
        self.assertEqual(results[0], results[1])

        row = self.conn.execute(
            """
            SELECT status, expense_version_id
            FROM bills WHERE id = %s
            """,
            (self.bill_id,),
        ).fetchone()

        self.assertEqual(row[0], "finalized")
        self.assertEqual(row[1], results[0])

        count = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM expenses WHERE group_id = %s
            """,
            (self.group_id,),
        ).fetchone()[0]

        self.assertEqual(count, 1)

        # Check all shares, including the grand total.
        shares = dict(self.conn.execute(
            """
            SELECT user_id, amount_cents
            FROM expense_shares
            WHERE expense_version_id = %s
            """,
            (results[0],),
        ).fetchall())

        self.assertEqual(
            shares,
            {
                self.users[0]: 3300,
                self.users[1]: 4200,
                self.users[2]: 900,
            },
        )
        self.assertEqual(sum(shares.values()), 8400)


if __name__ == "__main__":
    unittest.main()
