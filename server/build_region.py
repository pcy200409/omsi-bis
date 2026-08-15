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
import sys
from pathlib import Path

TOOL = r"C:\Users\pcy20\OneDrive\Desktop\CC\OMSI_TTData_Tool"
sys.path.insert(0, TOOL)
import omsi_ttdata as T  # noqa: E402

OMSI_MAPS = Path(r"C:\Program Files (x86)\Steam\steamapps\common\OMSI 2\maps")
DATA = Path(__file__).resolve().parent.parent / "data"
MAPW, SS = 1600, 1


def has_hangul(s: str) -> bool:
    return any("가" <= c <= "힣" for c in (s or ""))


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


def find_track(ttdata: Path, stem: str, line: str) -> Path | None:
    """.ttr naming varies: same as the trip, or just the line number, or absent."""
    for cand in (stem, line):
        p = ttdata / f"{cand}.ttr"
        if p.exists():
            return p
    return None


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

    def kname_for(st):
        sid = str(st["index"])
        if sid in OV:
            return OV[sid]
        ko = T.koreanize(st["name"])                         # auto RR->Hangul + glossary
        return ko if has_hangul(ko) else st["name"]

    def mkroute(trip, line, rkey, direction, first, last):
        stops = [{"id": str(st["index"]), "name": st["name"], "kname": kname_for(st),
                  "kachel": st.get("busstop", ""), "dist": round(st.get("dist", 0.0), 1)}
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

    # ── map geometry (spline reconstruction) ────────────────────────────
    # Every line of the region shares ONE pixel frame / background, so the map
    # can show them together and a stop keeps the same spot on every line.
    tracks = {}          # line -> {dir: .ttr path}
    for ent in entries:
        line, trips = ent["line"], trips_of.get(ent["line"], {})
        if not trips:
            continue
        tracks[line] = {d: find_track(ttdata, trips[d], line) for d in ("up", "down")}
    if not any(t for per in tracks.values() for t in per.values()):
        log("  ! no track files at all - strip view only (map view is skipped)")
        return
    log("  indexing tile splines + road map ...")
    splines = T.index_splines(str(mapdir))
    if not splines:
        log("  ! no tile splines - skipping map (strip view still works)")
        return
    try:
        roadmap = T.load_roadmap(str(mapdir))       # 맵마다 없거나 깨져 있기도 하다
    except Exception as e:
        log(f"  ! roadmap image unusable ({type(e).__name__}) - plain background")
        roadmap = None

    geos = {}            # line -> {dir: (trip, geo)}
    for ent in entries:
        line, parsed = ent["line"], ent.get("_parsed") or {}
        for d in ("up", "down"):
            trf = tracks.get(line, {}).get(d)
            if not parsed.get(d) or not trf:
                continue
            g = T.build_geo(parsed[d], T.parse_track(str(trf)), splines)
            if g:
                geos.setdefault(line, {})[d] = (parsed[d], g)
    if not geos:
        log("  ! geometry reconstruction failed - skipping map")
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
        path = [[round(px(x, z)[0], 1), round(px(x, z)[1], 1)] for s in geo["segs"] for x, z in s["pts"]]
        return {"stops": stops, "path": path}

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
