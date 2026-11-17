"""HTTP API: authentication, groups, expenses, history, payments and live bills.

Money logic lives in backend/ledger (Cheng's module); this layer adds
authentication, group-membership checks on every route, request validation,
HTTP error codes, and realtime NOTIFY events.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app import auth
from backend.app.db import notify, row, rows, tx
from backend.ledger.balances import get_group_balances
from backend.ledger.expenses import create_expense
from backend.ledger.finalize import allocate_cents, finalize_bill
from backend.ledger.history import get_balances_at
from backend.ledger.settle import settle_up
from backend.ledger.settlements import record_settlement
from backend.ledger.split import split_equally
from backend.ledger.versioning import (
    VersionConflictError,
    delete_expense,
    restore_expense,
    update_equal_expense,
)

router = APIRouter(prefix="/api")
User = Depends(auth.current_user)


# ------------------------------------------------------------------ helpers


class ApiError(HTTPException):
    """HTTPException whose JSON body can carry extra fields (e.g. current_version)."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(status, message)
        self.extra = extra


@contextmanager
def ledger_errors():
    """Translate ledger exceptions into HTTP status codes."""
    try:
        yield
    except VersionConflictError as exc:
        raise ApiError(409, str(exc))
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise ApiError(404, message)
        if message in ("Expense is not deleted",):
            raise ApiError(409, message)
        raise ApiError(422, message)


def require_member(conn, group_id: int, user_id: int) -> None:
    if row(conn, "SELECT 1 AS ok FROM group_members WHERE group_id = %s AND user_id = %s",
           (group_id, user_id)) is None:
        # 404, not 403: don't reveal that the group exists.
        raise ApiError(404, "group not found")


def member_ids(conn, group_id: int) -> list[int]:
    return [r["user_id"] for r in rows(
        conn, "SELECT user_id FROM group_members WHERE group_id = %s ORDER BY user_id", (group_id,))]


def expense_group(conn, expense_id: UUID, user_id: int) -> int:
    r = row(conn, "SELECT group_id FROM expenses WHERE expense_id = %s LIMIT 1", (expense_id,))
    if r is None:
        raise ApiError(404, "expense not found")
    require_member(conn, r["group_id"], user_id)
    return r["group_id"]


EXPENSE_SQL = """
    SELECT e.id, e.expense_id, e.version, e.group_id, e.payer_id, e.total_cents,
           e.description, e.created_at, e.voided_at, e.void_reason,
           (SELECT min(f.created_at) FROM expenses f WHERE f.expense_id = e.expense_id) AS first_created_at,
           (SELECT b.id FROM bills b JOIN expenses f ON f.id = b.expense_version_id
             WHERE f.expense_id = e.expense_id LIMIT 1) AS bill_id
      FROM expenses e
"""


def with_shares(conn, expense_rows: list[dict]) -> list[dict]:
    if expense_rows:
        shares = rows(conn, """
            SELECT expense_version_id, user_id, amount_cents FROM expense_shares
             WHERE expense_version_id = ANY(%s) ORDER BY user_id""",
            ([r["id"] for r in expense_rows],))
        by_version: dict[int, list] = {}
        for s in shares:
            by_version.setdefault(s.pop("expense_version_id"), []).append(s)
        for r in expense_rows:
            r["shares"] = by_version.get(r["id"], [])
    return expense_rows


def expense_detail(conn, expense_id: UUID) -> dict:
    versions = with_shares(conn, rows(conn, EXPENSE_SQL + " WHERE e.expense_id = %s ORDER BY e.version",
                                      (expense_id,)))
    live = next((v for v in versions if v["voided_at"] is None), None)
    return {"expense_id": expense_id, "group_id": versions[0]["group_id"],
            "live": live, "versions": versions}


def usernames(conn, group_id: int) -> dict[int, str]:
    return {r["id"]: r["username"] for r in rows(conn, """
        SELECT u.id, u.username FROM group_members m JOIN users u ON u.id = m.user_id
         WHERE m.group_id = %s""", (group_id,))}


def json_response(body, status: int) -> JSONResponse:
    return JSONResponse(jsonable_encoder(body), status_code=status)


