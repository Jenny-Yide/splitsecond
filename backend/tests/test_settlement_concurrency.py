
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg

from backend.ledger.settlements import record_settlement


DB_NAME = "splitsecond_test_db"


class TestSettlementConcurrency(unittest.TestCase):

    def setUp(self):
        # Create committed fixtures so both connections can see them.
        self.conn = psycopg.connect(f"dbname={DB_NAME}")
        self.users = []

        try:
            for _ in range(2):
                uid = self.conn.execute(
                    """
                    INSERT INTO users (username, password_hash)
                    VALUES (%s, 'test_hash')
                    RETURNING id
                    """,
                    (f"concurrency_{uuid4().hex}",),
                ).fetchone()[0]

                self.users.append(uid)

            self.group_id = self.conn.execute(
                """
                INSERT INTO groups (name)
                VALUES ('Concurrency Test')
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

            self.conn.commit()

        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def tearDown(self):
        # Clean up committed test records.
        try:
            self.conn.rollback()

            self.conn.execute(
                "DELETE FROM settlements WHERE group_id = %s",
                (self.group_id,),
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

    def test_same_key_concurrent_requests(self):
        key = uuid4()
        barrier = Barrier(2)

        def worker():
            with psycopg.connect(
                f"dbname={DB_NAME}",
                autocommit=True,
            ) as conn:
                barrier.wait(timeout=10)

                return record_settlement(
                    conn=conn,
                    group_id=self.group_id,
                    from_user_id=self.users[1],
                    to_user_id=self.users[0],
                    amount_cents=2000,
                    idempotency_key=key,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [f.result(timeout=15) for f in futures]

        # Both requests should return the same settlement ID.
        self.assertEqual(results[0], results[1])

        count = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM settlements
            WHERE idempotency_key = %s
            """,
            (key,),
        ).fetchone()[0]

        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
