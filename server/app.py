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
    vehNo: str = ""       # 차량번호 (driver-entered)
    company: str = ""     # 운행회사 (driver-entered)
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
        "vehNo": u.vehNo, "company": u.company, "dir": direction,
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


def _geo_overrides() -> dict:
    op = DATA_DIR / "geo_overrides.json"
    return json.loads(op.read_text(encoding="utf-8")) if op.exists() else {}


def _save_geo_overrides(ov: dict):
    (DATA_DIR / "geo_overrides.json").write_text(
        json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_geo_merged():
    """Original spline geometry (geo_124.json) with any admin position overrides
    (geo_overrides.json, keyed by stop id) merged in."""
    p = DATA_DIR / "geo_124.json"
    if not p.exists():
        return None
    geo = json.loads(p.read_text(encoding="utf-8"))
    ov = _geo_overrides()
    if ov:
        for d in ("up", "down"):
            for s in geo.get(d, {}).get("stops", []):
                if str(s["id"]) in ov:
                    s["xy"] = ov[str(s["id"])]
    return geo


class StopPos(BaseModel):
    dir: str
    id: str
    x: float
    y: float


class StopReset(BaseModel):
    dir: str = "up"
    id: str


@app.post("/api/stoppos")
async def edit_stoppos(e: StopPos):
    """Admin-only: record a stop's corrected map position (by id). Stored as an
    override; the original geo_124.json is never mutated, so a reset can undo it."""
    if READONLY:
        return JSONResponse({"error": "이 서버는 보기 전용입니다. 편집은 로컬에서 하세요."}, status_code=403)
    if e.dir not in ("up", "down"):
        return JSONResponse({"error": "bad dir"}, status_code=400)
    xy = [round(e.x, 1), round(e.y, 1)]
    ov = _geo_overrides()
    ov[str(e.id)] = xy
    _save_geo_overrides(ov)
    return {"ok": True, "id": str(e.id), "xy": xy}


@app.post("/api/stoppos/reset")
async def reset_stoppos(e: StopReset):
    """Admin-only: drop a stop's override, reverting it to the original position.
    Returns that original so the map can snap it back."""
    if READONLY:
        return JSONResponse({"error": "보기 전용"}, status_code=403)
    ov = _geo_overrides()
    ov.pop(str(e.id), None)
    _save_geo_overrides(ov)
    base = _load_geo_merged()          # override just removed, so this is the original
    d = e.dir if e.dir in ("up", "down") else "up"
    hit = next((s for s in base[d]["stops"] if str(s["id"]) == str(e.id)), None) if base else None
    return {"ok": True, "id": str(e.id), "xy": hit["xy"] if hit else None}


@app.get("/api/geo")
async def geo():
    g = _load_geo_merged()
    if g is None:
        return JSONResponse({"error": "no geo"}, status_code=404)
    return JSONResponse(g)


# ── notices (공지사항): anyone can read, only the admin (local) can post/delete ──
NOTICES_FILE = DATA_DIR / "notices.json"


def _load_notices() -> list:
    return json.loads(NOTICES_FILE.read_text(encoding="utf-8")) if NOTICES_FILE.exists() else []


def _save_notices(items: list):
    NOTICES_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


class Notice(BaseModel):
    title: str
    body: str = ""


@app.get("/api/notices")
async def notices():
    return sorted(_load_notices(), key=lambda n: n.get("id", 0), reverse=True)   # newest first


@app.post("/api/notices")
async def add_notice(n: Notice):
    if READONLY:
        return JSONResponse({"error": "공지 작성은 관리자만 가능합니다."}, status_code=403)
    title = n.title.strip()
    if not title:
        return JSONResponse({"error": "제목을 입력하세요."}, status_code=400)
    items = _load_notices()
    nid = (max((i.get("id", 0) for i in items), default=0)) + 1
    items.append({"id": nid, "title": title[:200], "body": n.body.strip(),
                  "date": time.strftime("%Y-%m-%d")})
    _save_notices(items)
    return {"ok": True, "id": nid}


@app.delete("/api/notices/{nid}")
async def del_notice(nid: int):
    if READONLY:
        return JSONResponse({"error": "관리자만 삭제할 수 있습니다."}, status_code=403)
    items = [i for i in _load_notices() if i.get("id") != nid]
    _save_notices(items)
    return {"ok": True}


@app.get("/geo_bg.png")
async def geo_bg():
    return FileResponse(WEB_DIR / "geo_bg.png", media_type="image/png")


@app.get("/")
async def index():
    # no-store so a plain browser refresh always gets the latest page during dev
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})
