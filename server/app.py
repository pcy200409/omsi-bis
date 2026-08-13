"""OMSI BIS server — schedule-based marker placement.

Clients read OMSI's own AI schedule (via OmsiHook, read-only) and POST it here.
The only thing the marker needs is which stop the bus is heading to
(`nextIdCode`, matching Busstops.cfg's id) plus OMSI's prev/next distances — the
frontend places the marker between those two stops. No coordinate math, odometer,
or projection is involved (those earlier approaches are gone).

Run:  .venv/Scripts/python -m uvicorn app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

STALE_SECONDS = 6.0
PUSH_HZ = 2          # broadcasts/sec to viewers; schedule markers + CSS easing stay
                     # smooth at 2 Hz, and this halves per-viewer send cost vs 5 Hz.
# On the shared/cloud deployment we run VIEW-ONLY: the name editor needs the OMSI
# map files (absent in the cloud) and a persistent disk (the free host's is
# ephemeral), so editing stays a local-only workflow. Set BIS_READONLY=1 there.
READONLY = os.environ.get("BIS_READONLY", "").strip() not in ("", "0", "false", "False")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

app = FastAPI(title="OMSI BIS server")
buses: dict[str, dict] = {}

# stop-id -> direction, so a bus is placed on the line whose schedule it's running
# (the next stop's id-code tells us which direction's stop list it belongs to).
ID2DIR: dict[int, str] = {}
for _key, _dir in (("124A", "up"), ("124B", "down")):
    _rp = DATA_DIR / f"route_{_key}.json"
    if _rp.exists():
        for _s in json.loads(_rp.read_text(encoding="utf-8"))["stops"]:
            try:
                ID2DIR[int(_s["id"])] = _dir
            except (ValueError, KeyError):
                pass


class Update(BaseModel):
    id: str
    nick: str = "bus"
    line: str = ""
    map: str = ""
    # OMSI's own schedule read-out (the ground truth for marker placement).
    nextIdx: int = -1
    nextIdCode: int = 0
    nextDist: float = 0.0
    prevDist: float = 0.0
    atStation: float = 0.0
    nextName: str = ""
    schedValid: bool = False
    # raw position fields are still sent by the client but unused here — the model
    # simply ignores any extra keys, so the client needs no redeploy.


@app.post("/api/update")
async def update(u: Update):
    prev = buses.get(u.id)
    direction = prev["dir"] if prev else None
    # OMSI's schedule is authoritative: the direction is whichever line's stop
    # list contains the next stop's id-code.
    if u.schedValid and u.nextIdCode in ID2DIR:
        direction = ID2DIR[u.nextIdCode]
    buses[u.id] = {
        "id": u.id, "nick": u.nick, "line": u.line, "map": u.map,
        "dir": direction,
        "nextIdCode": u.nextIdCode, "nextIdx": u.nextIdx,
        "nextDist": u.nextDist, "prevDist": u.prevDist,
        "atStation": u.atStation, "nextName": u.nextName,
        "schedValid": u.schedValid, "ts": time.time(),
    }
    return {"ok": True}


def snapshot() -> list[dict]:
    now = time.time()
    for bid in [k for k, v in buses.items() if now - v["ts"] > STALE_SECONDS]:
        buses.pop(bid, None)
    return [b for b in buses.values() if now - b["ts"] <= STALE_SECONDS]


@app.get("/api/state")
async def state():
    return JSONResponse(snapshot())


clients: set[WebSocket] = set()


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    clients.add(sock)
    try:
        while True:
            await sock.receive_text()          # only used to detect disconnect
    except Exception:
        pass
    finally:
        clients.discard(sock)


async def _broadcaster():
    # one snapshot per tick, pushed to all viewers — avoids per-connection loops
    # saturating the event loop (which was starving the POST handler).
    while True:
        if clients:
            msg = {"t": time.time(), "buses": snapshot()}
            for c in list(clients):
                try:
                    await c.send_json(msg)
                except Exception:
                    clients.discard(c)
        await asyncio.sleep(1 / PUSH_HZ)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_broadcaster())


@app.get("/api/routes")
async def routes_index():
    return FileResponse(DATA_DIR / "routes.json")


@app.get("/api/route/{key}")
async def route(key: str):
    key = re.sub(r"[^0-9A-Za-z]", "", key)          # sanitize path segment
    p = DATA_DIR / f"route_{key}.json"
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


def _load_route(key: str) -> dict | None:
    p = DATA_DIR / f"route_{key}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@app.get("/api/stops")
async def stops_list():
    """Every unique stop (merged across directions, in route order) with its
    romanized name + current Korean name — the data the name editor lists."""
    out, seen = [], {}
    for key, direction in (("124A", "up"), ("124B", "down")):
        r = _load_route(key)
        if not r:
            continue
        for s in r["stops"]:
            sid = str(s["id"])
            if sid in seen:
                seen[sid]["dirs"].append(direction)
                continue
            row = {"id": sid, "name": s.get("name", ""),
                   "kname": s.get("kname", ""), "dirs": [direction]}
            seen[sid] = row
            out.append(row)
    return out


class KnameEdit(BaseModel):
    id: str
    kname: str


def _rebuild_routes() -> tuple[bool, str]:
    """Regenerate route_*.json from the map + current overrides, and refresh the
    in-memory id->direction map. Returns (ok, message)."""
    r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "build_routes.py")],
                       capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "build_routes failed")[-500:]
    ID2DIR.clear()
    for _k, _d in (("124A", "up"), ("124B", "down")):
        rj = _load_route(_k)
        if rj:
            for _s in rj["stops"]:
                try:
                    ID2DIR[int(_s["id"])] = _d
                except (ValueError, KeyError):
                    pass
    return True, "ok"


@app.get("/api/config")
async def config():
    # the frontend hides the edit UI when the deployment is view-only
    return {"editable": not READONLY}


@app.post("/api/kname")
async def edit_kname(e: KnameEdit):
    if READONLY:
        return JSONResponse({"error": "이 서버는 보기 전용입니다. 편집은 로컬에서 하세요."}, status_code=403)
    kname = e.kname.strip()
    sid = re.sub(r"[^0-9]", "", e.id)
    if not sid:
        return JSONResponse({"error": "bad id"}, status_code=400)
    if not kname:
        return JSONResponse({"error": "empty name"}, status_code=400)
    ov_path = DATA_DIR / "kname_overrides.json"
    ov = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    ov[sid] = kname
    ov_path.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")
    ok, msg = _rebuild_routes()
    if not ok:
        return JSONResponse({"error": msg}, status_code=500)
    return {"ok": True, "id": sid, "kname": kname}


@app.get("/")
async def index():
    # no-store so a plain browser refresh always gets the latest page during dev
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})
