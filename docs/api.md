# API, authentication, realtime and web UI

Code: `backend/app/` (`main.py`, `api.py`, `auth.py`, `db.py`, `realtime.py`, `static/`).
Money logic stays in `backend/ledger/`; this layer adds authentication, a group-membership
check on every route, validation, HTTP status codes and realtime events.

## Run it

Compose (`docker compose up -d --build --wait`) serves the app at <http://localhost:8080>.
Open it on two phones or browsers to try live claiming.

Without Docker:

```sh
pip install -r requirements.txt
createdb splitsecond_dev && psql -d splitsecond_dev -f backend/schema.sql
export DATABASE_URL=dbname=splitsecond_dev JWT_SECRET=$(openssl rand -hex 32)
uvicorn backend.app.main:app --port 8001 &
uvicorn backend.app.main:app --port 8002 &
python tools/e2e_two_replicas.py http://localhost:8001 http://localhost:8002   # needs: pip install websockets
```

Tests: `python -m unittest discover -s backend/tests` (`test_api.py` covers the API).

## Authentication

- `POST /api/auth/signup` `{username, password}` → `{token, user}`. Usernames are lower-cased; passwords need ≥ 8 characters and are hashed with bcrypt.
- `POST /api/auth/login` `{username, password}` → `{token, user}`.
- Every other `/api` route needs `Authorization: Bearer <token>` (JWT, HS256, signed with `JWT_SECRET`, valid for 7 days).
- Outsiders get **404** for a group they don't belong to, so they can't tell whether it exists.

## Routes

All amounts are integer cents. Errors are `{"error": "...", ...}`.

| Method & path | Body / query | Notes |
| --- | --- | --- |
| `GET /api/me` | | |
| `GET /api/groups` | | Includes `my_net_cents` |
| `POST /api/groups` | `{name}` | Creator becomes a member |
| `GET /api/groups/{id}` | | Includes `members` |
| `POST /api/groups/{id}/members` | `{username}` | Any member can add an existing user |
| `GET /api/groups/{id}/expenses` | | Active versions with `shares` |
| `POST /api/groups/{id}/expenses` | `{description, total_cents, payer_id, split_mode, participants \| amounts \| shares, idempotency_key}` | `split_mode`: `equal` (default everyone), `exact`, `shares`. **201** new, **200** replay of the same key |
| `GET /api/expenses/{expense_id}` | | `live` + all `versions` |
| `PUT /api/expenses/{expense_id}` | `{expected_version, description, total_cents, participants}` | Equal split, same payer (`update_equal_expense`). **409** + `current_version` if stale |
| `DELETE /api/expenses/{expense_id}?expected_version=N` | | Soft delete; **409** if stale |
| `POST /api/expenses/{expense_id}/restore` | | Deleted expenses only |
| `GET /api/groups/{id}/balances?at=ISO8601` | | `at` → `get_balances_at` |
| `GET /api/groups/{id}/settle-up` | | |
| `GET/POST /api/groups/{id}/settlements` | `{from_user_id, to_user_id, amount_cents, idempotency_key}` | |
| `GET /api/groups/{id}/history` | | `events` (created / edited / restored / deleted / payment) and `deleted_expenses` |
| `GET/POST /api/groups/{id}/bills` | `{payer_id, tax_cents, tip_cents, items: [{name, price_cents}]}` | |
| `GET /api/bills/{id}` | | Items, `claimed_by`, `unclaimed_item_ids`, live `preview` per person |
| `POST /api/bills/{id}/items`, `DELETE /api/bills/{id}/items/{item}` | | Open bills only |
| `PUT /api/bills/{id}/items/{item}/claim` | `{for_user}` optional | Idempotent; claim for someone without a phone |
| `DELETE /api/bills/{id}/items/{item}/claim?for_user=` | | |
| `POST /api/bills/{id}/finalize` | | `finalize_bill`; **422** + `unclaimed_item_ids` |

The bill `preview` uses the same rules as `finalize_bill`, so what people see while
claiming is exactly what gets written (tested).

## Concurrency

- **One request, one transaction.** The ledger functions open `conn.transaction()`. Inside the request's transaction that becomes a savepoint, so the ledger change and its `NOTIFY` commit together.
- **Claims vs. finalize.** Claims lock the bill `FOR SHARE`; `finalize_bill` locks it `FOR UPDATE`. A claim either lands before finalize and is counted, or waits and then gets 409. It never lands on a finalized bill (tested with a race).
- **Idempotent creates.** A double tap or retry with the same `idempotency_key` returns the first expense, also when the requests arrive at the same moment (tested with 8 parallel requests).
- **Pool health.** The connection pool tests each connection before use, so replicas recover by themselves after PostgreSQL restarts.

## Realtime (WebSockets + LISTEN/NOTIFY)

`WS /ws/groups/{id}?token=<jwt>`: members only (others are closed with code 4403).

Messages:
- `hello`: sent on connect.
- `ping`: sent every `WS_PING_SECONDS` (30).
- `bill_updated` with `bill_id`, `ledger_changed`, `members_changed`.
- `resync`: sent after the replica's LISTEN connection reconnects.

How it works:
- Each replica keeps one dedicated connection that runs `LISTEN splitsecond`.
- Changes call `pg_notify` in their own transaction, so events go out only after commit.
- Payloads carry IDs only, well under PostgreSQL's 8000-byte limit; clients re-fetch over HTTP.
- Clients also reload when they reconnect, because missed notifications are not stored.

`GET /health` now also reports `realtime_listening` and the number of open sockets.
`/ws` (echo) is unchanged for infrastructure tests.

## Known gaps in the ledger (for Cheng)

1. Edits only support equal splits and can't change the payer (`update_equal_expense`).
2. `restore_expense` only restores deleted expenses. The proposal also promises restoring an older version after a bad edit.
3. `create_expense` now takes an optional `idempotency_key`. Before, it always generated its own key, so client retries created duplicates. This is the only change to `backend/ledger/`; please review.
4. The schema has no `created_by` on expenses, so History can't show who changed what. Bills have no title, so the UI shows "Bill #id".
5. `bill_items.price_cents` allows 0; if every claimed item were free, `allocate_cents` would raise. The API rejects price 0.
