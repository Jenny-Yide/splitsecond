import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from backend.app import auth, db
from backend.app.api import ApiError, require_member, router
from backend.app.realtime import Hub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpx2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

DATABASE_URL = os.environ["DATABASE_URL"]
BACKUP_MAX_AGE = timedelta(hours=float(os.environ.get("BACKUP_MAX_AGE_HOURS", "26")))
# With archive_timeout=300 a completed segment normally waits at most a few minutes.
WAL_ARCHIVE_MAX_LAG = timedelta(minutes=float(os.environ.get("WAL_ARCHIVE_MAX_LAG_MINUTES", "15")))
# Must stay below proxy and load-balancer idle timeouts (often 60 s).
WS_PING_SECONDS = float(os.environ.get("WS_PING_SECONDS", "30"))
HOSTNAME = socket.gethostname()

hub: Hub | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global hub
    await run_in_threadpool(db.open_pool, DATABASE_URL)
    hub = Hub(DATABASE_URL)
    await hub.start()
    yield
    await hub.stop()
    await run_in_threadpool(db.close_pool)


app = FastAPI(title="SplitSecond", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(ApiError)
async def api_error(_, exc: ApiError):
    return JSONResponse({"error": exc.detail, **exc.extra}, status_code=exc.status_code)


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(psycopg.errors.CheckViolation)
async def check_violation(_, exc: Exception):
    return JSONResponse({"error": f"rejected by the database: {exc}"}, status_code=422)


def connect():
    return psycopg.connect(DATABASE_URL, connect_timeout=3)


def wal_segment_number(wal_file):
    """Position of a WAL file within its timeline, e.g. 000000010000000200000003 -> 0x203."""
    if not wal_file:
        return 0
    log, seg = int(wal_file[8:16], 16), int(wal_file[16:24], 16)
    return log * 0x100 + seg


@app.get("/livez")
def livez():
    return {"status": "ok", "host": HOSTNAME}


@app.get("/health")
def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
    except psycopg.Error:
        return JSONResponse(
            {"status": "database unavailable", "host": HOSTNAME}, status_code=503
        )
    return {
        "status": "ok",
        "host": HOSTNAME,
        "realtime_listening": bool(hub and hub.listening),
        "websockets": hub.socket_count if hub else 0,
    }


@app.get("/health/backup")
def backup_health():
    try:
        with connect() as conn:
            runs = conn.execute(
                """
                SELECT k.kind, max(r.finished_at), now() - max(r.finished_at)
                FROM (VALUES ('dump'), ('base')) AS k(kind)
                LEFT JOIN backup_runs r
                  ON r.kind = k.kind AND r.status = 'success'
                GROUP BY k.kind
                """
            ).fetchall()
            (
                archive_mode,
                last_archived,
                archive_age,
                archived_wal,
                last_failed,
                failed_wal,
                current_wal,
            ) = conn.execute(
                """
                SELECT current_setting('archive_mode'),
                       last_archived_time, now() - last_archived_time, last_archived_wal,
                       last_failed_time, last_failed_wal,
                       pg_walfile_name(pg_current_wal_lsn())
                FROM pg_stat_archiver
                """
            ).fetchone()
    except psycopg.Error:
        return JSONResponse({"status": "database unavailable"}, status_code=503)

    problems = []
    backups = {}
    for kind, last_success, age in runs:
        backups[kind] = {
            "last_success": last_success.isoformat() if last_success else None,
            "age_hours": round(age.total_seconds() / 3600, 2) if age else None,
        }
        if age is None or age > BACKUP_MAX_AGE:
            problems.append(
                f"no successful {kind} backup in the last "
                f"{BACKUP_MAX_AGE.total_seconds() / 3600:g} hours"
            )

    archiving = archive_mode != "off"
    failing = archiving and last_failed is not None and (
        last_archived is None or last_failed > last_archived
    )
    # Completed segments not yet archived. An idle database switches no segments, so
    # an old last_archived_time alone is not a problem; a backlog that old is.
    pending = wal_segment_number(current_wal) - wal_segment_number(archived_wal) - 1
    behind = (
        archiving
        and pending > 0
        and (archive_age is None or archive_age > WAL_ARCHIVE_MAX_LAG)
    )
    if failing:
        problems.append(f"WAL archiving is failing (last failed segment {failed_wal})")
    if behind:
        problems.append(
            f"{pending} WAL segment(s) waiting more than "
            f"{WAL_ARCHIVE_MAX_LAG.total_seconds() / 60:g} minutes to be archived"
        )

    body = {
        "status": "unhealthy" if problems else "ok",
        "problems": problems,
        "max_age_hours": BACKUP_MAX_AGE.total_seconds() / 3600,
        "backups": backups,
        "wal_archiving": {
            "enabled": archiving,
            "last_archived": last_archived.isoformat() if last_archived else None,
            "last_failed": last_failed.isoformat() if last_failed else None,
            "pending_segments": max(pending, 0),
            "failing": failing,
            "behind": behind,
        },
    }
    return JSONResponse(body, status_code=503 if problems else 200)


@app.websocket("/ws")
async def websocket_echo(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"type": "hello", "host": HOSTNAME})

    async def ping():
        while True:
            await asyncio.sleep(WS_PING_SECONDS)
            await websocket.send_json({"type": "ping", "host": HOSTNAME})

    pinger = asyncio.create_task(ping())
    try:
        while True:
            message = await websocket.receive_text()
            await websocket.send_json({"type": "echo", "host": HOSTNAME, "data": message})
    except WebSocketDisconnect:
        pass
    finally:
        pinger.cancel()


@app.websocket("/ws/groups/{group_id}")
async def group_socket(websocket: WebSocket, group_id: int, token: str = ""):
    """Live updates for one group. Pushes {"type": "bill_updated" | "ledger_changed" |
    "members_changed" | "resync" | "ping", ...}; clients re-fetch over HTTP."""

    def authorize():
        user_id = auth.read_token(token)
        with db.tx() as conn:
            require_member(conn, group_id, user_id)

    try:
        await run_in_threadpool(authorize)
    except HTTPException:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    hub.add(group_id, websocket)

    async def ping():
        # Proxies and load balancers drop idle connections.
        while True:
            await asyncio.sleep(WS_PING_SECONDS)
            await websocket.send_json({"type": "ping", "host": HOSTNAME})

    pinger = asyncio.create_task(ping())
    try:
        await websocket.send_json({"type": "hello", "host": HOSTNAME})
        while True:
            await websocket.receive_text()  # client keepalives; content ignored
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        pinger.cancel()
        hub.remove(group_id, websocket)


# The phone web UI. Mounted last so it never shadows the API routes above.
app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="ui")
