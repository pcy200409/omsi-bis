"""OMSI 맵의 TTData 훑기 — 운행파일(.ttp)이 어떤 노선으로 묶이고, 그 운행의
경로 트랙(.ttr)이 무엇인지.

맵마다 이름 규칙이 제각각이라 한 곳에 모아 둔다:
  운행:  "124 A"  ·  "92 (to Munsan Univ)"  ·  "725_WBG_hin"  ·  "102_1"
  트랙:  운행과 같은 이름 / 노선번호만 / 또 다른 이름("1(1)", "156_A_-_B")
트랙이 아예 없는 맵도 흔하다(그럼 지도 없이 정류장 목록만 만든다).

서버가 가볍게 import 할 수 있도록 표준 라이브러리만 쓴다.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

SIMILAR_MIN = 0.55        # 이름이 다른 트랙을 붙일 때 요구하는 최소 유사도


def line_of(stem: str) -> str:
    """운행파일 이름의 맨 앞이 노선: "124 A"·"92 (to X)"·"725_WBG_hin" -> 124·92·725"""
    m = re.match(r"[^\s_(]+", stem.strip())
    return m.group(0) if m else stem.strip()


def line_sort(line: str):
    m = re.match(r"(\d+)(.*)", line)
    return (0, int(m.group(1)), m.group(2)) if m else (1, 0, line)


def track_stems(ttdata: Path) -> list[str]:
    return sorted(f.stem for f in ttdata.glob("*.ttr"))


def find_track(ttdata: Path, stem: str, line: str, ttrs: list[str] | None = None) -> Path | None:
    """이 운행의 경로 트랙 찾기: 같은 이름 -> 노선번호 -> 같은 노선의 트랙 중 가장
    비슷한 이름. (마지막 규칙이 없으면 "1(1).ttr" 같은 맵이 통째로 지도를 잃는다)"""
    for cand in (stem, line):
        p = ttdata / f"{cand}.ttr"
        if p.exists():
            return p
    ttrs = track_stems(ttdata) if ttrs is None else ttrs
    same = [t for t in ttrs if line_of(t) == line]
    if not same:
        return None
    if len(same) == 1:
        return ttdata / f"{same[0]}.ttr"
    best = max(same, key=lambda c: difflib.SequenceMatcher(None, c.lower(), stem.lower()).ratio())
    ratio = difflib.SequenceMatcher(None, best.lower(), stem.lower()).ratio()
    return ttdata / f"{best}.ttr" if ratio >= SIMILAR_MIN else None


def scan(ttdata: Path) -> list[dict]:
    """[{line, trips:[{file, track}]}] — track=이 운행에 붙일 .ttr이 있는가."""
    if not ttdata.is_dir():
        return []
    ttrs = track_stems(ttdata)
    groups: dict[str, list[str]] = {}
    for f in sorted(ttdata.glob("*.ttp")):
        groups.setdefault(line_of(f.stem), []).append(f.stem)
    return [{"line": ln,
             "trips": [{"file": s, "track": find_track(ttdata, s, ln, ttrs) is not None}
                       for s in groups[ln]]}
            for ln in sorted(groups, key=line_sort)]