# ------------------------------------------------------------------ models


class SignupIn(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=200)


class LoginIn(BaseModel):
    username: str
    password: str


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MemberIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)


class ExpenseIn(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    total_cents: int = Field(gt=0, le=100_000_000_00)
    payer_id: int
    split_mode: Literal["equal", "exact", "shares"]
    participants: list[int] | None = None   # equal (default: everyone)
    amounts: dict[int, int] | None = None   # exact
    shares: dict[int, int] | None = None    # shares
    idempotency_key: UUID | None = None


class ExpenseEditIn(BaseModel):
    expected_version: int
    description: str = Field(min_length=1, max_length=200)
    total_cents: int = Field(gt=0, le=100_000_000_00)
    participants: list[int] = Field(min_length=1)


class SettlementIn(BaseModel):
    from_user_id: int
    to_user_id: int
    amount_cents: int = Field(gt=0)
    idempotency_key: UUID | None = None


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    price_cents: int = Field(gt=0, le=100_000_000)


class BillIn(BaseModel):
    payer_id: int | None = None
    tax_cents: int = Field(default=0, ge=0)
    tip_cents: int = Field(default=0, ge=0)
    items: list[ItemIn] = Field(default=[], max_length=100)


class ClaimIn(BaseModel):
    for_user: int | None = None


# ------------------------------------------------------------------ auth


@router.post("/auth/signup", status_code=201)
def signup(body: SignupIn):
    try:
        with tx() as conn:
            user = row(conn, """
                INSERT INTO users (username, password_hash) VALUES (%s, %s)
                RETURNING id, username""",
                (body.username.strip().lower(), auth.hash_password(body.password)))
    except psycopg.errors.UniqueViolation:
        raise ApiError(409, "that username is taken")
    return {"token": auth.make_token(user["id"]), "user": user}


@router.post("/auth/login")
def login(body: LoginIn):
    with tx() as conn:
        user = row(conn, "SELECT id, username, password_hash FROM users WHERE username = %s",
                   (body.username.strip().lower(),))
    if user is None or not auth.check_password(body.password, user.pop("password_hash")):
        raise ApiError(401, "wrong username or password")
    return {"token": auth.make_token(user["id"]), "user": user}


@router.get("/me")
def me(user_id: int = User):
    with tx() as conn:
        user = row(conn, "SELECT id, username FROM users WHERE id = %s", (user_id,))
    if user is None:
        raise ApiError(401, "account no longer exists")
    return user


# ------------------------------------------------------------------ groups


@router.get("/groups")
def my_groups(user_id: int = User):
    with tx() as conn:
        groups = rows(conn, """
            SELECT g.id, g.name, g.created_at,
                   (SELECT count(*) FROM group_members x WHERE x.group_id = g.id) AS member_count
              FROM groups g JOIN group_members m ON m.group_id = g.id
             WHERE m.user_id = %s ORDER BY g.created_at DESC""", (user_id,))
        for g in groups:
            g["my_net_cents"] = get_group_balances(conn, g["id"]).get(user_id, 0)
    return groups


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, user_id: int = User):
    with tx() as conn:
        group = row(conn, "INSERT INTO groups (name) VALUES (%s) RETURNING id, name, created_at",
                    (body.name.strip(),))
        conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (%s, %s)",
                     (group["id"], user_id))
    return group


@router.get("/groups/{group_id}")
def get_group(group_id: int, user_id: int = User):
    with tx() as conn:
        require_member(conn, group_id, user_id)
        group = row(conn, "SELECT id, name, created_at FROM groups WHERE id = %s", (group_id,))
        group["members"] = rows(conn, """
            SELECT u.id, u.username FROM group_members m JOIN users u ON u.id = m.user_id
             WHERE m.group_id = %s ORDER BY u.id""", (group_id,))
    return group


