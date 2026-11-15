
import unittest
from uuid import uuid4

import psycopg

from backend.ledger.expenses import create_expense


class TestExpenseCreation(unittest.TestCase):

    def setUp(self):
        self.conn = psycopg.connect("dbname=splitsecond_test_db")
        self.conn.execute("BEGIN")

        self.user_ids = []

        for _ in range(3):
            user_id = self.conn.execute(
                """
                INSERT INTO users (username, password_hash)
                VALUES (%s, 'test_hash')
                RETURNING id
                """,
                (f"expense_test_{uuid4().hex}",),
            ).fetchone()[0]
            self.user_ids.append(user_id)

        self.group_id = self.conn.execute(
            """
            INSERT INTO groups (name)
            VALUES ('Expense Test Group')
            RETURNING id
            """
        ).fetchone()[0]

        for uid in self.user_ids:
            self.conn.execute(
                """
                INSERT INTO group_members (group_id, user_id)
                VALUES (%s, %s)
                """,
                (self.group_id, uid),
            )

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def check_expense(self, expense_id, expected):
        rows = self.conn.execute(
            """
            SELECT user_id, amount_cents
            FROM expense_shares
            WHERE expense_version_id = %s
            """,
            (expense_id,),
        ).fetchall()

        self.assertEqual(dict(rows), expected)

    def test_equal_expense(self):
        expense_id = create_expense(
            self.conn, self.group_id, self.user_ids[0],
            10000, "equal", self.user_ids, "Equal test",
        )

        self.check_expense(
            expense_id,
            dict(zip(self.user_ids, [3334, 3333, 3333])),
        )

    def test_exact_expense(self):
        amounts = dict(zip(self.user_ids, [5000, 3000, 2000]))

        expense_id = create_expense(
            self.conn, self.group_id, self.user_ids[0],
            10000, "exact", amounts, "Exact test",
        )

        self.check_expense(expense_id, amounts)

    def test_shares_expense(self):
        weights = dict(zip(self.user_ids, [2, 1, 1]))

        expense_id = create_expense(
            self.conn, self.group_id, self.user_ids[0],
            10000, "shares", weights, "Shares test",
        )

        self.check_expense(
            expense_id,
            dict(zip(self.user_ids, [5000, 2500, 2500])),
        )

    def test_nonmember_rejected(self):
        with self.assertRaises(ValueError):
            create_expense(
                self.conn, self.group_id, self.user_ids[0],
                10000, "equal",
                self.user_ids + [999999],
                "Invalid member",
            )


if __name__ == "__main__":
    unittest.main()
