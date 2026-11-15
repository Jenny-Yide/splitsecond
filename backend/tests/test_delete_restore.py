
import unittest
from uuid import uuid4

import psycopg

from backend.ledger.expenses import create_equal_expense
from backend.ledger.versioning import (
    delete_expense,
    restore_expense,
    VersionConflictError,
)
from backend.ledger.balances import get_group_balances


class TestDeleteRestore(unittest.TestCase):

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
                (f"delete_test_{uuid4().hex}",),
            ).fetchone()[0]

            self.users.append(uid)

        self.group_id = self.conn.execute(
            """
            INSERT INTO groups (name)
            VALUES ('Delete Restore Test')
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

        self.original_id = create_equal_expense(
            self.conn,
            self.group_id,
            self.users[0],
            10000,
            self.users,
            "Delete restore test",
        )

        self.expense_uuid = self.conn.execute(
            """
            SELECT expense_id
            FROM expenses
            WHERE id = %s
            """,
            (self.original_id,),
        ).fetchone()[0]

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def test_delete_and_restore(self):
        # Delete version 1.
        delete_expense(
            self.conn,
            self.expense_uuid,
            expected_version=1,
        )

        old = self.conn.execute(
            """
            SELECT void_reason, voided_at
            FROM expenses WHERE id = %s
            """,
            (self.original_id,),
        ).fetchone()

        self.assertEqual(old[0], "deleted")
        self.assertIsNotNone(old[1])

        # A deleted expense no longer affects balances.
        balances = get_group_balances(self.conn, self.group_id)
        self.assertTrue(all(v == 0 for v in balances.values()))

        # Restore as version 2.
        restored_id = restore_expense(
            self.conn,
            self.expense_uuid,
        )

        self.assertNotEqual(restored_id, self.original_id)

        version, total, voided_at = self.conn.execute(
            """
            SELECT version, total_cents, voided_at
            FROM expenses WHERE id = %s
            """,
            (restored_id,),
        ).fetchone()

        self.assertEqual(version, 2)
        self.assertEqual(total, 10000)
        self.assertIsNone(voided_at)

        # Balances are restored.
        balances = get_group_balances(self.conn, self.group_id)
        expected = dict(zip(self.users, [6666, -3333, -3333]))
        self.assertEqual(balances, expected)

    def test_stale_delete_rejected(self):
        with self.assertRaises(VersionConflictError):
            delete_expense(
                self.conn,
                self.expense_uuid,
                expected_version=99,
            )

    def test_restore_active_expense_rejected(self):
        with self.assertRaises(ValueError):
            restore_expense(
                self.conn,
                self.expense_uuid,
            )


if __name__ == "__main__":
    unittest.main()
