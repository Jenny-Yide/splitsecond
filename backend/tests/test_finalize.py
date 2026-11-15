
import unittest
from uuid import uuid4

import psycopg

from backend.ledger.finalize import finalize_bill


class TestFinalize(unittest.TestCase):

    def setUp(self):
        self.conn = psycopg.connect("dbname=splitsecond_test_db")
        self.conn.execute("BEGIN")

        self.users = []

        for _ in range(3):
            uid = self.conn.execute(
                """
                INSERT INTO users (username, password_hash)
                VALUES (%s, 'test_hash')
                RETURNING id
                """,
                (f"finalize_test_{uuid4().hex}",),
            ).fetchone()[0]
            self.users.append(uid)

        self.group_id = self.conn.execute(
            """
            INSERT INTO groups (name)
            VALUES ('Finalize Test')
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

        self.items = []

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
            self.items.append(item_id)

        # Burger -> user 1
        # Steak -> user 2
        # Fries -> user 1 and user 3
        claims = [
            (self.items[0], self.users[0]),
            (self.items[1], self.users[1]),
            (self.items[2], self.users[0]),
            (self.items[2], self.users[2]),
        ]

        for item_id, uid in claims:
            self.conn.execute(
                """
                INSERT INTO item_claims (item_id, user_id)
                VALUES (%s, %s)
                """,
                (item_id, uid),
            )

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def test_successful_finalization(self):
        expense_id = finalize_bill(self.conn, self.bill_id)

        rows = self.conn.execute(
            """
            SELECT user_id, amount_cents
            FROM expense_shares
            WHERE expense_version_id = %s
            """,
            (expense_id,),
        ).fetchall()

        expected = {
            self.users[0]: 3300,
            self.users[1]: 4200,
            self.users[2]: 900,
        }

        self.assertEqual(dict(rows), expected)

        status = self.conn.execute(
            "SELECT status FROM bills WHERE id = %s",
            (self.bill_id,),
        ).fetchone()[0]

        self.assertEqual(status, "finalized")

    def test_double_finalization(self):
        first_id = finalize_bill(self.conn, self.bill_id)
        second_id = finalize_bill(self.conn, self.bill_id)

        self.assertEqual(first_id, second_id)

        count = self.conn.execute(
            """
            SELECT COUNT(*) FROM expenses
            WHERE id = %s
            """,
            (first_id,),
        ).fetchone()[0]

        self.assertEqual(count, 1)

    def test_unclaimed_item_rejected(self):
        self.conn.execute(
            "DELETE FROM item_claims WHERE item_id = %s",
            (self.items[0],),
        )

        with self.assertRaises(ValueError):
            finalize_bill(self.conn, self.bill_id)

        status = self.conn.execute(
            "SELECT status FROM bills WHERE id = %s",
            (self.bill_id,),
        ).fetchone()[0]

        self.assertEqual(status, "open")


if __name__ == "__main__":
    unittest.main()