@router.post("/groups/{group_id}/members")
def add_member(group_id: int, body: MemberIn, user_id: int = User):
    """Any member can add an existing user by username."""
    with tx() as conn:
        require_member(conn, group_id, user_id)
        user = row(conn, "SELECT id, username FROM users WHERE username = %s",
                   (body.username.strip().lower(),))
        if user is None:
            raise ApiError(404, "no user with that username; ask them to sign up first")
        conn.execute("""INSERT INTO group_members (group_id, user_id) VALUES (%s, %s)
                        ON CONFLICT DO NOTHING""", (group_id, user["id"]))
        notify(conn, group_id, "members_changed")
    return user


# ------------------------------------------------------------------ expenses


@router.get("/groups/{group_id}/expenses")
def list_expenses(group_id: int, user_id: int = User):
    with tx() as conn:
        require_member(conn, group_id, user_id)
        return with_shares(conn, rows(conn, EXPENSE_SQL + """
            WHERE e.group_id = %s AND e.voided_at IS NULL
            ORDER BY first_created_at DESC, e.id DESC""", (group_id,)))


@router.post("/groups/{group_id}/expenses")
def add_expense(group_id: int, body: ExpenseIn, user_id: int = User):
    with tx() as conn:
        require_member(conn, group_id, user_id)
        key = body.idempotency_key
        if key:  # a retry or double tap: return what the first request created
            existing = row(conn, "SELECT expense_id, group_id FROM expenses WHERE idempotency_key = %s", (key,))
            if existing:
                if existing["group_id"] != group_id:
                    raise ApiError(409, "idempotency key already used for another group")
                return json_response(expense_detail(conn, existing["expense_id"]), 200)

        if body.split_mode == "equal":
            split_data = body.participants or member_ids(conn, group_id)
        elif body.split_mode == "exact":
            split_data = {u: a for u, a in (body.amounts or {}).items() if a}
        else:
            split_data = {u: w for u, w in (body.shares or {}).items() if w}

        try:
            with ledger_errors():
                version_id = create_expense(conn, group_id, body.payer_id, body.total_cents,
                                            body.split_mode, split_data, body.description.strip(),
                                            idempotency_key=key)
        except psycopg.errors.UniqueViolation:
            # A concurrent request with the same key committed first. The ledger's
            # savepoint rolled back, so this transaction is still usable.
            existing = row(conn, "SELECT expense_id FROM expenses WHERE idempotency_key = %s", (key,))
            return json_response(expense_detail(conn, existing["expense_id"]), 200)

        expense_id = row(conn, "SELECT expense_id FROM expenses WHERE id = %s", (version_id,))["expense_id"]
        notify(conn, group_id, "ledger_changed", expense_id=expense_id)
        return json_response(expense_detail(conn, expense_id), 201)


@router.get("/expenses/{expense_id}")
def get_expense(expense_id: UUID, user_id: int = User):
    with tx() as conn:
        expense_group(conn, expense_id, user_id)
        return expense_detail(conn, expense_id)


def _current_version(conn, expense_id: UUID):
    r = row(conn, "SELECT version FROM expenses WHERE expense_id = %s AND voided_at IS NULL", (expense_id,))
    return r["version"] if r else None


@router.put("/expenses/{expense_id}")
def edit_expense(expense_id: UUID, body: ExpenseEditIn, user_id: int = User):
    """Equal split only: that is what the ledger's update function supports."""
    with tx() as conn:
        group_id = expense_group(conn, expense_id, user_id)
        try:
            with ledger_errors():
                update_equal_expense(conn, expense_id, body.expected_version, body.total_cents,
                                     body.participants, body.description.strip())
        except ApiError as exc:
            if exc.status_code == 409:
                exc.extra["current_version"] = _current_version(conn, expense_id)
            raise
        notify(conn, group_id, "ledger_changed", expense_id=expense_id)
        return expense_detail(conn, expense_id)


@router.delete("/expenses/{expense_id}")
def remove_expense(expense_id: UUID, expected_version: int, user_id: int = User):
    with tx() as conn:
        group_id = expense_group(conn, expense_id, user_id)
        try:
            with ledger_errors():
                delete_expense(conn, expense_id, expected_version)
        except ApiError as exc:
            if exc.status_code == 409:
                exc.extra["current_version"] = _current_version(conn, expense_id)
            raise
        notify(conn, group_id, "ledger_changed", expense_id=expense_id)
        return expense_detail(conn, expense_id)


