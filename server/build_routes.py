"""Rebuild both directions of line 124 from the CURRENT map data.

Sources (all in the map's TTData folder):
  Busstops.cfg   -> id -> {name (romanized), kachel}
  124 A.ttp      -> ordered station ids (상행 / up)
  124 B.ttp      -> ordered station ids (하행 / down)   [new [station_typ2] format]

Korean names are overlaid from the previously-built data/route_124A.json (by id),
and for 하행 by stripping the trailing " A"/" E" direction suffix and matching the
same romanized base as 상행. Anything still unmatched keeps the romanized name and
is listed as needing a Korean name.

Distances: 상행 reuses the real cumulative distances we already had (by id) and
linearly interpolates the two inserted stops; 하행 mirrors 상행 (total - paired) and
interpolates the rest, then is forced monotonic. Approximate but fine for the strip.
"""
import json, re
from pathlib import Path

MAP = Path(r"C:\Program Files (x86)\Steam\steamapps\common\OMSI 2\maps\Segang Alpha\TTData")
DATA = Path(__file__).resolve().parent.parent / "data"


def read_text(p: Path) -> str:
    b = p.read_bytes()
    return b.decode("utf-16") if b[:2] == b"\xff\xfe" else b.decode("utf-8", errors="replace")


def parse_busstops(p: Path) -> dict:
    lines = [l.rstrip("\r") for l in read_text(p).split("\n")]
    m, i = {}, 0
    while i < len(lines):
        if lines[i].strip() == "[busstop]":
            m[lines[i + 3].strip()] = {"name": lines[i + 1].strip(), "kachel": lines[i + 2].strip()}
            i += 4
        else:
            i += 1
    return m


def ttp_ids(p: Path) -> list:
    lines = [l.rstrip("\r") for l in read_text(p).split("\n")]
    return [lines[i + 1].strip() for i, l in enumerate(lines) if l.strip().startswith("[station")]


def strip_dir(name: str) -> str:
    return re.sub(r"\s+[A-E]$", "", name).strip()


def interp_monotonic(dists, total):
    """Fill None entries by linear interpolation between known anchors, then force
    strictly increasing."""
    n = len(dists)
    if dists[0] is None:
        dists[0] = 0.0
    if dists[-1] is None:
        dists[-1] = total
    # forward-fill gaps by interpolation between nearest known neighbours
    i = 0
    while i < n:
        if dists[i] is None:
            j = i
            while j < n and dists[j] is None:
                j += 1
            lo, hi = dists[i - 1], dists[j] if j < n else total
            for k in range(i, j):
                dists[k] = lo + (hi - lo) * (k - i + 1) / (j - i + 1)
            i = j
        else:
            i += 1
    # enforce monotonic increasing with a small minimum gap
    for i in range(1, n):
        if dists[i] <= dists[i - 1]:
            dists[i] = dists[i - 1] + 50.0
    return dists


master = parse_busstops(MAP / "Busstops.cfg")
ids_up = ttp_ids(MAP / "124 A.ttp")
ids_dn = ttp_ids(MAP / "124 B.ttp")

# manual Korean-name overrides, keyed by STOP ID (the editing GUI writes this file).
# These are AUTHORITATIVE — they win over every automatic lookup, so a name the user
# fixed by hand is never overwritten by a map re-parse. Keys are stop-id strings.
_ov = DATA / "kname_overrides.json"
OVERRIDES = json.loads(_ov.read_text(encoding="utf-8")) if _ov.exists() else {}

# previous Korean names + real distances (by id) from the earlier 상행 build.
# Only treat a kname as a real Korean name if it actually contains Hangul — the
# build writes romanized fallbacks into kname too, and we must not mistake those
# for translations on the next run (that would shadow kname_overrides.json).
def has_hangul(s: str) -> bool:
    return any("가" <= c <= "힣" for c in (s or ""))

prev = json.loads((DATA / "route_124A.json").read_text(encoding="utf-8"))
kname_by_id = {s["id"]: s["kname"] for s in prev["stops"] if has_hangul(s.get("kname", ""))}
dist_by_id = {s["id"]: s["dist"] for s in prev["stops"]}
# romanized-base -> Korean, for pairing 하행 to 상행 Korean names
kname_by_romaji = {strip_dir(s["name"]): s["kname"]
                   for s in prev["stops"] if has_hangul(s.get("kname", ""))}
# romanized-base -> real 상행 distance, for mirroring to 하행
dist_by_romaji = {master[i]["name"] if i in master else None: dist_by_id.get(i)
                  for i in ids_up if i in dist_by_id}

needs_korean = []


def build(ids, key, label, direction):
    stops = []
    total_up = max(dist_by_id.values())
    for idx, sid in enumerate(ids):
        m = master.get(sid, {})
        romaji = m.get("name", f"id{sid}")
        base = strip_dir(romaji)
        # id-keyed manual override wins; else reuse prior Korean names / pairing.
        kname = (OVERRIDES.get(str(sid)) or kname_by_id.get(sid)
                 or kname_by_romaji.get(base))
        if not kname:
            needs_korean.append((key, idx, sid, romaji))
        # distance
        if direction == "up":
            dist = dist_by_id.get(sid)
        else:  # down: mirror the paired 상행 stop
            paired = dist_by_romaji.get(base)
            dist = (total_up - paired) if paired is not None else None
        stops.append({"id": sid, "name": romaji, "kname": kname or romaji,
                      "kachel": m.get("kachel"), "dist": dist})
    dists = interp_monotonic([s["dist"] for s in stops], max(dist_by_id.values()))
    for s, d in zip(stops, dists):
        s["dist"] = round(d, 1)
    route = {
        "key": key, "no": "124", "type": "일반", "dir": direction, "label": label,
        "from": stops[0]["kname"], "to": stops[-1]["kname"],
        "length": stops[-1]["dist"], "stops": stops,
    }
    (DATA / f"route_{key}.json").write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")
    return route


up = build(ids_up, "124A", "124 상행", "up")
dn = build(ids_dn, "124B", "124 하행", "down")

# routes index for the UI toggle
index = [{"key": "124A", "no": "124", "dir": "up",
          "label": f"124 상행 · {up['from']}→{up['to']}", "stops": len(up["stops"])},
         {"key": "124B", "no": "124", "dir": "down",
          "label": f"124 하행 · {dn['from']}→{dn['to']}", "stops": len(dn["stops"])}]
(DATA / "routes.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"124A: {len(up['stops'])} stops, {up['from']} -> {up['to']}, {up['length']:.0f} m")
print(f"124B: {len(dn['stops'])} stops, {dn['from']} -> {dn['to']}, {dn['length']:.0f} m")
print(f"\nstops still needing a Korean name: {len(needs_korean)}")
for key, idx, sid, romaji in needs_korean:
    print(f"  {key} #{idx:2} id={sid:<7} {romaji}")
