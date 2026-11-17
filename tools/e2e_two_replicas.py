#!/usr/bin/env python3
"""End-to-end check that live updates cross API replicas.

Phone A's WebSocket is held by replica 1; phone B's claim and the finalize are
handled by replica 2. Phone A must still see both (via LISTEN/NOTIFY).

    pip install httpx websockets
    python tools/e2e_two_replicas.py http://localhost:8001 http://localhost:8002

Against Kubernetes, port-forward two specific API pods first:
    kubectl -n splitsecond port-forward pod/<api-pod-1> 8001:8000 &
    kubectl -n splitsecond port-forward pod/<api-pod-2> 8002:8000 &
"""
import asyncio
import json
import sys
import time
import uuid

import httpx
import websockets


async def main(base1: str, base2: str) -> None:
    async with httpx.AsyncClient(timeout=10) as http:
        async def signup(base, prefix):
            r = await http.post(f"{base}/api/auth/signup", json={
                "username": f"{prefix}_{uuid.uuid4().hex[:8]}", "password": "e2e-password"})
            r.raise_for_status()
            d = r.json()
            return d["user"], {"Authorization": f"Bearer {d['token']}"}, d["token"]

        a, a_h, a_token = await signup(base1, "phone_a")
        b, b_h, _ = await signup(base2, "phone_b")
        g = (await http.post(f"{base1}/api/groups", json={"name": "e2e"}, headers=a_h)).json()
        (await http.post(f"{base1}/api/groups/{g['id']}/members", json={"username": b["username"]},
                         headers=a_h)).raise_for_status()
        bill = (await http.post(f"{base1}/api/groups/{g['id']}/bills", headers=a_h, json={
            "tax_cents": 130, "tip_cents": 200,
            "items": [{"name": "Ramen", "price_cents": 1600}, {"name": "Beer", "price_cents": 800}]})).json()

        ws_url = base1.replace("http", "ws", 1) + f"/ws/groups/{g['id']}?token={a_token}"
        async with websockets.connect(ws_url) as ws:
            assert json.loads(await ws.recv())["type"] == "hello"

            async def next_event():
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    if msg["type"] != "ping":
                        return msg

            t0 = time.perf_counter()
            beer = bill["items"][1]["id"]
            (await http.put(f"{base2}/api/bills/{bill['id']}/items/{beer}/claim", headers=b_h)).raise_for_status()
            msg = await next_event()
            ms = (time.perf_counter() - t0) * 1000
            assert msg == {"group_id": g["id"], "type": "bill_updated", "bill_id": bill["id"]}, msg
            print(f"claim sent to {base2} reached the socket on {base1} in {ms:.0f} ms")

            ramen = bill["items"][0]["id"]
            (await http.put(f"{base2}/api/bills/{bill['id']}/items/{ramen}/claim", headers=a_h)).raise_for_status()
            (await http.post(f"{base2}/api/bills/{bill['id']}/finalize", headers=b_h)).raise_for_status()
            seen = set()
            while not {"bill_updated", "ledger_changed"} <= seen:
                seen.add((await next_event())["type"])

        bal = (await http.get(f"{base1}/api/groups/{g['id']}/balances", headers=a_h)).json()["balances"]
        print("final balances:", {r["username"]: r["net_cents"] for r in bal})
        assert sum(r["net_cents"] for r in bal) == 0
    print("OK: cross-replica live updates work")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001",
                     sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8002"))
