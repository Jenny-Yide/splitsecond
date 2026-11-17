"""Cross-replica realtime fan-out with PostgreSQL LISTEN/NOTIFY (no Redis).

A phone's WebSocket is held by one replica, but its claims may be handled by
another. Each replica keeps one dedicated connection that LISTENs on a channel.
Whichever replica commits a change NOTIFYs; every replica forwards the event to
its own sockets for that group.

Sessions that are not listening when a NOTIFY fires never receive it, so events
carry IDs only: clients reload on (re)connect, and when this listener itself
reconnects it tells its clients to resync.
"""
import asyncio
import json
import logging
from collections import defaultdict

import psycopg
from starlette.websockets import WebSocket

from backend.app.db import NOTIFY_CHANNEL

log = logging.getLogger("splitsecond.realtime")


class Hub:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.groups: dict[int, set[WebSocket]] = defaultdict(set)
        self.listening = False
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="listen-notify")
        try:  # don't report ready until we are actually listening
            await asyncio.wait_for(self._ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.error("LISTEN connection not ready yet; will keep retrying")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        backoff, first = 1.0, True
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
                async with conn:
                    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    self.listening = True
                    self._ready.set()
                    backoff = 1.0
                    if not first:
                        # Events may have been missed while we were disconnected.
                        await self._broadcast_all({"type": "resync"})
                    first = False
                    log.info("listening on %s", NOTIFY_CHANNEL)
                    async for note in conn.notifies():
                        try:
                            await self.dispatch(json.loads(note.payload))
                        except ValueError:
                            continue
            except asyncio.CancelledError:
                self.listening = False
                raise
            except Exception as exc:  # database restart, network blip...
                self.listening = False
                log.warning("LISTEN connection lost (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def dispatch(self, event: dict) -> None:
        group_id = event.get("group_id")
        sockets = list(self.groups.get(group_id, ()))
        if sockets:
            await asyncio.gather(*(self._send(ws, group_id, event) for ws in sockets))

    async def _broadcast_all(self, event: dict) -> None:
        for group_id, sockets in list(self.groups.items()):
            await asyncio.gather(*(self._send(ws, group_id, event) for ws in list(sockets)))

    async def _send(self, ws: WebSocket, group_id: int, event: dict) -> None:
        try:
            await asyncio.wait_for(ws.send_json(event), timeout=5)
        except Exception:  # slow or dead client: drop it
            self.remove(group_id, ws)

    def add(self, group_id: int, ws: WebSocket) -> None:
        self.groups[group_id].add(ws)

    def remove(self, group_id: int, ws: WebSocket) -> None:
        sockets = self.groups.get(group_id)
        if sockets is not None:
            sockets.discard(ws)
            if not sockets:
                self.groups.pop(group_id, None)

    @property
    def socket_count(self) -> int:
        return sum(len(s) for s in self.groups.values())