@router.post("/expenses/{expense_id}/restore")
def undelete_expense(expense_id: UUID, user_id: int = User):
    with tx() as conn:
        group_id = expense_group(conn, expense_id, user_id)
        with ledger_errors():
            restore_expense(conn, expense_id)
        notify(conn, group_id, "ledger_changed", expense_id=expense_id)
        return expense_detail(conn, expense_id)


# ------------------------------------------------------------------ balances, payments, history


@router.get("/groups/{group_id}/balances")
def balances(group_id: int, at: datetime | None = None, user_id: int = User):
    """Current balances, or balances as of `at` (ISO 8601). Positive = is owed money."""
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    with tx() as conn:
        require_member(conn, group_id, user_id)
        nets = get_balances_at(conn, group_id, at) if at else get_group_balances(conn, group_id)
        names = usernames(conn, group_id)
    return {"at": at, "balances": [
        {"user_id": u, "username": names.get(u), "net_cents": nets[u]} for u in sorted(nets)]}


@router.get("/groups/{group_id}/settle-up")
def settle_plan(group_id: int, user_id: int = User):
    with tx() as conn:
        require_member(conn, group_id, user_id)
        plan = settle_up(get_group_balances(conn, group_id))
        names = usernames(conn, group_id)
    return [{"from_user_id": f, "from_username": names.get(f), "to_user_id": t,
             "to_username": names.get(t), "amount_cents": a} for f, t, a in plan]


@router.get("/groups/{group_id}/settlements")
def list_settlements(group_id: int, user_id: int = User):
    with tx() as conn:
        require_member(conn, group_id, user_id)
        return rows(conn, """
            SELECT id, from_user_id, to_user_id, amount_cents, created_at FROM settlements
             WHERE group_id = %s ORDER BY created_at DESC, id DESC""", (group_id,))


@router.post("/groups/{group_id}/settlements")
def add_settlement(group_id: int, body: SettlementIn, user_id: int = User):
    with tx() as conn:
        require_member(conn, group_id, user_id)
        replay = body.idempotency_key and row(
            conn, "SELECT 1 AS ok FROM settlements WHERE idempotency_key = %s", (body.idempotency_key,))
        with ledger_errors():
            sid = record_settlement(conn, group_id, body.from_user_id, body.to_user_id,
                                    body.amount_cents, body.idempotency_key)
        if not replay:
            notify(conn, group_id, "ledger_changed", settlement_id=sid)
        payment = row(conn, "SELECT id, from_user_id, to_user_id, amount_cents, created_at "
                            "FROM settlements WHERE id = %s", (sid,))
        return json_response(payment, 200 if replay else 201)


@router.get("/groups/{group_id}/history")
def history(group_id: int, user_id: int = User):
    """Every change in the group, newest first, plus deleted expenses that can be restored."""
    with tx() as conn:
        require_member(conn, group_id, user_id)
        versions = with_shares(conn, rows(conn, EXPENSE_SQL + """
            WHERE e.group_id = %s ORDER BY e.expense_id, e.version""", (group_id,)))
        payments = rows(conn, """SELECT id, from_user_id, to_user_id, amount_cents, created_at
                                   FROM settlements WHERE group_id = %s""", (group_id,))
    events, latest, prev = [], {}, {}
    for v in versions:
        before = prev.get(v["expense_id"])
        kind = ("created" if v["version"] == 1
                else "restored" if before and before["void_reason"] == "deleted" else "edited")
        events.append({"kind": kind, "at": v["created_at"], **v})
        if v["void_reason"] == "deleted":
            events.append({"kind": "deleted", "at": v["voided_at"], **v})
        prev[v["expense_id"]] = latest[v["expense_id"]] = v
    for p in payments:
        events.append({"kind": "payment", "at": p["created_at"], **p})
    events.sort(key=lambda e: e["at"], reverse=True)
    deleted = sorted((v for v in latest.values() if v["void_reason"] == "deleted"),
                     key=lambda v: v["voided_at"], reverse=True)
    return {"events": events, "deleted_expenses": deleted}


# ------------------------------------------------------------------ live bills


