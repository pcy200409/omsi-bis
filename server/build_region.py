"""Build one region's route + map data from its OMSI map.

Map-agnostic: reuses OMSI_TTData_Tool's parsers (handles both .ttp formats,
pulls real distances from StnLinks.cfg, and auto-transliterates Korean names),
and its spline reconstruction for the map geometry. So adding a new map only
needs an entry in data/regions.json and a run of this script.

Trip files are named freely per map ("124 A.ttp", "92 (to Munsan Univ).ttp",
"725_WBG_hin.ttp" …), so the region entry names them explicitly:

  {"key","name","map","line", "trips": {"up": "<ttp stem>", "down": "<ttp stem>"}}

Without "trips" we fall back to the "<line> A"/"<line> B" convention. The track
(.ttr, only needed for the map view) is looked up by trip stem, then by line
number; a map with no track still gets the route strip.

Usage:  python build_region.py <region_key>
Outputs data/regions/<key>/{routes.json, route_<line>A.json, route_<line>B.json,
geo.json, geo_bg.png}. Manual kname/geo overrides (same folder) are respected.
"""
import json
import math
import sys
from pathlib import Path

TOOL = r"C:\Users\pcy20\OneDrive\Desktop\CC\OMSI_TTData_Tool"
sys.path.insert(0, TOOL)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import omsi_ttdata as T  # noqa: E402
from tripscan import find_track  # noqa: E402  (맵마다 다른 .ttr 이름 규칙 흡수)
from stopscan import stops_from_tiles  # noqa: E402  (.ttr 없는 맵: 타일에서 정류장 좌표)

OMSI_MAPS = Path(r"C:\Program Files (x86)\Steam\steamapps\common\OMSI 2\maps")
DATA = Path(__file__).resolve().parent.parent / "data"
MAPW, SS = 1600, 1

# 노선도/시간표 도구와 같은 용어사전을 쓴다 — 한 정류장이 두 곳에서 다른 이름으로
# 나오면 안 되니, 사전 파일은 도구 쪽 out/ 하나만 본다.
GLOSSARY = Path(TOOL) / "out" / "glossary_ko.csv"        # 용어 규칙 (구절/단어)
NAMEMAP = Path(TOOL) / "out" / "stationnames_ko.csv"     # 영문 정류장명 -> 한글


def has_hangul(s: str) -> bool:
    return any("가" <= c <= "힣" for c in (s or ""))


def load_terms(log=print):
    """용어사전을 koreanize()에 물리고, 정류장명 대조표를 돌려준다."""
    if GLOSSARY.exists():
        T.USER_PHRASES, T.USER_WORDS = T.load_glossary(str(GLOSSARY))
        if T.USER_PHRASES or T.USER_WORDS:
            log(f"  용어사전: 구절 {len(T.USER_PHRASES)} · 단어 {len(T.USER_WORDS)}")
    names = T.load_name_map(str(NAMEMAP)) if NAMEMAP.exists() else {}
    if names:
        log(f"  정류장명 대조표: {len(names)}개 (노선도 도구와 공용)")
    return names


def hhmm(m) -> str:
    return f"{int(m) // 60 % 24:02d}:{int(m) % 60:02d}" if m is not None else ""


def region_lines(reg: dict) -> list[dict]:
    """A region is a MAP and carries many lines: [{"line", "trips":{"up","down"}}].
    Old single-line entries ({"line","trips"}) are read as a one-line region."""
    raw = reg.get("lines")
    if not raw:
        return [{"line": reg["line"], "trips": reg.get("trips") or {}}]
    out = []
    for l in raw:
        if isinstance(l, str):
            out.append({"line": l, "trips": {}})
        else:
            out.append({"line": l["line"], "trips": l.get("trips") or {}})
    return out


def trip_stems(ent: dict) -> dict:
    """Which .ttp file is each direction? (explicit, else the "<line> A/B" convention)"""
    t = ent.get("trips") or {}
    line = ent["line"]
    return {"up": t.get("up") or f"{line} A", "down": t.get("down") or f"{line} B"}


