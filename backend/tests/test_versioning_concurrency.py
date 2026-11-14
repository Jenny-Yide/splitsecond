
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg

from backend.ledger.expenses import create_equal_expense
from backend.ledger.versioning import (
    update_equal_expense,
    VersionConflictError,
)


DB_NAME = "splitsecond_test_db"


class TestVersioningConcurrency(unittest.TestCase):

    def setUp(self):
        self.conn = psycopg.connect(f"dbname={DB_NAME}")
        self.users = []

        try:
            for _ in range(3):
                uid = self.conn.execute(
                    """
                    INSERT INTO users (username, password_hash)
                    VALUES (%s, 'test_hash')
                    RETURNING id
                    """,
                    (f"version_concurrent_{uuid4().hex}",),
                ).fetchone()[0]
                self.users.append(uid)

            self.group_id = self.conn.execute(
                """
                INSERT INTO groups (name)
                VALUES ('Version Concurrency Test')
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

            original_id = create_equal_expense(
                self.conn,
                self.group_id,
                self.users[0],
                10000,
                self.users,
                "Original expense",
            )

            self.expense_uuid = self.conn.execute(
                """
                SELECT expense_id FROM expenses
                WHERE id = %s
                """,
                (original_id,),
            ).fetchone()[0]

            # Make fixtures visible to other connections.
            self.conn.commit()

        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def tearDown(self):
        try:
            self.conn.rollback()

            # Remove shares before expenses because of FK.
            self.conn.execute(
                """
                DELETE FROM expense_shares
                WHERE expense_version_id IN (
                    SELECT id FROM expenses
                    WHERE expense_id = %s
                )
                """,
                (self.expense_uuid,),
            )

            self.conn.execute(
                "DELETE FROM expenses WHERE expense_id = %s",
                (self.expense_uuid,),
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

    def test_concurrent_updates(self):
        barrier = Barrier(2)

        def worker(new_total):
            with psycopg.connect(
                f"dbname={DB_NAME}",
                autocommit=True,
            ) as conn:
                barrier.wait(timeout=10)

                try:
                    update_equal_expense(
                        conn=conn,
                        expense_id=self.expense_uuid,
                        expected_version=1,
                        total_cents=new_total,
                        user_ids=self.users,
                        description=f"Updated to {new_total}",
                    )
                    return "success"

                except VersionConflictError:
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(worker, 12000),
                pool.submit(worker, 15000),
            ]
            results = [f.result(timeout=15) for f in futures]

        self.assertEqual(sorted(results), ["conflict", "success"])

        rows = self.conn.execute(
            """
            SELECT version, total_cents, void_reason, voided_at
            FROM expenses
            WHERE expense_id = %s
            ORDER BY version
            """,
            (self.expense_uuid,),
        ).fetchall()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[0][2], "updated")
        self.assertIsNotNone(rows[0][3])

        self.assertEqual(rows[1][0], 2)
        self.assertIn(rows[1][1], (12000, 15000))
        self.assertIsNone(rows[1][3])


if __name__ == "__main__":
    unittest.main()