def bill_for(conn, bill_id: int, user_id: int, lock: str = "") -> dict:
    bill = row(conn, f"SELECT * FROM bills WHERE id = %s {lock}", (bill_id,))
    if bill is None:
        raise ApiError(404, "bill not found")
    require_member(conn, bill["group_id"], user_id)
    return bill


def require_open(bill: dict) -> None:
    if bill["status"] != "open":
        raise ApiError(409, f"this bill is already {bill['status']}")


def preview_split(items: list[dict], tax: int, tip: int) -> dict[int, dict]:
    """What each claimant owes so far, using the same rules as finalize_bill:
    each item split equally among its claimants (leftover cents by ascending
    user ID), then tax and tip by largest remainder on each subtotal.
    Unclaimed items are left out."""
    subtotals: dict[int, int] = {}
    for item in items:
        if item["claimed_by"]:
            for uid, cents in split_equally(item["price_cents"], item["claimed_by"]).items():
                subtotals[uid] = subtotals.get(uid, 0) + cents
    if not subtotals or sum(subtotals.values()) == 0:
        return {}
    tax_s, tip_s = allocate_cents(tax, subtotals), allocate_cents(tip, subtotals)
    return {u: {"user_id": u, "subtotal_cents": s, "tax_cents": tax_s[u], "tip_cents": tip_s[u],
                "total_cents": s + tax_s[u] + tip_s[u]} for u, s in sorted(subtotals.items())}


def bill_detail(conn, bill_id: int, user_id: int) -> dict:
    bill = bill_for(conn, bill_id, user_id)
    items = rows(conn, "SELECT id, name, price_cents FROM bill_items WHERE bill_id = %s ORDER BY id",
                 (bill_id,))
    claims = rows(conn, """SELECT c.item_id, c.user_id FROM item_claims c
                            JOIN bill_items i ON i.id = c.item_id
                           WHERE i.bill_id = %s ORDER BY c.user_id""", (bill_id,))
    by_item: dict[int, list[int]] = {}
    for c in claims:
        by_item.setdefault(c["item_id"], []).append(c["user_id"])
    for item in items:
        item["claimed_by"] = by_item.get(item["id"], [])
    subtotal = sum(i["price_cents"] for i in items)
    expense = None
    if bill["expense_version_id"]:
        expense = row(conn, "SELECT expense_id FROM expenses WHERE id = %s", (bill["expense_version_id"],))
    return {**bill, "items": items, "subtotal_cents": subtotal,
            "total_cents": subtotal + bill["tax_cents"] + bill["tip_cents"],
            "unclaimed_item_ids": [i["id"] for i in items if not i["claimed_by"]],
            "preview": list(preview_split(items, bill["tax_cents"], bill["tip_cents"]).values()),
            "expense_id": expense["expense_id"] if expense else None}


@router.get("/groups/{group_id}/bills")
def list_bills(group_id: int, user_id: int = User):
    with tx() as conn:
        require_member(conn, group_id, user_id)
        return rows(conn, """
            SELECT b.id, b.payer_id, b.status, b.created_at, b.tax_cents, b.tip_cents,
                   count(i.id) AS item_count,
                   COALESCE(sum(i.price_cents), 0)::bigint + b.tax_cents + b.tip_cents AS total_cents
              FROM bills b LEFT JOIN bill_items i ON i.bill_id = b.id
             WHERE b.group_id = %s GROUP BY b.id ORDER BY b.created_at DESC""", (group_id,))


@router.post("/groups/{group_id}/bills", status_code=201)
def create_bill(group_id: int, body: BillIn, user_id: int = User):
    with tx() as conn:
        require_member(conn, group_id, user_id)
        payer = body.payer_id or user_id
        if payer not in member_ids(conn, group_id):
            raise ApiError(422, "the payer must be a group member")
        bill = row(conn, """INSERT INTO bills (group_id, payer_id, tax_cents, tip_cents)
                            VALUES (%s, %s, %s, %s) RETURNING id""",
                   (group_id, payer, body.tax_cents, body.tip_cents))
        for item in body.items:
            conn.execute("INSERT INTO bill_items (bill_id, name, price_cents) VALUES (%s, %s, %s)",
                         (bill["id"], item.name.strip(), item.price_cents))
        notify(conn, group_id, "bill_updated", bill_id=bill["id"])
        return bill_detail(conn, bill["id"], user_id)


