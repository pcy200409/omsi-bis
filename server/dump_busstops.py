"""Dump the map's Busstops.cfg master (id -> name, kachel) to a readable file."""
import json
from pathlib import Path

BASE = Path(r"C:\Program Files (x86)\Steam\steamapps\common\OMSI 2\maps\Segang Alpha")

def read_text(p: Path) -> str:
    b = p.read_bytes()
    return b.decode("utf-16") if b[:2] == b"\xff\xfe" else b.decode("utf-8", errors="replace")

def parse_busstops(p: Path) -> dict:
    lines = [l.rstrip("\r") for l in read_text(p).split("\n")]
    master = {}
    i = 0
    while i < len(lines):
        if lines[i].strip() == "[busstop]":
            name = lines[i+1].strip()
            kachel = lines[i+2].strip()
            bid = lines[i+3].strip()
            master[bid] = {"name": name, "kachel": kachel}
            i += 4
        else:
            i += 1
    return master

def ttp_ids(p: Path):
    lines = [l.rstrip("\r") for l in read_text(p).split("\n")]
    out = []
    for i, l in enumerate(lines):
        if l.strip().startswith("[station"):
            out.append(lines[i+1].strip())
    return out

master = parse_busstops(BASE / "TTData" / "Busstops.cfg")
a = ttp_ids(BASE / "TTData" / "124 A.ttp")
b = ttp_ids(BASE / "TTData" / "124 B.ttp")

out = Path(__file__).resolve().parent / "busstops_dump.txt"
with out.open("w", encoding="utf-8") as f:
    f.write(f"master busstops: {len(master)}\n\n")
    f.write(f"=== 124 A (상행) {len(a)} stops ===\n")
    for i, sid in enumerate(a):
        m = master.get(sid, {})
        f.write(f"{i:2} id={sid:<7} kachel={m.get('kachel','?'):<4} {m.get('name','<NOT IN MASTER>')}\n")
    f.write(f"\n=== 124 B (하행) {len(b)} stops ===\n")
    for i, sid in enumerate(b):
        m = master.get(sid, {})
        f.write(f"{i:2} id={sid:<7} kachel={m.get('kachel','?'):<4} {m.get('name','<NOT IN MASTER>')}\n")
print(f"wrote {out}")
