"""Smoke test for the live server in demo mode. Requires the server running:

    python -m uvicorn marshal_ai.server:app --app-dir src --port 8000

Connects over the websocket, runs the demo, and asserts the swarm streamed the
expected stages including a self-correction round.
"""
import asyncio
import json
import time
import urllib.request

import websockets

URL_HEALTH = "http://127.0.0.1:8000/health"
URL_WS = "ws://127.0.0.1:8000/ws"


async def main() -> int:
    for _ in range(60):
        try:
            urllib.request.urlopen(URL_HEALTH, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("server never came up")
        return 1

    types: list[str] = []
    final = None
    async with websockets.connect(URL_WS, max_size=None) as ws:
        await ws.send(json.dumps({"question": "Monolith or microservices?", "budget": 0.50, "mode": "demo"}))
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                types.append(msg.get("type"))
                if msg.get("type") == "done":
                    final = msg.get("answer")
        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            pass

    print("EVENTS:", " ".join(t for t in types if t))
    checks = {
        "decomposed": "decomposed" in types,
        "workers_start": "workers_start" in types,
        "worker_done": "worker_done" in types,
        "graded": "graded" in types,
        "self-correction (rescope_start)": "rescope_start" in types,
        "synthesise_start": "synthesise_start" in types,
        "done": "done" in types,
        "final answer text": bool(final and "Final answer" in final),
    }
    ok = all(checks.values())
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print("SMOKE OK" if ok else "SMOKE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