@router.get("/bills/{bill_id}")
def get_bill(bill_id: int, user_id: int = User):
    with tx() as conn:
        return bill_detail(conn, bill_id, user_id)


@router.post("/bills/{bill_id}/items")
def add_item(bill_id: int, body: ItemIn, user_id: int = User):
    with tx() as conn:
        bill = bill_for(conn, bill_id, user_id, "FOR UPDATE")
        require_open(bill)
        conn.execute("INSERT INTO bill_items (bill_id, name, price_cents) VALUES (%s, %s, %s)",
                     (bill_id, body.name.strip(), body.price_cents))
        notify(conn, bill["group_id"], "bill_updated", bill_id=bill_id)
        return bill_detail(conn, bill_id, user_id)


@router.delete("/bills/{bill_id}/items/{item_id}")
def remove_item(bill_id: int, item_id: int, user_id: int = User):
    with tx() as conn:
        bill = bill_for(conn, bill_id, user_id, "FOR UPDATE")
        require_open(bill)
        conn.execute("DELETE FROM item_claims WHERE item_id = %s", (item_id,))
        if conn.execute("DELETE FROM bill_items WHERE id = %s AND bill_id = %s",
                        (item_id, bill_id)).rowcount == 0:
            raise ApiError(404, "item not found")
        notify(conn, bill["group_id"], "bill_updated", bill_id=bill_id)
        return bill_detail(conn, bill_id, user_id)


def set_claim(bill_id: int, item_id: int, user_id: int, claimed: bool, for_user: int | None):
    """Claim or unclaim an item; idempotent and order-independent across phones.

    FOR SHARE on the bill lets claims from many phones run in parallel but makes
    them wait for a concurrent finalize (which takes FOR UPDATE). After it
    commits, the claim sees status 'finalized' and gets 409, so no claim can
    land on a bill that has already become an expense."""
    with tx() as conn:
        bill = bill_for(conn, bill_id, user_id, "FOR SHARE")
        require_open(bill)
        target = for_user or user_id
        if target not in member_ids(conn, bill["group_id"]):
            raise ApiError(422, "you can only claim for group members")
        if row(conn, "SELECT 1 AS ok FROM bill_items WHERE id = %s AND bill_id = %s",
               (item_id, bill_id)) is None:
            raise ApiError(404, "item not found")
        if claimed:
            conn.execute("INSERT INTO item_claims (item_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                         (item_id, target))
        else:
            conn.execute("DELETE FROM item_claims WHERE item_id = %s AND user_id = %s", (item_id, target))
        notify(conn, bill["group_id"], "bill_updated", bill_id=bill_id)
        return bill_detail(conn, bill_id, user_id)


@router.put("/bills/{bill_id}/items/{item_id}/claim")
def claim_item(bill_id: int, item_id: int, body: ClaimIn | None = None, user_id: int = User):
    return set_claim(bill_id, item_id, user_id, True, body.for_user if body else None)


@router.delete("/bills/{bill_id}/items/{item_id}/claim")
def unclaim_item(bill_id: int, item_id: int, for_user: int | None = None, user_id: int = User):
    return set_claim(bill_id, item_id, user_id, False, for_user)


@router.post("/bills/{bill_id}/finalize")
def finalize(bill_id: int, user_id: int = User):
    with tx() as conn:
        bill = bill_for(conn, bill_id, user_id)
        try:
            with ledger_errors():
                finalize_bill(conn, bill_id)  # locks the bill FOR UPDATE, one transaction
        except ApiError as exc:
            if exc.detail == "Some items are unclaimed":
                exc.extra["unclaimed_item_ids"] = bill_detail(conn, bill_id, user_id)["unclaimed_item_ids"]
            raise
        notify(conn, bill["group_id"], "bill_updated", bill_id=bill_id)
        notify(conn, bill["group_id"], "ledger_changed")
        return bill_detail(conn, bill_id, user_id)
