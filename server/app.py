"""OMSI BIS server — schedule-based marker placement, multi-region.

Clients read OMSI's own AI schedule (via OmsiHook, read-only) and POST it here.
The marker only needs which stop the bus heads to (`nextIdCode`, matching
Busstops.cfg's id) plus OMSI's prev/next distances. Each OMSI map is a REGION
(data/regions/<key>/…); a bus's region is derived from the map it reports.

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
# On the shared/cloud deployment we run VIEW-ONLY: the editors need the OMSI map
# files (absent in the cloud) and a persistent disk (the free host's is
# ephemeral), so editing stays a local-only workflow. Set BIS_READONLY=1 there.
READONLY = os.environ.get("BIS_READONLY", "").strip() not in ("", "0", "false", "False")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REGIONS_DIR = DATA_DIR / "regions"

app = FastAPI(title="OMSI BIS server")
buses: dict[str, dict] = {}


# ── regions ──────────────────────────────────────────────────────────────
def load_regions() -> list[dict]:
    p = DATA_DIR / "regions.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


REGIONS = load_regions()
DEFAULT_REGION = REGIONS[0]["key"] if REGIONS else "segang"
REGION_KEYS = {r["key"] for r in REGIONS}
MAP2REGION = {r.get("map", ""): r["key"] for r in REGIONS}      # OMSI map name -> region


def region_dir(key: str) -> Path:
    return REGIONS_DIR / key


def valid_region(key: str) -> str:
    return key if key in REGION_KEYS else DEFAULT_REGION


def build_id2dir(key: str) -> dict[int, str]:
    """stop-id -> direction for one region (its schedule tells us which line)."""
    d: dict[int, str] = {}
    for f in region_dir(key).glob("route_*.json"):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            for s in r["stops"]:
                d[int(s["id"])] = r.get("dir")
        except Exception:
            pass
    return d


ID2DIR: dict[str, dict[int, str]] = {r["key"]: build_id2dir(r["key"]) for r in REGIONS}


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


@app.post("/api/update")
async def update(u: Update):
    region = MAP2REGION.get(u.map, DEFAULT_REGION)     # which region this bus is on
    prev = buses.get(u.id)
    direction = prev["dir"] if prev else None
    id2 = ID2DIR.get(region, {})
    if u.schedValid and u.nextIdCode in id2:
        direction = id2[u.nextIdCode]
    buses[u.id] = {
        "id": u.id, "nick": u.nick, "line": u.line, "map": u.map, "region": region,
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


# ── region-scoped route + geo data ───────────────────────────────────────
@app.get("/api/regions")
async def regions_list():
    return [{"key": r["key"], "name": r["name"], "line": r.get("line", "")} for r in REGIONS]


@app.get("/api/routes")
async def routes_index(region: str = ""):
    p = region_dir(valid_region(region or DEFAULT_REGION)) / "routes.json"
    return FileResponse(p) if p.exists() else JSONResponse([], status_code=200)


def _load_route(rk: str, key: str) -> dict | None:
    p = region_dir(rk) / f"route_{key}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@app.get("/api/route/{key}")
async def route(key: str, region: str = ""):
    rk = valid_region(region or DEFAULT_REGION)
    key = re.sub(r"[^0-9A-Za-z]", "", key)          # sanitize path segment
    r = _load_route(rk, key)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(r)


@app.get("/api/stops")
async def stops_list(region: str = ""):
    """Every unique stop (merged across directions) for the name editor."""
    rk = valid_region(region or DEFAULT_REGION)
    out, seen = [], {}
    for f in sorted(region_dir(rk).glob("route_*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        direction = r.get("dir")
        for s in r["stops"]:
            sid = str(s["id"])
            if sid in seen:
                if direction not in seen[sid]["dirs"]:
                    seen[sid]["dirs"].append(direction)
                continue
            seen[sid] = {"id": sid, "name": s.get("name", ""),
                         "kname": s.get("kname", ""), "dirs": [direction]}
            out.append(seen[sid])
    return out


def _load_geo_merged(rk: str):
    """Original spline geometry with any admin position overrides merged in."""
    p = region_dir(rk) / "geo.json"
    if not p.exists():
        return None
    geo = json.loads(p.read_text(encoding="utf-8"))
    ovp = region_dir(rk) / "geo_overrides.json"
    if ovp.exists():
        ov = json.loads(ovp.read_text(encoding="utf-8"))
        for d in ("up", "down"):
            for s in geo.get(d, {}).get("stops", []):
                if str(s["id"]) in ov:
                    s["xy"] = ov[str(s["id"])]
    return geo


@app.get("/api/geo")
async def geo(region: str = ""):
    g = _load_geo_merged(valid_region(region or DEFAULT_REGION))
    if g is None:
        return JSONResponse({"error": "no geo"}, status_code=404)
    return JSONResponse(g)


@app.get("/api/geo_bg")
async def geo_bg(region: str = ""):
    p = region_dir(valid_region(region or DEFAULT_REGION)) / "geo_bg.png"
    if not p.exists():
        return JSONResponse({"error": "no bg"}, status_code=404)
    return FileResponse(p, media_type="image/png")


@app.get("/api/config")
async def config():
    return {"editable": not READONLY}


# ── admin editing (local only) ───────────────────────────────────────────
class KnameEdit(BaseModel):
    region: str = ""
    id: str
    kname: str


class StopPos(BaseModel):
    region: str = ""
    dir: str = "up"
    id: str
    x: float
    y: float


class StopReset(BaseModel):
    region: str = ""
    dir: str = "up"
    id: str


def _rebuild_region(rk: str) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "build_region.py"), rk],
                       capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "build_region failed")[-500:]
    ID2DIR[rk] = build_id2dir(rk)
    return True, "ok"


@app.post("/api/kname")
async def edit_kname(e: KnameEdit):
    if READONLY:
        return JSONResponse({"error": "이 서버는 보기 전용입니다. 편집은 로컬에서 하세요."}, status_code=403)
    rk = valid_region(e.region or DEFAULT_REGION)
    kname = e.kname.strip()
    sid = re.sub(r"[^0-9]", "", e.id)
    if not sid or not kname:
        return JSONResponse({"error": "bad request"}, status_code=400)
    ov_path = region_dir(rk) / "kname_overrides.json"
    ov = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    ov[sid] = kname
    ov_path.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")
    ok, msg = _rebuild_region(rk)
    if not ok:
        return JSONResponse({"error": msg}, status_code=500)
    return {"ok": True, "id": sid, "kname": kname}


def _geo_ov_path(rk: str) -> Path:
    return region_dir(rk) / "geo_overrides.json"


@app.post("/api/stoppos")
async def edit_stoppos(e: StopPos):
    if READONLY:
        return JSONResponse({"error": "이 서버는 보기 전용입니다. 편집은 로컬에서 하세요."}, status_code=403)
    rk = valid_region(e.region or DEFAULT_REGION)
    xy = [round(e.x, 1), round(e.y, 1)]
    p = _geo_ov_path(rk)
    ov = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    ov[str(e.id)] = xy
    p.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "id": str(e.id), "xy": xy}


@app.post("/api/stoppos/reset")
async def reset_stoppos(e: StopReset):
    if READONLY:
        return JSONResponse({"error": "보기 전용"}, status_code=403)
    rk = valid_region(e.region or DEFAULT_REGION)
    p = _geo_ov_path(rk)
    ov = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    ov.pop(str(e.id), None)
    p.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")
    base = _load_geo_merged(rk)          # override just removed -> original
    d = e.dir if e.dir in ("up", "down") else "up"
    hit = next((s for s in base[d]["stops"] if str(s["id"]) == str(e.id)), None) if base else None
    return {"ok": True, "id": str(e.id), "xy": hit["xy"] if hit else None}


# ── notices (공지사항): site-wide; read for all, write/delete admin only ──────
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
    return sorted(_load_notices(), key=lambda n: n.get("id", 0), reverse=True)


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
    _save_notices([i for i in _load_notices() if i.get("id") != nid])
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})
