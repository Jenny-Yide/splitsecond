import asyncio
import os
import socket
from datetime import timedelta

import psycopg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

DATABASE_URL = os.environ["DATABASE_URL"]
BACKUP_MAX_AGE = timedelta(hours=float(os.environ.get("BACKUP_MAX_AGE_HOURS", "26")))
# Must stay below the ingress idle timeout (60s by default in ingress-nginx).
WS_PING_SECONDS = float(os.environ.get("WS_PING_SECONDS", "30"))
HOSTNAME = socket.gethostname()

app = FastAPI(title="SplitSecond")


def connect():
    return psycopg.connect(DATABASE_URL, connect_timeout=3)


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
            last_success, age = conn.execute(
                """
                SELECT max(finished_at), now() - max(finished_at)
                FROM backup_runs
                WHERE status = 'success'
                """
            ).fetchone()
    except psycopg.Error:
        return JSONResponse({"status": "database unavailable"}, status_code=503)

    body = {
        "last_success": last_success.isoformat() if last_success else None,
        "age_hours": round(age.total_seconds() / 3600, 2) if age else None,
        "max_age_hours": BACKUP_MAX_AGE.total_seconds() / 3600,
    }
    if age is None or age > BACKUP_MAX_AGE:
        return JSONResponse({"status": "stale", **body}, status_code=503)
    return {"status": "ok", **body}


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
