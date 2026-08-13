"""Build the map-view geometry for line 124 by REUSING the OMSI_TTData_Tool's
spline-reconstruction (it already restores stop GPS coords from the map tiles and
overlays them on the road-network image).

Outputs (committed, so the cloud map works too):
  web/geo_bg.png    — road-network background cropped to the 124 route area
  data/geo_124.json — per-direction stop pixel coords + route polyline, in the
                      SAME pixel frame as geo_bg.png (both directions share it).

Run:  python build_geo.py
"""
import json
import os
import sys
from pathlib import Path

TOOL = r"C:\Users\pcy20\OneDrive\Desktop\CC\OMSI_TTData_Tool"
sys.path.insert(0, TOOL)
import omsi_ttdata as T  # noqa: E402

MAP = Path(r"C:\Program Files (x86)\Steam\steamapps\common\OMSI 2\maps\Segang Alpha")
TTDATA = MAP / "TTData"
DATA = Path(__file__).resolve().parent.parent / "data"
WEB = Path(__file__).resolve().parent.parent / "web"

MAPW = 1600      # output image width in px (SS=1 keeps the file small)
SS = 1


def make_geo(tid: str):
    trip = T.parse_ttp(str(TTDATA / f"{tid}.ttp"))
    track = T.parse_track(str(TTDATA / f"{tid}.ttr"))
    geo = T.build_geo(trip, track, splines)
    return trip, geo


print("indexing tile splines + road map …")
splines = T.index_splines(str(MAP))
roadmap = T.load_roadmap(str(MAP))
print(f"  splines={len(splines)}  roadmap={'ok' if roadmap else 'MISSING'}")

tripA, geoA = make_geo("124 A")
tripB, geoB = make_geo("124 B")
if not geoA or not geoB:
    raise SystemExit("build_geo failed (no geometry) — check .ttr/splines")

# one shared pixel frame + background covering BOTH directions
bg, px, W, H = T._geo_canvas([geoA, geoB], roadmap, MAPW, SS)
if bg is None:
    from PIL import Image
    bg = Image.new("RGB", (W, H), "white")
WEB.mkdir(exist_ok=True)
bg.save(WEB / "geo_bg.png")
print(f"  geo_bg.png {W}x{H} saved")


def P(x, z):
    a, b = px(x, z)
    return [round(a, 1), round(b, 1)]


def emit(trip, geo):
    stops = []
    for st, (x, z) in zip(trip["stations"], geo["stops"]):
        stops.append({"id": str(st["index"]), "xy": P(x, z)})
    # flatten the reconstructed arcs into one polyline (segment joins auto-bridge
    # the small gaps build_geo left at wrong-tile skips / intersections)
    path = []
    for s in geo["segs"]:
        for x, z in s["pts"]:
            path.append(P(x, z))
    return {"stops": stops, "path": path}


out = {"W": W, "H": H, "bg": "geo_bg.png",
       "up": emit(tripA, geoA), "down": emit(tripB, geoB)}
(DATA / "geo_124.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
print(f"  geo_124.json: up={len(out['up']['stops'])} stops/"
      f"{len(out['up']['path'])} pts, down={len(out['down']['stops'])} stops/"
      f"{len(out['down']['path'])} pts")

# cross-check the geo stop ids line up with the route JSON the strip uses
for key, d in (("124A", "up"), ("124B", "down")):
    rp = DATA / f"route_{key}.json"
    if rp.exists():
        rid = [str(s["id"]) for s in json.loads(rp.read_text(encoding="utf-8"))["stops"]]
        gid = [s["id"] for s in out[d]["stops"]]
        match = sum(1 for a, b in zip(rid, gid) if a == b)
        print(f"  {key}: id match {match}/{len(rid)} (route vs geo, by index)")