def ttl_first_last(ttdata: Path, line: str, stem: str = ""):
    p = ttdata / f"{line}.ttl"
    if not p.exists() and stem:
        p = ttdata / f"{stem}.ttl"
    if not p.exists():
        return "", ""
    lines = [l.rstrip("\r") for l in T.read_text(str(p)).split("\n")]
    deps = []
    for i, l in enumerate(lines):
        if l.strip() == "[addtrip]":
            try:
                deps.append(float(lines[i + 3].strip()))
            except (ValueError, IndexError):
                pass
    return (hhmm(min(deps)), hhmm(max(deps))) if deps else ("", "")


SNAP_MAX = 60.0          # 타일 정류장이 복원 경로에서 이보다 멀면 보정하지 않는다


def refine_stops(geo: dict, trip: dict, tiles: dict, log=print) -> int:
    """트랙 '거리'로 찍은 정류장을 타일의 실제 정류장 위치로 보정한다.

    거리 기반은 정류장이 경로 끝에서 한 점으로 뭉치거나(마커가 겹쳐 사라진다)
    수백 m 밀리는 일이 있다. 타일에 있는 실제 정류장 오브젝트를 경로선에 투영해
    제자리에 놓되, 노선 진행순서(뒤로 안 감)는 지킨다."""
    pts = [p for s in geo["segs"] for p in s["pts"]]
    if len(pts) < 2:
        return 0
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + math.dist(pts[i-1], pts[i]))

    def project(p, from_s):
        best = None
        for i in range(1, len(pts)):
            if cum[i] < from_s:
                continue
            (x0, z0), (x1, z1) = pts[i-1], pts[i]
            vx, vz = x1-x0, z1-z0
            L = vx*vx + vz*vz
            t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0]-x0)*vx + (p[1]-z0)*vz) / L))
            qx, qz = x0 + vx*t, z0 + vz*t
            d = math.dist(p, (qx, qz))
            if best is None or d < best[0]:
                best = (d, cum[i-1] + math.dist((x0, z0), (qx, qz)), (qx, qz))
        return best

    fixed, prev_s = 0, 0.0
    for k, st in enumerate(trip["stations"]):
        t = tiles.get(int(st["index"]))
        if not t:
            continue
        hit = project(t, prev_s)
        if not hit or hit[0] > SNAP_MAX:
            continue
        geo["stops"][k] = hit[2]
        prev_s = hit[1]
        fixed += 1
    if fixed:
        log(f"    정류장 {fixed}/{len(trip['stations'])}개를 타일 위치로 보정")
    return fixed


