"""API tests: auth, access control, idempotency, optimistic concurrency, history,
payments, live bills and WebSockets. They run against splitsecond_test_db like
the ledger tests (libpq takes host/user/password from PG* variables)."""
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "dbname=splitsecond_test_db")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-at-least-32-bytes-long")
os.environ.setdefault("WS_PING_SECONDS", "1")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from backend.app.main import app  # noqa: E402


class Person:
    def __init__(self, client, prefix="user"):
        self.username = f"{prefix}_{uuid4().hex[:10]}"
        r = client.post("/api/auth/signup", json={"username": self.username, "password": "correct-horse"})
        assert r.status_code == 201, r.text
        self.id = r.json()["user"]["id"]
        self.token = r.json()["token"]
        self.h = {"Authorization": f"Bearer {self.token}"}


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._cm = TestClient(app)
        cls.client = cls._cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)

    def setUp(self):
        c = self.client
        # Created in this order, so a.id < b.id < c.id.
        self.a, self.b, self.c = Person(c, "alice"), Person(c, "bob"), Person(c, "carol")
        self.gid = c.post("/api/groups", json={"name": "Flat 4B"}, headers=self.a.h).json()["id"]
        for p in (self.b, self.c):
            r = c.post(f"/api/groups/{self.gid}/members", json={"username": p.username}, headers=self.a.h)
            self.assertEqual(r.status_code, 200, r.text)

    # -------------------------------------------------------------- helpers

    def add_expense(self, who, total, mode="equal", key=None, **split):
        body = {"description": "Groceries", "total_cents": total, "payer_id": who.id,
                "split_mode": mode, **split}
        if key:
            body["idempotency_key"] = str(key)
        return self.client.post(f"/api/groups/{self.gid}/expenses", json=body, headers=who.h)

    def net(self, **params):
        r = self.client.get(f"/api/groups/{self.gid}/balances", params=params, headers=self.a.h)
        self.assertEqual(r.status_code, 200, r.text)
        return {b["user_id"]: b["net_cents"] for b in r.json()["balances"]}

    def make_bill(self):
        r = self.client.post(f"/api/groups/{self.gid}/bills", headers=self.a.h, json={
            "tax_cents": 949, "tip_cents": 1314, "items": [
                {"name": "Noodle bowl", "price_cents": 1400},
                {"name": "Steak", "price_cents": 3800},
                {"name": "Drink", "price_cents": 600},
                {"name": "Drink", "price_cents": 600},
                {"name": "Gyoza", "price_cents": 900}]})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def claim(self, bill, idx, who, for_user=None):
        item = bill["items"][idx]["id"]
        return self.client.put(f"/api/bills/{bill['id']}/items/{item}/claim", headers=who.h,
                               json={"for_user": for_user} if for_user else None)

    # -------------------------------------------------------------- auth & access

    def test_signup_login_me(self):
        c = self.client
        self.assertEqual(c.post("/api/auth/signup", json={"username": self.a.username, "password": "whatever123"}).status_code, 409)
        self.assertEqual(c.post("/api/auth/login", json={"username": self.a.username, "password": "nope"}).status_code, 401)
        r = c.post("/api/auth/login", json={"username": self.a.username.upper(), "password": "correct-horse"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()["id"], self.a.id)
        self.assertEqual(c.get("/api/groups", headers={"Authorization": "Bearer junk"}).status_code, 401)
        self.assertEqual(c.get("/api/groups").status_code, 401)

    def test_outsiders_cannot_see_or_touch_a_group(self):
        eve = Person(self.client, "eve")
        eid = self.add_expense(self.a, 3000).json()["expense_id"]
        for path in (f"/api/groups/{self.gid}", f"/api/groups/{self.gid}/balances",
                     f"/api/groups/{self.gid}/history", f"/api/expenses/{eid}"):
            self.assertEqual(self.client.get(path, headers=eve.h).status_code, 404, path)
        self.assertEqual(self.add_expense(eve, 100).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/expenses/{eid}", params={"expected_version": 1},
                                            headers=eve.h).status_code, 404)
        r = self.add_expense(self.a, 100, participants=[self.a.id, eve.id])
        self.assertEqual(r.status_code, 422)

    def test_add_member_requires_existing_user(self):
        r = self.client.post(f"/api/groups/{self.gid}/members", json={"username": "nobody_" + uuid4().hex}, headers=self.a.h)
        self.assertEqual(r.status_code, 404)
        groups = self.client.get("/api/groups", headers=self.b.h).json()
        self.assertEqual([g["id"] for g in groups if g["id"] == self.gid], [self.gid])

    # -------------------------------------------------------------- expenses

    def test_equal_split_and_balances(self):
        r = self.add_expense(self.a, 10000)
        self.assertEqual(r.status_code, 201, r.text)
        shares = {s["user_id"]: s["amount_cents"] for s in r.json()["live"]["shares"]}
        self.assertEqual(shares, {self.a.id: 3334, self.b.id: 3333, self.c.id: 3333})
        self.assertEqual(self.net(), {self.a.id: 6666, self.b.id: -3333, self.c.id: -3333})

    def test_exact_and_shares(self):
        r = self.add_expense(self.b, 5000, "exact", amounts={self.a.id: 1000, self.b.id: 4000})
        self.assertEqual(r.status_code, 201, r.text)
        r = self.add_expense(self.c, 1000, "shares", shares={self.a.id: 1, self.c.id: 2})
        self.assertEqual({s["user_id"]: s["amount_cents"] for s in r.json()["live"]["shares"]},
                         {self.a.id: 333, self.c.id: 667})
        bad = self.add_expense(self.a, 5000, "exact", amounts={self.a.id: 1000, self.b.id: 3999})
        self.assertEqual(bad.status_code, 422)

    def test_idempotent_create(self):
        key = uuid4()
        first, again = self.add_expense(self.a, 3000, key=key), self.add_expense(self.a, 3000, key=key)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json()["expense_id"], again.json()["expense_id"])

    def test_idempotent_create_concurrent_double_tap(self):
        key = uuid4()
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.add_expense(self.a, 4200, key=key), range(8)))
        self.assertTrue({r.status_code for r in results} <= {200, 201}, [r.text for r in results])
        self.assertEqual(len({r.json()["expense_id"] for r in results}), 1)
        expenses = self.client.get(f"/api/groups/{self.gid}/expenses", headers=self.a.h).json()
        self.assertEqual(len(expenses), 1)

    def test_stale_edit_gets_409(self):
        eid = self.add_expense(self.a, 9000).json()["expense_id"]
        body = {"expected_version": 1, "description": "Groceries + wine", "total_cents": 12000,
                "participants": [self.a.id, self.b.id, self.c.id]}
        r = self.client.put(f"/api/expenses/{eid}", json=body, headers=self.b.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["live"]["version"], 2)
        stale = self.client.put(f"/api/expenses/{eid}", json={**body, "total_cents": 1}, headers=self.c.h)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["current_version"], 2)
        self.assertEqual(self.net()[self.a.id], 8000)

    def test_concurrent_edits_exactly_one_wins(self):
        eid = self.add_expense(self.a, 9000).json()["expense_id"]
        people = [self.a, self.b, self.c]

        def edit(i):
            return self.client.put(f"/api/expenses/{eid}", headers=people[i % 3].h, json={
                "expected_version": 1, "description": f"edit {i}", "total_cents": 1000 + i,
                "participants": [self.a.id, self.b.id]}).status_code

        with ThreadPoolExecutor(6) as pool:
            self.assertEqual(sorted(pool.map(edit, range(6))), [200, 409, 409, 409, 409, 409])

    def test_delete_restore_history_and_past_balances(self):
        eid = self.add_expense(self.a, 3000).json()["expense_id"]
        time.sleep(0.05)
        before_delete = datetime.now(timezone.utc)
        time.sleep(0.05)
        r = self.client.delete(f"/api/expenses/{eid}", params={"expected_version": 1}, headers=self.b.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(set(self.net().values()), {0})
        self.assertEqual(self.net(at=before_delete.isoformat())[self.a.id], 2000)
        hist = self.client.get(f"/api/groups/{self.gid}/history", headers=self.c.h).json()
        self.assertEqual([e["kind"] for e in hist["events"]], ["deleted", "created"])
        self.assertEqual(hist["deleted_expenses"][0]["expense_id"], eid)
        r = self.client.post(f"/api/expenses/{eid}/restore", headers=self.c.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["live"]["version"], 2)
        self.assertEqual(self.net()[self.a.id], 2000)
        self.assertEqual(self.client.post(f"/api/expenses/{eid}/restore", headers=self.c.h).status_code, 409)
        kinds = [e["kind"] for e in self.client.get(f"/api/groups/{self.gid}/history", headers=self.c.h).json()["events"]]
        self.assertEqual(kinds, ["restored", "deleted", "created"])

    # -------------------------------------------------------------- payments

    def test_settle_up_and_idempotent_payments(self):
        self.add_expense(self.a, 9000)
        self.add_expense(self.b, 3000, participants=[self.b.id, self.c.id])
        plan = self.client.get(f"/api/groups/{self.gid}/settle-up", headers=self.a.h).json()
        self.assertLessEqual(len(plan), 2)
        for p in plan:
            body = {"from_user_id": p["from_user_id"], "to_user_id": p["to_user_id"],
                    "amount_cents": p["amount_cents"], "idempotency_key": str(uuid4())}
            self.assertEqual(self.client.post(f"/api/groups/{self.gid}/settlements", json=body, headers=self.b.h).status_code, 201)
            self.assertEqual(self.client.post(f"/api/groups/{self.gid}/settlements", json=body, headers=self.b.h).status_code, 200)
        self.assertEqual(set(self.net().values()), {0})
        self.assertEqual(self.client.get(f"/api/groups/{self.gid}/settle-up", headers=self.a.h).json(), [])

    # -------------------------------------------------------------- live bills

    def test_live_bill_end_to_end(self):
        bill = self.make_bill()
        self.assertEqual(bill["total_cents"], 7300 + 949 + 1314)
        self.claim(bill, 0, self.b)
        for i in (1, 2, 3):
            self.claim(bill, i, self.c)
        r = self.client.post(f"/api/bills/{bill['id']}/finalize", headers=self.a.h)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["unclaimed_item_ids"], [bill["items"][4]["id"]])
        self.claim(bill, 4, self.b)
        self.claim(bill, 4, self.c)
        self.claim(bill, 4, self.c)  # claiming twice is a no-op
        preview = self.client.get(f"/api/bills/{bill['id']}", headers=self.a.h).json()["preview"]
        self.assertEqual({p["user_id"]: p["total_cents"] for p in preview},
                         {self.b.id: 1850 + 241 + 333, self.c.id: 5450 + 708 + 981})
        r = self.client.post(f"/api/bills/{bill['id']}/finalize", headers=self.a.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "finalized")
        # The preview matched what finalize_bill actually wrote.
        self.assertEqual(self.net(), {self.a.id: 9563, self.b.id: -2424, self.c.id: -7139})
        self.assertEqual(self.claim(bill, 0, self.a).status_code, 409)
        self.assertEqual(self.client.post(f"/api/bills/{bill['id']}/items", headers=self.a.h,
                                          json={"name": "late", "price_cents": 100}).status_code, 409)
        expense = self.client.get(f"/api/groups/{self.gid}/expenses", headers=self.a.h).json()[0]
        self.assertEqual(expense["bill_id"], bill["id"])

    def test_claim_for_someone_and_unclaim(self):
        bill = self.make_bill()
        self.assertEqual(self.claim(bill, 0, self.a, for_user=self.b.id).json()["items"][0]["claimed_by"], [self.b.id])
        item = bill["items"][0]["id"]
        r = self.client.delete(f"/api/bills/{bill['id']}/items/{item}/claim", params={"for_user": self.b.id}, headers=self.a.h)
        self.assertEqual(r.json()["items"][0]["claimed_by"], [])

    def test_claims_from_many_phones_in_any_order(self):
        bill = self.make_bill()
        jobs = [(i, p) for i in range(5) for p in (self.a, self.b, self.c)] * 2
        with ThreadPoolExecutor(10) as pool:
            codes = set(pool.map(lambda j: self.claim(bill, j[0], j[1]).status_code, jobs))
        self.assertEqual(codes, {200})
        full = self.client.get(f"/api/bills/{bill['id']}", headers=self.a.h).json()
        for item in full["items"]:
            self.assertEqual(sorted(item["claimed_by"]), sorted([self.a.id, self.b.id, self.c.id]))
        self.assertEqual(sum(p["total_cents"] for p in full["preview"]), full["total_cents"])

    def test_finalize_races_with_claims(self):
        """A claim either lands before finalize (and is counted) or gets 409."""
        bill = self.make_bill()
        for i in range(5):
            self.claim(bill, i, self.a)

        def act(i):
            if i == 5:
                return "finalize", self.client.post(f"/api/bills/{bill['id']}/finalize", headers=self.a.h).status_code
            return "claim", self.claim(bill, i % 5, self.b).status_code

        with ThreadPoolExecutor(11) as pool:
            results = list(pool.map(act, range(11)))
        self.assertIn(("finalize", 200), results)
        self.assertTrue({code for kind, code in results if kind == "claim"} <= {200, 409})
        full = self.client.get(f"/api/bills/{bill['id']}", headers=self.a.h).json()
        expense = self.client.get(f"/api/expenses/{full['expense_id']}", headers=self.a.h).json()["live"]
        written = {s["user_id"]: s["amount_cents"] for s in expense["shares"]}
        self.assertEqual(written, {p["user_id"]: p["total_cents"] for p in full["preview"]})

    # -------------------------------------------------------------- websockets

    def test_websocket_gets_bill_events(self):
        bill = self.make_bill()
        with self.client.websocket_connect(f"/ws/groups/{self.gid}?token={self.b.token}") as ws:
            self.assertEqual(ws.receive_json()["type"], "hello")
            self.claim(bill, 0, self.c)
            msg = ws.receive_json()
            while msg["type"] == "ping":
                msg = ws.receive_json()
            self.assertEqual(msg, {"group_id": self.gid, "type": "bill_updated", "bill_id": bill["id"]})

    def test_websocket_rejects_outsiders(self):
        eve = Person(self.client, "eve")
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"/ws/groups/{self.gid}?token={eve.token}") as ws:
                ws.receive_json()

    def test_health_reports_realtime(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["realtime_listening"])


if __name__ == "__main__":
    unittest.main()
