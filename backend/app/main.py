import asyncio
import os
import socket
from datetime import timedelta

import psycopg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

DATABASE_URL = os.environ["DATABASE_URL"]
BACKUP_MAX_AGE = timedelta(hours=float(os.environ.get("BACKUP_MAX_AGE_HOURS", "26")))
# With archive_timeout=300 a completed segment normally waits at most a few minutes.
WAL_ARCHIVE_MAX_LAG = timedelta(minutes=float(os.environ.get("WAL_ARCHIVE_MAX_LAG_MINUTES", "15")))
# Must stay below the ingress idle timeout (60s by default in ingress-nginx).
WS_PING_SECONDS = float(os.environ.get("WS_PING_SECONDS", "30"))
HOSTNAME = socket.gethostname()

app = FastAPI(title="SplitSecond")


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
    return {"status": "ok", "host": HOSTNAME}


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