def build_region(key: str, log=print):
    regions = json.loads((DATA / "regions.json").read_text(encoding="utf-8"))
    reg = next((r for r in regions if r["key"] == key), None)
    if not reg:
        raise SystemExit(f"region '{key}' not found in data/regions.json")
    mapdir = OMSI_MAPS / reg["map"]
    ttdata = mapdir / "TTData"
    if not ttdata.exists():
        raise SystemExit(f"map TTData not found: {ttdata}")
    out = DATA / "regions" / key
    out.mkdir(parents=True, exist_ok=True)
    entries = region_lines(reg)
    log(f"building region '{key}'  map='{reg['map']}'  lines={[e['line'] for e in entries]}")

    ovf = out / "kname_overrides.json"                       # authoritative Korean names
    OV = json.loads(ovf.read_text(encoding="utf-8")) if ovf.exists() else {}
    exf = out / "extra_stops.json"          # 운행파일에 없는데 사람이 넣은 정류장
    EXTRA = json.loads(exf.read_text(encoding="utf-8")) if exf.exists() else {}

    NAMES = load_terms(log)                 # 노선도/시간표 도구와 같은 용어사전
    learned = {}                            # BIS에서 손으로 고친 이름 -> 대조표에 돌려준다

    def kname_for(st):
        sid, en = str(st["index"]), st["name"]
        if sid in OV:                                        # BIS에서 직접 고친 이름이 최우선
            if en and OV[sid] != NAMES.get(en):
                learned[en] = OV[sid]
            return OV[sid]
        if en in NAMES and NAMES[en].strip():                # 도구 쪽에서 정한 이름
            return NAMES[en]
        ko = T.koreanize(en)                                 # auto RR->Hangul + glossary
        return ko if has_hangul(ko) else en

    def add_extras(trip, line, direction):
        """운행파일에 빠진 정류장을 사람이 넣어둔 대로 끼워 넣는다(재생성해도 유지).
        trip 자체에 넣어야 노선 목록·지도 좌표가 같은 순서로 따라온다."""
        for e in (EXTRA.get(line) or {}).get(direction, []):
            sid = int(e["id"])
            if any(int(st["index"]) == sid for st in trip["stations"]):
                continue                                     # 이미 있으면 건너뛴다
            after = str(e.get("after", ""))
            at = next((i+1 for i, st in enumerate(trip["stations"])
                       if str(st["index"]) == after), 0 if after == "" else len(trip["stations"]))
            prev = trip["stations"][at-1]["dist"] if at > 0 else 0.0
            nxt = trip["stations"][at]["dist"] if at < len(trip["stations"]) else prev + 400.0
            trip["stations"].insert(at, {"index": sid, "name": e.get("name") or str(sid),
                                         "busstop": "", "dist": (prev + nxt) / 2,
                                         "_extra": True})

    def mkroute(trip, line, rkey, direction, first, last):
        stops = [{"id": str(st["index"]), "name": st["name"], "kname": kname_for(st),
                  "kachel": st.get("busstop", ""), "dist": round(st.get("dist", 0.0), 1),
                  **({"added": True} if st.get("_extra") else {})}
                 for st in trip["stations"]]
        return {"key": rkey, "no": line, "type": "일반", "dir": direction,
                "from": stops[0]["kname"], "to": stops[-1]["kname"],
                "length": stops[-1]["dist"], "first": first, "last": last, "stops": stops}

    routes, index, trips_of = {}, [], {}
    for ent in entries:
        line, trips = ent["line"], trip_stems(ent)
        trips_of[line] = trips
        parsed = {}
        for d in ("up", "down"):
            p = ttdata / f"{trips[d]}.ttp"
            parsed[d] = T.parse_ttp(str(p)) if p.exists() else None
        if not parsed["up"]:
            log(f"  ! {line}: trip file not found ({trips['up']}.ttp) - skipped")
            continue
        for d in ("up", "down"):
            if parsed[d]:
                add_extras(parsed[d], line, d)
        first, last = ttl_first_last(ttdata, line, trips["up"])
        for d, suffix, label in (("up", "A", "상행"), ("down", "B", "하행")):
            if not parsed[d]:
                continue
            rkey = f"{line}{suffix}"
            r = mkroute(parsed[d], line, rkey, d, first, last)
            routes[rkey] = r
            index.append({"key": rkey, "no": line, "dir": d,
                          "label": f"{line} {label} · {r['from']}→{r['to']}", "stops": len(r["stops"])})
        ent["_parsed"] = parsed
    if not routes:
        raise SystemExit("no line could be built - check the trip files in data/regions.json")

    for stale in out.glob("route_*.json"):                   # 빠진 노선 파일은 정리
        if stale.stem[len("route_"):] not in routes:
            stale.unlink()
    for rkey, r in routes.items():
        (out / f"route_{rkey}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "routes.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  routes: " + ", ".join(f"{k}={len(r['stops'])} stops" for k, r in routes.items()))

    if learned and NAMEMAP.exists():        # BIS에서 고친 이름을 도구 쪽 대조표에도 반영
        NAMES.update(learned)
        T.save_name_map(str(NAMEMAP), NAMES)
        log(f"  정류장명 대조표에 {len(learned)}개 반영 (노선도/시간표에도 같은 이름)")

    # ── map geometry (spline reconstruction) ────────────────────────────
    # Every line of the region shares ONE pixel frame / background, so the map
    # can show them together and a stop keeps the same spot on every line.
    tracks = {}          # line -> {dir: .ttr path}
    for ent in entries:
        line, trips = ent["line"], trips_of.get(ent["line"], {})
        if not trips:
            continue
        tracks[line] = {d: find_track(ttdata, trips[d], line) for d in ("up", "down")}
    has_track = any(t for per in tracks.values() for t in per.values())
    log("  indexing tile splines + road map ..." if has_track
        else "  no .ttr track - recovering stop positions from the map tiles ...")
    splines = T.index_splines(str(mapdir)) if has_track else {}
    try:
        roadmap = T.load_roadmap(str(mapdir))       # 맵마다 없거나 깨져 있기도 하다
    except Exception as e:
        log(f"  ! roadmap image unusable ({type(e).__name__}) - plain background")
        roadmap = None

    tile_stops: dict | None = None                  # 타일에서 뽑은 정류장 좌표(한 번만)

    def tiles():
        nonlocal tile_stops
        if tile_stops is None:
            tile_stops = stops_from_tiles(str(mapdir), log)
        return tile_stops

    def stops_only(trip):
        """트랙 없는 노선: 타일의 정류장 오브젝트 위치만으로 지오를 만든다.
        (노선 선은 못 그리고 버스는 정류장 사이를 직선으로 간다)"""
        pts = [tiles().get(int(st["index"])) for st in trip["stations"]]
        known = [i for i, p in enumerate(pts) if p]
        if len(known) < 2:
            return None
        for i, p in enumerate(pts):                 # 못 찾은 정류장은 이웃 사이로 보간
            if p:
                continue
            lo = max([k for k in known if k < i], default=known[0])
            hi = min([k for k in known if k > i], default=known[-1])
            f = 0.5 if lo == hi else (i-lo)/(hi-lo)
            pts[i] = (pts[lo][0] + (pts[hi][0]-pts[lo][0])*f,
                      pts[lo][1] + (pts[hi][1]-pts[lo][1])*f)
        # segs 는 화면 범위 계산용으로만 쓰인다 (노선 선으로는 내보내지 않는다)
        return {"segs": [{"d0": 0, "d1": 1, "pts": pts}], "stops": pts, "noroute": True}

    geos = {}            # line -> {dir: (trip, geo)}
    for ent in entries:
        line, parsed = ent["line"], ent.get("_parsed") or {}
        for d in ("up", "down"):
            if not parsed.get(d):
                continue
            trf = tracks.get(line, {}).get(d)
            g = T.build_geo(parsed[d], T.parse_track(str(trf)), splines) if trf else None
            if g:
                log(f"  {line} {d}:")
                refine_stops(g, parsed[d], tiles(), log)   # 겹치거나 밀린 정류장 바로잡기
            else:
                g = stops_only(parsed[d])           # 트랙이 없거나 복원 실패
            if g:
                geos.setdefault(line, {})[d] = (parsed[d], g)
    if not geos:
        log("  ! no geometry at all - strip view only (map view is skipped)")
        return
    bg, px, W, H = T._geo_canvas([g for per in geos.values() for _, g in per.values()],
                                 roadmap, MAPW, SS)
    if bg is None:
        from PIL import Image
        bg = Image.new("RGB", (W, H), "white")
    bg.save(out / "geo_bg.png")

    def emit(trip, geo):
        stops = [{"id": str(st["index"]), "xy": [round(px(x, z)[0], 1), round(px(x, z)[1], 1)]}
                 for st, (x, z) in zip(trip["stations"], geo["stops"])]
        path = ([] if geo.get("noroute") else                 # 트랙 없는 노선은 선을 안 그린다
                [[round(px(x, z)[0], 1), round(px(x, z)[1], 1)]
                 for s in geo["segs"] for x, z in s["pts"]])
        return {"stops": stops, "path": path, "noroute": bool(geo.get("noroute"))}

    geo_out = {"W": W, "H": H, "bg": "geo_bg.png",
               "lines": {line: {d: emit(trip, g) for d, (trip, g) in per.items()}
                         for line, per in geos.items()}}
    (out / "geo.json").write_text(json.dumps(geo_out, ensure_ascii=False), encoding="utf-8")
    log(f"  geo_bg.png {W}x{H} + geo.json (" +
        ", ".join(f"{line}:{'/'.join(per)}" for line, per in geos.items()) + ")")
    log(f"done: region '{key}'")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python build_region.py <region_key>")
    build_region(sys.argv[1])
