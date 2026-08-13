"""Parse an OMSI .ttp (Time Table Trip) file into a route JSON for the BIS strip.

Each [station] block:
    [station]
    <id>
    <f2>
    <name>
    <f4>
    <f5>
    <dist>     # cumulative distance from route start, metres
    <f7>
    ...
Usage:  python parse_route.py "<map>" "<trip.ttp>" <out.json>
"""
import json, sys
from pathlib import Path

def read_text(p: Path) -> str:
    b = p.read_bytes()
    if b[:2] == b"\xff\xfe":
        return b.decode("utf-16")
    return b.decode("utf-8", errors="replace")

def parse(ttp: Path) -> dict:
    lines = [l.rstrip("\r") for l in read_text(ttp).split("\n")]
    trip_name = line_no = dest = ""
    stops = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s == "[trip]":
            trip_name = lines[i+1].strip()
            dest = lines[i+2].strip()
            line_no = lines[i+3].strip()
            i += 4; continue
        if s == "[station]":
            try:
                stops.append({
                    "id":   lines[i+1].strip(),
                    "name": lines[i+3].strip(),
                    "dist": float(lines[i+6].strip()),
                })
            except (ValueError, IndexError):
                pass
            i += 9; continue
        i += 1
    return {
        "no": line_no or trip_name.split()[0] if trip_name else "",
        "type": "일반",
        "trip": trip_name,
        "dest": dest,
        "from": stops[0]["name"] if stops else "",
        "to": stops[-1]["name"] if stops else "",
        "length": stops[-1]["dist"] if stops else 0,
        "stops": stops,
    }

if __name__ == "__main__":
    ttp = Path(sys.argv[1]); out = Path(sys.argv[2])
    route = parse(ttp)
    out.write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{out.name}: {len(route['stops'])} stops, {route['length']:.0f} m, {route['from']} -> {route['to']}")
