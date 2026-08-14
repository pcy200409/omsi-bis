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


def trip_stems(reg: dict) -> dict:
    """Which .ttp file is each direction? (explicit, else the "<line> A/B" convention)"""
    t = reg.get("trips") or {}
    line = reg["line"]
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
    line = reg["line"]
    if not ttdata.exists():
        raise SystemExit(f"map TTData not found: {ttdata}")
    out = DATA / "regions" / key
    out.mkdir(parents=True, exist_ok=True)
    trips = trip_stems(reg)
    log(f"building region '{key}'  map='{reg['map']}'  line={line}  trips={trips}")

    ovf = out / "kname_overrides.json"                       # authoritative Korean names
    OV = json.loads(ovf.read_text(encoding="utf-8")) if ovf.exists() else {}
    first, last = ttl_first_last(ttdata, line, trips["up"])

    def kname_for(st):
        sid = str(st["index"])
        if sid in OV:
            return OV[sid]
        ko = T.koreanize(st["name"])                         # auto RR->Hangul + glossary
        return ko if has_hangul(ko) else st["name"]

    def parse_dir(direction):
        p = ttdata / f"{trips[direction]}.ttp"
        return T.parse_ttp(str(p)) if p.exists() else None

    tripA, tripB = parse_dir("up"), parse_dir("down")
    if not tripA:
        raise SystemExit(f"trip file not found: {ttdata / (trips['up'] + '.ttp')}")

    def mkroute(trip, rkey, direction):
        stops = [{"id": str(st["index"]), "name": st["name"], "kname": kname_for(st),
                  "kachel": st.get("busstop", ""), "dist": round(st.get("dist", 0.0), 1)}
                 for st in trip["stations"]]
        return {"key": rkey, "no": line, "type": "일반", "dir": direction,
                "from": stops[0]["kname"], "to": stops[-1]["kname"],
                "length": stops[-1]["dist"], "first": first, "last": last, "stops": stops}

    routes, index = {}, []
    upA = mkroute(tripA, f"{line}A", "up"); routes[f"{line}A"] = upA
    index.append({"key": f"{line}A", "no": line, "dir": "up",
                  "label": f"{line} 상행 · {upA['from']}→{upA['to']}", "stops": len(upA["stops"])})
    if tripB:
        dnB = mkroute(tripB, f"{line}B", "down"); routes[f"{line}B"] = dnB
        index.append({"key": f"{line}B", "no": line, "dir": "down",
                      "label": f"{line} 하행 · {dnB['from']}→{dnB['to']}", "stops": len(dnB["stops"])})
    for rkey, r in routes.items():
        (out / f"route_{rkey}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "routes.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  routes: " + ", ".join(f"{k}={len(r['stops'])} stops" for k, r in routes.items()))

    # ── map geometry (spline reconstruction) ────────────────────────────
    # Only worth the (slow) spline indexing if some direction has a track file.
    tracks = {d: find_track(ttdata, trips[d], line) for d in ("up", "down")}
    for d, trf in tracks.items():
        if not trf:
            log(f"  ! {d}: no .ttr track - this direction gets no map")
    if not any(tracks.values()):
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
    geos = {}
    for direction in ("up", "down"):
        trip = tripA if direction == "up" else tripB
        if not trip or not tracks[direction]:
            continue
        g = T.build_geo(trip, T.parse_track(str(tracks[direction])), splines)
        if g:
            geos[direction] = (trip, g)
    if not geos:
        log("  ! geometry reconstruction failed - skipping map")
        return
    bg, px, W, H = T._geo_canvas([g for _, g in geos.values()], roadmap, MAPW, SS)
    if bg is None:
        from PIL import Image
        bg = Image.new("RGB", (W, H), "white")
    bg.save(out / "geo_bg.png")

    def emit(trip, geo):
        stops = [{"id": str(st["index"]), "xy": [round(px(x, z)[0], 1), round(px(x, z)[1], 1)]}
                 for st, (x, z) in zip(trip["stations"], geo["stops"])]
        path = [[round(px(x, z)[0], 1), round(px(x, z)[1], 1)] for s in geo["segs"] for x, z in s["pts"]]
        return {"stops": stops, "path": path}

    geo_out = {"W": W, "H": H, "bg": "geo_bg.png"}
    for direction, (trip, g) in geos.items():
        geo_out[direction] = emit(trip, g)
    (out / "geo.json").write_text(json.dumps(geo_out, ensure_ascii=False), encoding="utf-8")
    log(f"  geo_bg.png {W}x{H} + geo.json ({', '.join(geos)})")
    log(f"done: region '{key}'")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python build_region.py <region_key>")
    build_region(sys.argv[1])
