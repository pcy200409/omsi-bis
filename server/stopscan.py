"""맵 타일에서 정류장 좌표 복원 — 경로파일(.ttr)이 없는 맵을 위해.

.ttr이 있으면 노선 경로를 따라 정류장 위치가 나오지만(build_region의 기본 경로),
트랙을 안 넣은 맵도 많다. 그런 맵도 지도에 정류장은 찍을 수 있다: OMSI 타일
(tile_x_y.map)에 정류장 오브젝트가 들어 있기 때문이다. 두 가지로 저장된다.

  [object]            정류장이 독립 오브젝트 — 타일 안 좌표가 그대로 들어 있다
  [splineAttachement] 도로 스플라인에 붙은 정류장 — "그 도로의 몇 m 지점에서
                      옆으로 얼마" 형태라, 직전 [spline]의 기하로 풀어야 한다

두 경우 모두 오브젝트의 ID 칸이 Busstops.cfg의 정류장 id와 같아서, 노선의
정류장 목록과 바로 맞출 수 있다.
"""
from __future__ import annotations

import glob
import math
import os
import re

TILE = 300.0                      # OMSI 타일 한 변(m)


def _spline_geom(body: list[str], gx: int, gy: int) -> dict | None:
    try:
        return {"x": gx*TILE + float(body[5]), "z": gy*TILE + float(body[7]),
                "hdg": float(body[8]), "len": float(body[9]), "rad": float(body[10])}
    except (ValueError, IndexError):
        return None


def _points(s: dict, step: float = 2.0) -> list[tuple[float, float]]:
    """스플라인(직선 또는 원호)을 폴리라인으로 — 도구의 spline_points와 같은 식."""
    R, L, th0 = s["rad"], s["len"], math.radians(s["hdg"])
    if abs(R) < 1e-6:
        return [(s["x"], s["z"]), (s["x"] + L*math.sin(th0), s["z"] + L*math.cos(th0))]
    n = max(2, int(abs(L) / step) + 1)
    pts = []
    for k in range(n + 1):
        th = th0 + (L * k / n) / R
        pts.append((s["x"] + R*(math.cos(th0) - math.cos(th)),
                    s["z"] + R*(math.sin(th) - math.sin(th0))))
    return pts


def _at(pts, dist: float, off: float) -> tuple[float, float]:
    """폴리라인에서 dist(m) 지점, 진행방향 기준 오른쪽으로 off(m) 떨어진 점."""
    cum = [0.0]
    for k in range(1, len(pts)):
        cum.append(cum[-1] + math.dist(pts[k-1], pts[k]))
    t = min(max(dist, 0.0), cum[-1])
    k = next((j for j in range(1, len(cum)) if cum[j] >= t), len(cum) - 1)
    span = cum[k] - cum[k-1]
    f = (t - cum[k-1]) / span if span > 1e-9 else 0.0
    (x0, z0), (x1, z1) = pts[k-1], pts[k]
    px, pz = x0 + (x1-x0)*f, z0 + (z1-z0)*f
    dx, dz = x1-x0, z1-z0
    n = math.hypot(dx, dz) or 1.0
    return px + off*dz/n, pz - off*dx/n


def _is_stop(path: str) -> bool:
    p = path.lower()
    return p.endswith(".sco") and ("stop" in p or "halte" in p) and "routearrows" not in p


def stops_from_tiles(mapdir: str, log=None) -> dict[int, tuple[float, float]]:
    """정류장 id -> 월드좌표 (x, z). 타일을 전부 훑으므로 지역 빌드당 한 번만."""
    from omsi_ttdata import read_text          # 인코딩(UTF-16/8bit) 판별은 도구 것 재사용
    out: dict[int, tuple[float, float]] = {}
    for tp in sorted(glob.glob(os.path.join(mapdir, "tile_*.map"))):
        m = re.match(r"tile_(-?\d+)_(-?\d+)\.map$", os.path.basename(tp))
        if not m:
            continue
        gx, gy = int(m.group(1)), int(m.group(2))
        lines = [l.strip() for l in read_text(tp).splitlines()]
        cur = None                              # 직전 [spline] — 붙임 정류장이 참조한다
        for i, head in enumerate(lines):
            if head in ("[spline]", "[spline_h]"):
                cur = _spline_geom(lines[i+1:i+12], gx, gy)
            elif head == "[object]":
                b = lines[i+1:i+12]
                if len(b) > 4 and _is_stop(b[1]):
                    try:
                        out[int(b[2])] = (gx*TILE + float(b[3]), gy*TILE + float(b[4]))
                    except (ValueError, IndexError):
                        pass
            elif head == "[splineAttachement]" and cur:
                b = lines[i+1:i+12]
                if len(b) > 6 and _is_stop(b[1]):
                    try:
                        out[int(b[2])] = _at(_points(cur), float(b[6]), float(b[4]))
                    except (ValueError, IndexError):
                        pass
    if log:
        log(f"  tiles: 정류장 오브젝트 {len(out)}개에서 좌표 복원")
    return out
