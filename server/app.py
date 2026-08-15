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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tripscan  # noqa: E402  (맵 TTData 훑기 — build_region.py와 같은 규칙)

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
# Where this machine keeps its OMSI maps — only used by the admin "지역 추가" GUI,
# which is local-only anyway (the cloud copy has no OMSI install).
OMSI_MAPS = Path(os.environ.get(
    "OMSI_MAPS_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\OMSI 2\maps"))

app = FastAPI(title="OMSI BIS server")
buses: dict[str, dict] = {}


# ── regions ──────────────────────────────────────────────────────────────
def region_lines(reg: dict) -> list[dict]:
    """A region is a MAP with many lines: [{"line", "trips":{"up","down"}}].
    Older single-line entries ({"line","trips"}) still read as a one-line region."""
    raw = reg.get("lines")
    if not raw:
        return [{"line": reg.get("line", ""), "trips": reg.get("trips") or {}}]
    return [{"line": l, "trips": {}} if isinstance(l, str)
            else {"line": l["line"], "trips": l.get("trips") or {}} for l in raw]


def load_regions() -> list[dict]:
    p = DATA_DIR / "regions.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def save_regions(regs: list[dict]):
    (DATA_DIR / "regions.json").write_text(
        json.dumps(regs, ensure_ascii=False, indent=2), encoding="utf-8")


REGIONS = load_regions()
DEFAULT_REGION = REGIONS[0]["key"] if REGIONS else "segang"
REGION_KEYS = {r["key"] for r in REGIONS}
MAP2REGION = {r.get("map", ""): r["key"] for r in REGIONS}      # OMSI map name -> region


def region_dir(key: str) -> Path:
    return REGIONS_DIR / key


def valid_region(key: str) -> str:
    return key if key in REGION_KEYS else DEFAULT_REGION


