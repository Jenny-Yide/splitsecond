
import unittest
from uuid import uuid4

import psycopg

from backend.ledger.expenses import create_equal_expense
from backend.ledger.versioning import (
    update_equal_expense,
    VersionConflictError,
)


class TestVersioning(unittest.TestCase):

    def setUp(self):
        self.conn = psycopg.connect("dbname=splitsecond_test_db")

        # Start a transaction that will be rolled back.
        self.conn.execute("BEGIN")

        # Create test users.
        self.user_ids = []

        for i in range(3):
            user_id = self.conn.execute(
                """
                INSERT INTO users (username, password_hash)
                VALUES (%s, 'test_hash')
                RETURNING id
                """,
                (f"version_test_{uuid4().hex}",),
            ).fetchone()[0]

            self.user_ids.append(user_id)

        # Create a test group.
        self.group_id = self.conn.execute(
            """
            INSERT INTO groups (name)
            VALUES ('Version Test Group')
            RETURNING id
            """
        ).fetchone()[0]

        for user_id in self.user_ids:
            self.conn.execute(
                """
                INSERT INTO group_members (group_id, user_id)
                VALUES (%s, %s)
                """,
                (self.group_id, user_id),
            )

        self.expense_version_id = create_equal_expense(
            conn=self.conn,
            group_id=self.group_id,
            payer_id=self.user_ids[0],
            total_cents=10000,
            user_ids=self.user_ids,
            description="Version test expense",
        )

        self.expense_uuid = self.conn.execute(
            """
            SELECT expense_id
            FROM expenses
            WHERE id = %s
            """,
            (self.expense_version_id,),
        ).fetchone()[0]

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def test_update_creates_new_version(self):
        new_id = update_equal_expense(
            conn=self.conn,
            expense_id=self.expense_uuid,
            expected_version=1,
            total_cents=12000,
            user_ids=self.user_ids,
            description="Updated version",
        )

        rows = self.conn.execute(
            """
            SELECT version, total_cents, voided_at
            FROM expenses
            WHERE expense_id = %s
            ORDER BY version
            """,
            (self.expense_uuid,),
        ).fetchall()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[0][1], 10000)
        self.assertIsNotNone(rows[0][2])

        self.assertEqual(rows[1][0], 2)
        self.assertEqual(rows[1][1], 12000)
        self.assertIsNone(rows[1][2])
        self.assertNotEqual(new_id, self.expense_version_id)

    def test_stale_version_rejected(self):
        update_equal_expense(
            conn=self.conn,
            expense_id=self.expense_uuid,
            expected_version=1,
            total_cents=12000,
            user_ids=self.user_ids,
            description="First update",
        )

        with self.assertRaises(VersionConflictError):
            update_equal_expense(
                conn=self.conn,
                expense_id=self.expense_uuid,
                expected_version=1,
                total_cents=15000,
                user_ids=self.user_ids,
                description="Stale update",
            )

    def test_nonexistent_expense_rejected(self):
        with self.assertRaises(VersionConflictError):
            update_equal_expense(
                conn=self.conn,
                expense_id=uuid4(),
                expected_version=1,
                total_cents=15000,
                user_ids=self.user_ids,
                description="Missing expense",
            )


if __name__ == "__main__":
    unittest.main()
