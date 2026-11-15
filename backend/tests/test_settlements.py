
import unittest
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import psycopg

from backend.ledger.expenses import create_equal_expense
from backend.ledger.settlements import record_settlement
from backend.ledger.balances import get_group_balances
from backend.ledger.history import get_balances_at


class TestSettlements(unittest.TestCase):

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
                (f"settlement_test_{uuid4().hex}",),
            ).fetchone()[0]
            self.users.append(uid)

        self.group_id = self.conn.execute(
            """
            INSERT INTO groups (name)
            VALUES ('Settlement Test')
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

        create_equal_expense(
            self.conn,
            self.group_id,
            self.users[0],
            10000,
            self.users,
            "Settlement test expense",
        )

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def test_settlement_updates_balances(self):
        before = get_group_balances(self.conn, self.group_id)

        self.assertEqual(
            before,
            dict(zip(self.users, [6666, -3333, -3333])),
        )

        record_settlement(
            self.conn,
            self.group_id,
            self.users[1],
            self.users[0],
            2000,
        )

        after = get_group_balances(self.conn, self.group_id)

        self.assertEqual(
            after,
            dict(zip(self.users, [4666, -1333, -3333])),
        )

    def test_invalid_amount_rejected(self):
        with self.assertRaises(ValueError):
            record_settlement(
                self.conn,
                self.group_id,
                self.users[1],
                self.users[0],
                0,
            )

    def test_self_payment_rejected(self):
        with self.assertRaises(ValueError):
            record_settlement(
                self.conn,
                self.group_id,
                self.users[0],
                self.users[0],
                1000,
            )

    def test_nonmember_rejected(self):
        with self.assertRaises(ValueError):
            record_settlement(
                self.conn,
                self.group_id,
                self.users[1],
                999999,
                1000,
            )



    def test_historical_settlement_balances(self):
        # Record a $20 settlement.
        settlement_id = record_settlement(
            self.conn,
            self.group_id,
            self.users[1],
            self.users[0],
            2000,
        )

        # Read the timestamp recorded by PostgreSQL.
        settlement_time = self.conn.execute(
            """
            SELECT created_at
            FROM settlements
            WHERE id = %s
            """,
            (settlement_id,),
        ).fetchone()[0]

        # Make the expense one second earlier so that
        # both states can be queried unambiguously.
        self.conn.execute(
            """
            UPDATE expenses
            SET created_at = %s
            WHERE group_id = %s
            """,
            (
                settlement_time - timedelta(seconds=2),
                self.group_id,
            ),
        )

        before = settlement_time - timedelta(seconds=1)
        after = settlement_time + timedelta(seconds=1)

        balances_before = get_balances_at(
            self.conn, self.group_id, before
        )

        balances_after = get_balances_at(
            self.conn, self.group_id, after
        )

        self.assertEqual(
            balances_before,
            dict(zip(self.users, [6666, -3333, -3333])),
        )

        self.assertEqual(
            balances_after,
            dict(zip(self.users, [4666, -1333, -3333])),
        )

    def test_idempotency_same_request(self):
        key = uuid4()

        first_id = record_settlement(
            self.conn, self.group_id,
            self.users[1], self.users[0],
            2000, idempotency_key=key,
        )

        second_id = record_settlement(
            self.conn, self.group_id,
            self.users[1], self.users[0],
            2000, idempotency_key=key,
        )

        self.assertEqual(first_id, second_id)

        count = self.conn.execute(
            """
            SELECT COUNT(*) FROM settlements
            WHERE idempotency_key = %s
            """,
            (key,),
        ).fetchone()[0]

        self.assertEqual(count, 1)

    def test_idempotency_different_amount(self):
        key = uuid4()

        record_settlement(
            self.conn, self.group_id,
            self.users[1], self.users[0],
            2000, idempotency_key=key,
        )

        with self.assertRaises(ValueError):
            record_settlement(
                self.conn, self.group_id,
                self.users[1], self.users[0],
                3000, idempotency_key=key,
            )

    def test_idempotency_different_keys(self):
        first_id = record_settlement(
            self.conn, self.group_id,
            self.users[1], self.users[0],
            1000, idempotency_key=uuid4(),
        )

        second_id = record_settlement(
            self.conn, self.group_id,
            self.users[1], self.users[0],
            1000, idempotency_key=uuid4(),
        )

        self.assertNotEqual(first_id, second_id)


if __name__ == "__main__":
    unittest.main()
