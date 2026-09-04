"""P2.1：主腿链分组——四角柱 → z 排序母杆链（DXF 证据，无 GT 层位）。

输出供 subdivide_legs_at_levels / 召回诊断使用；每条链带长度守恒摘要。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

Vec3 = Tuple[float, float, float]
NodeMap = Dict[str, Vec3]


def _corner_key(x: float, y: float, tol: float = 0.15) -> Tuple[int, int]:
    """象限键：按 x/y 符号分四角（容差带内归 0 轴）。"""
    sx = 0 if abs(x) < tol else (1 if x > 0 else -1)
    sy = 0 if abs(y) < tol else (1 if y > 0 else -1)
    return (sx, sy)


def build_leg_chains(
    nodes: NodeMap,
    bars: List[dict],
    *,
    vertical_min_ratio: float = 0.85,
    min_len_mm: float = 200.0,
) -> Dict[str, Any]:
    """主腿杆 → 四角链（按 z 排序的 segment 列表）。

    只取 role=LEG、近竖直（|dz|/L >= vertical_min_ratio）杆件。
    """
    segs_by_corner: Dict[Tuple[int, int], List[dict]] = defaultdict(list)
    skipped = {"not_leg": 0, "not_vertical": 0, "short": 0, "no_node": 0}

    for b in bars:
        role = str(b.get("role") or "").upper()
        if role not in ("LEG", ""):
            skipped["not_leg"] += 1
            continue
        if role == "" and not b.get("corner_leg"):
            skipped["not_leg"] += 1
            continue
        f = nodes.get(b.get("from"))
        t = nodes.get(b.get("to"))
        if f is None or t is None:
            skipped["no_node"] += 1
            continue
        dx = abs(t[0] - f[0])
        dy = abs(t[1] - f[1])
        dz = abs(t[2] - f[2])
        L = math.hypot(dx, dy, dz)
        if L < min_len_mm:
            skipped["short"] += 1
            continue
        if dz / max(L, 1e-9) < vertical_min_ratio:
            skipped["not_vertical"] += 1
            continue
        mid = ((f[0] + t[0]) / 2, (f[1] + t[1]) / 2, (f[2] + t[2]) / 2)
        ck = _corner_key(mid[0], mid[1])
        z_lo, z_hi = sorted((f[2], t[2]))
        segs_by_corner[ck].append({
            "bar_id": str(b.get("id")),
            "z_lo": z_lo,
            "z_hi": z_hi,
            "len_mm": L,
            "from": b.get("from"),
            "to": b.get("to"),
        })

    chains: List[Dict[str, Any]] = []
    for ck, segs in sorted(segs_by_corner.items()):
        segs.sort(key=lambda s: (s["z_lo"], s["z_hi"]))
        z_min = min(s["z_lo"] for s in segs)
        z_max = max(s["z_hi"] for s in segs)
        sum_len = sum(s["len_mm"] for s in segs)
        chains.append({
            "corner": ck,
            "n_segments": len(segs),
            "z_span": (round(z_min, 1), round(z_max, 1)),
            "sum_seg_len_mm": round(sum_len, 1),
            "span_mm": round(z_max - z_min, 1),
            "segments": segs,
        })

    return {
        "n_corners": len(chains),
        "chains": chains,
        "skipped": skipped,
    }