def build_id2dir(key: str) -> dict[int, list[dict]]:
    """stop-id -> the routes that serve it, so a bus's schedule tells us both its
    line and its direction (a region now holds many lines, sharing stops)."""
    d: dict[int, list[dict]] = {}
    for f in sorted(region_dir(key).glob("route_*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        info = {"key": r.get("key", ""), "no": r.get("no", ""), "dir": r.get("dir")}
        for s in r["stops"]:
            d.setdefault(int(s["id"]), []).append(info)
    return d


ID2DIR: dict[str, dict[int, list[dict]]] = {r["key"]: build_id2dir(r["key"]) for r in REGIONS}


def reload_regions():
    """Re-read regions.json after the admin adds/removes one (no restart needed)."""
    global REGIONS, DEFAULT_REGION, REGION_KEYS, MAP2REGION, ID2DIR
    REGIONS = load_regions()
    DEFAULT_REGION = REGIONS[0]["key"] if REGIONS else "segang"
    REGION_KEYS = {r["key"] for r in REGIONS}
    MAP2REGION = {r.get("map", ""): r["key"] for r in REGIONS}
    ID2DIR = {r["key"]: build_id2dir(r["key"]) for r in REGIONS}


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
    route = prev.get("route") if prev else None
    cands = ID2DIR.get(region, {}).get(u.nextIdCode, []) if u.schedValid else []
    if cands:
        # the driver's line number picks between routes sharing this stop
        same = [c for c in cands if u.line and c["no"] == u.line]
        hit = (same or cands)[0]
        direction, route = hit["dir"], hit["key"]
    buses[u.id] = {
        "id": u.id, "nick": u.nick, "line": u.line, "map": u.map, "region": region,
        "vehNo": u.vehNo, "company": u.company, "dir": direction, "route": route,
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
    return [{"key": r["key"], "name": r["name"], "map": r.get("map", ""),
             "lines": [e["line"] for e in region_lines(r)]} for r in REGIONS]


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
    key = re.sub(r"[^0-9A-Za-z._\-]", "", key)      # sanitize path segment (line ids like 8-1)
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


_GEO_CACHE: dict[str, tuple[tuple, dict]] = {}      # 한 지역의 geo.json은 수 MB까지 간다


def _load_geo_merged(rk: str):
    """Original spline geometry (all lines, one shared pixel frame) with any admin
    position overrides merged in. Pre-multiline files are normalized on read."""
    p = region_dir(rk) / "geo.json"
    if not p.exists():
        return None
    ovp = region_dir(rk) / "geo_overrides.json"
    stamp = (p.stat().st_mtime_ns, ovp.stat().st_mtime_ns if ovp.exists() else 0)
    cached = _GEO_CACHE.get(rk)
    if cached and cached[0] == stamp:
        return cached[1]
    geo = json.loads(p.read_text(encoding="utf-8"))
    if "lines" not in geo:                       # 옛 형식: 지역에 노선 하나뿐이던 시절
        one = {d: geo.pop(d) for d in ("up", "down") if d in geo}
        first = next((json.loads(f.read_text(encoding="utf-8")).get("no", "")
                      for f in sorted(region_dir(rk).glob("route_*.json"))), "")
        geo["lines"] = {first: one} if one else {}
    if ovp.exists():
        ov = json.loads(ovp.read_text(encoding="utf-8"))
        for per in geo["lines"].values():
            for d in ("up", "down"):
                for s in per.get(d, {}).get("stops", []):
                    if str(s["id"]) in ov:
                        s["xy"] = ov[str(s["id"])]
    _GEO_CACHE[rk] = (stamp, geo)
    return geo


@app.get("/api/geo")
async def geo(region: str = "", line: str = ""):
    """One line's geometry ({W,H,up,down}); every line shares the same frame."""
    g = _load_geo_merged(valid_region(region or DEFAULT_REGION))
    if g is None or not g.get("lines"):
        return JSONResponse({"error": "no geo"}, status_code=404)
    per = g["lines"].get(line) or (g["lines"].get(next(iter(g["lines"]))) if not line else None)
    if per is None:
        return JSONResponse({"error": "no geo for line"}, status_code=404)
    return JSONResponse({"W": g["W"], "H": g["H"], "bg": g.get("bg", ""), **per})


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
    """Run build_region.py for one region; returns (ok, build log)."""
    here = Path(__file__).resolve().parent
    # 한글 윈도우면 파이프 기본 인코딩이 cp949라 로그 한 줄에 빌드가 통째로 죽는다
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, str(here / "build_region.py"), rk],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=str(here))
    log = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        return False, (log or "build_region failed")[-1500:]
    ID2DIR[rk] = build_id2dir(rk)
    return True, log[-1500:]


# ── admin: region (map) management — powers the 지역 추가 GUI ─────────────
def _slug(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


@app.get("/api/maps")
async def maps_list():
    """Every OMSI map here, its trip files (.ttp) grouped by line number.

    No naming convention is assumed — the admin picks which trip file is the
    up/down direction. `track` says whether a route path (.ttr) was found, i.e.
    whether this line can appear on the map at all (tripscan does the matching,
    the same way build_region.py will)."""
    if READONLY or not OMSI_MAPS.is_dir():
        return []
    out = []
    for d in sorted(p for p in OMSI_MAPS.iterdir() if p.is_dir()):
        lines = tripscan.scan(d / "TTData")
        if lines:
            out.append({"map": d.name, "region": MAP2REGION.get(d.name), "lines": lines})
    return out


class LineIn(BaseModel):
    line: str
    tripUp: str = ""        # .ttp stem (empty = the "<line> A" convention)
    tripDown: str = ""


class RegionNew(BaseModel):
    key: str = ""
    name: str
    map: str
    lines: list[LineIn] = []
    # 예전(노선 1개) 형식도 그대로 받는다
    line: str = ""
    tripUp: str = ""
    tripDown: str = ""


def _auto_lines(mp: str) -> list[LineIn]:
    """Every line the map offers, with its first two trip files as up/down —
    'add the whole map at once' is the normal case."""
    out = []
    for g in tripscan.scan(OMSI_MAPS / mp / "TTData"):
        files = [t["file"] for t in g["trips"]]
        up = next((f for f in files if f.strip().upper().endswith(" A")), files[0])
        down = next((f for f in files if f.strip().upper().endswith(" B")),
                    next((f for f in files if f != up), ""))
        out.append(LineIn(line=g["line"], tripUp=up, tripDown=down))
    return out


def _check_lines(mp: str, lines: list[LineIn]) -> tuple[list[dict] | None, str]:
    tt = OMSI_MAPS / mp / "TTData"
    out, seen = [], set()
    for l in lines:
        ln = l.line.strip()
        if not ln or ln in seen:
            continue
        seen.add(ln)
        up = (l.tripUp or f"{ln} A").strip()
        down = (l.tripDown or "").strip()
        if not (tt / f"{up}.ttp").exists():
            return None, f"운행 파일이 없습니다: {up}.ttp"
        if down and not (tt / f"{down}.ttp").exists():
            return None, f"운행 파일이 없습니다: {down}.ttp"
        out.append({"line": ln, "trips": {"up": up, "down": down}})
    if not out:
        return None, "노선을 하나 이상 지정하세요."
    return out, ""


@app.post("/api/regions")
async def add_region(r: RegionNew):
    if READONLY:
        return JSONResponse({"error": "지역 추가는 로컬(관리자)에서만 가능합니다."}, status_code=403)
    name, mp = r.name.strip(), r.map.strip()
    key = _slug(r.key) or _slug(mp)
    if not (name and mp and key):
        return JSONResponse({"error": "지역 이름과 맵을 지정하세요."}, status_code=400)
    regs = load_regions()
    if any(x["key"] == key for x in regs):
        return JSONResponse({"error": f"이미 쓰는 지역 키입니다: {key}"}, status_code=409)
    if any(x.get("map") == mp for x in regs):
        return JSONResponse({"error": f"이 맵은 이미 등록돼 있습니다: {mp}"}, status_code=409)
    if not (OMSI_MAPS / mp / "TTData").is_dir():
        return JSONResponse({"error": f"맵을 찾을 수 없습니다: {mp}"}, status_code=400)
    wanted = r.lines or ([LineIn(line=r.line, tripUp=r.tripUp, tripDown=r.tripDown)]
                         if r.line else _auto_lines(mp))       # 지정 없으면 맵 전체
    lines, err = _check_lines(mp, wanted)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    save_regions(regs + [{"key": key, "name": name, "map": mp, "lines": lines}])
    ok, log = await asyncio.to_thread(_rebuild_region, key)      # keeps the server responsive
    if not ok:
        save_regions(regs)                  # failed build -> leave no half-registered region
        reload_regions()
        return JSONResponse({"error": "노선 데이터 생성 실패", "log": log}, status_code=500)
    reload_regions()
    return {"ok": True, "key": key, "name": name, "log": log}


@app.post("/api/regions/{key}/rebuild")
async def rebuild_region(key: str):
    if READONLY:
        return JSONResponse({"error": "재생성은 로컬(관리자)에서만 가능합니다."}, status_code=403)
    key = _slug(key)
    if key not in REGION_KEYS:
        return JSONResponse({"error": "없는 지역입니다."}, status_code=404)
    ok, log = await asyncio.to_thread(_rebuild_region, key)
    if not ok:
        return JSONResponse({"error": "재생성 실패", "log": log}, status_code=500)
    return {"ok": True, "key": key, "log": log}


@app.post("/api/regions/{key}/lines")
async def add_line(key: str, l: LineIn):
    """OMSI 에디터로 노선을 새로 만든 뒤 그 노선만 지역에 붙일 때."""
    if READONLY:
        return JSONResponse({"error": "노선 추가는 로컬(관리자)에서만 가능합니다."}, status_code=403)
    key = _slug(key)
    regs = load_regions()
    reg = next((x for x in regs if x["key"] == key), None)
    if not reg:
        return JSONResponse({"error": "없는 지역입니다."}, status_code=404)
    have = region_lines(reg)
    if any(e["line"] == l.line.strip() for e in have):
        return JSONResponse({"error": f"이미 있는 노선입니다: {l.line}"}, status_code=409)
    checked, err = _check_lines(reg.get("map", ""), [l])
    if err:
        return JSONResponse({"error": err}, status_code=400)
    reg.pop("line", None); reg.pop("trips", None)          # 옛 단일노선 필드 정리
    reg["lines"] = [{"line": e["line"], "trips": e["trips"]} for e in have] + checked
    save_regions(regs)
    ok, log = await asyncio.to_thread(_rebuild_region, key)
    if not ok:
        return JSONResponse({"error": "노선 데이터 생성 실패", "log": log}, status_code=500)
    reload_regions()
    return {"ok": True, "key": key, "line": checked[0]["line"], "log": log}


@app.delete("/api/regions/{key}/lines/{line}")
async def del_line(key: str, line: str):
    if READONLY:
        return JSONResponse({"error": "노선 삭제는 로컬(관리자)에서만 가능합니다."}, status_code=403)
    key = _slug(key)
    regs = load_regions()
    reg = next((x for x in regs if x["key"] == key), None)
    if not reg:
        return JSONResponse({"error": "없는 지역입니다."}, status_code=404)
    have = region_lines(reg)
    left = [e for e in have if e["line"] != line]
    if len(left) == len(have):
        return JSONResponse({"error": "없는 노선입니다."}, status_code=404)
    if not left:
        return JSONResponse({"error": "지역의 마지막 노선입니다. 지역째 삭제하세요."}, status_code=400)
    reg.pop("line", None); reg.pop("trips", None)
    reg["lines"] = [{"line": e["line"], "trips": e["trips"]} for e in left]
    save_regions(regs)
    ok, log = await asyncio.to_thread(_rebuild_region, key)
    reload_regions()
    if not ok:
        return JSONResponse({"error": "재생성 실패", "log": log}, status_code=500)
    return {"ok": True, "key": key, "line": line, "log": log}


@app.delete("/api/regions/{key}")
async def del_region(key: str):
    if READONLY:
        return JSONResponse({"error": "지역 삭제는 로컬(관리자)에서만 가능합니다."}, status_code=403)
    key = _slug(key)
    regs = load_regions()
    if not any(x["key"] == key for x in regs):
        return JSONResponse({"error": "없는 지역입니다."}, status_code=404)
    if len(regs) <= 1:
        return JSONResponse({"error": "최소 한 개 지역은 있어야 합니다."}, status_code=400)
    save_regions([x for x in regs if x["key"] != key])
    reload_regions()
    # data/regions/<key>/ 는 그대로 둔다 — 다시 추가하면 손수 고친 이름·위치가 살아난다.
    return {"ok": True, "key": key}


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
    hit = None
    for per in (base or {}).get("lines", {}).values():
        for d in ("up", "down"):
            hit = hit or next((s for s in per.get(d, {}).get("stops", [])
                               if str(s["id"]) == str(e.id)), None)
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
