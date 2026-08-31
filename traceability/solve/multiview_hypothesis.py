"""Phase 5：多视图 2D 证据 → 3D 杆件假设（06 段试点）。

front 面 (x,z) 与 left/right 面 (y,z) 的 dxf_geom 斜材段关联，
生成 geometry_origin=multiview_hypothesis 的 3D 对角假设（B 类 reconstructed）。

设计约束（无 GT）：
  * 只关联 z 跨度重叠 ≥50% 的段对；
  * 长度比 ∈ [0.5, 2.0]；
  * 异号象限（x/y 均非零时）优先——depth diagonal 守门；
  * 假设杆不替换已有 dtd_/diagonal_topology 杆，只补全 front-only 不可见成员。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]
NodeMap = Dict[str, Vec3]


def _bar_endpoints_xz(nodes: NodeMap, bar: dict) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    f = nodes.get(bar.get("from"))
    t = nodes.get(bar.get("to"))
    if f is None or t is None:
        return None
    return ((f[0], f[2]), (t[0], t[2]))


def _bar_endpoints_yz(nodes: NodeMap, bar: dict) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    f = nodes.get(bar.get("from"))
    t = nodes.get(bar.get("to"))
    if f is None or t is None:
        return None
    return ((f[1], f[2]), (t[1], t[2]))


def _z_span(seg: Tuple[Tuple[float, float], Tuple[float, float]]) -> Tuple[float, float]:
    z1, z2 = seg[0][1], seg[1][1]
    return (min(z1, z2), max(z1, z2))


def _seg_len2d(seg: Tuple[Tuple[float, float], Tuple[float, float]]) -> float:
    (a0, a1), (b0, b1) = seg
    return math.hypot(b0 - a0, b1 - a1)


def _z_overlap_ratio(
    sa: Tuple[Tuple[float, float], Tuple[float, float]],
    sb: Tuple[Tuple[float, float], Tuple[float, float]],
) -> float:
    alo, ahi = _z_span(sa)
    blo, bhi = _z_span(sb)
    inter = max(0.0, min(ahi, bhi) - max(alo, blo))
    denom = max(min(ahi - alo, bhi - blo), 1e-9)
    return inter / denom


def _collect_view_segments(
    nodes: NodeMap,
    bars: List[dict],
    *,
    sheet: str,
    faces: Sequence[str],
    z_window: Optional[Tuple[float, float]],
    axis: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in bars:
        p = b.get("properties") or b
        if str(p.get("geometry_origin") or "") != "dxf_geom":
            continue
        if str(p.get("source_file") or "") != sheet:
            continue
        face = str(p.get("face") or "f").lower()
        if face not in {f.lower() for f in faces}:
            continue
        if str(p.get("role") or "").upper() not in ("DIAG", ""):
            continue
        seg = (_bar_endpoints_xz if axis == "xz" else _bar_endpoints_yz)(nodes, b)
        if seg is None:
            continue
        zlo, zhi = _z_span(seg)
        if z_window and (zhi < z_window[0] or zlo > z_window[1]):
            continue
        ln = _seg_len2d(seg)
        if ln < 400.0:
            continue
        out.append({
            "bar_id": str(b.get("id")),
            "face": face,
            "axis": axis,
            "seg": seg,
            "len2d": ln,
            "z_mid": (zlo + zhi) / 2.0,
        })
    return out


def associate_multiview_pairs(
    front_segs: List[Dict[str, Any]],
    side_segs: List[Dict[str, Any]],
    *,
    min_z_overlap: float = 0.5,
    len_ratio_lo: float = 0.5,
    len_ratio_hi: float = 2.0,
) -> List[Tuple[Dict[str, Any], Dict[str, Any], float]]:
    """front ↔ side 段配对（贪心：每 front 最多一对）。"""
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any], float]] = []
    used_side: set = set()
    for f in front_segs:
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for s in side_segs:
            if s["bar_id"] in used_side:
                continue
            ov = _z_overlap_ratio(f["seg"], s["seg"])
            if ov < min_z_overlap:
                continue
            lr = f["len2d"] / max(s["len2d"], 1e-9)
            if not (len_ratio_lo <= lr <= len_ratio_hi):
                continue
            # 评分：z 中点差 + 长度差（越小越好）
            score = abs(f["z_mid"] - s["z_mid"]) + abs(f["len2d"] - s["len2d"])
            if best is None or score < best[0]:
                best = (score, s)
        if best:
            used_side.add(best[1]["bar_id"])
            pairs.append((f, best[1], best[0]))
    return pairs


def _hypothesis_endpoints_3d(
    fseg: Tuple[Tuple[float, float], Tuple[float, float]],
    sseg: Tuple[Tuple[float, float], Tuple[float, float]],
    hw_fn,
) -> Tuple[Vec3, Vec3]:
    """z 对齐端点：x 来自 front，y 来自 side，z 取两侧均值。"""
    (fx1, fz1), (fx2, fz2) = fseg
    (fy1, sz1), (fy2, sz2) = sseg
    if fz1 <= fz2:
        z_a = (fz1 + sz1) / 2.0
        z_b = (fz2 + sz2) / 2.0
        x_a, x_b = fx1, fx2
        y_a, y_b = fy1, fy2
    else:
        z_a = (fz2 + sz2) / 2.0
        z_b = (fz1 + sz1) / 2.0
        x_a, x_b = fx2, fx1
        y_a, y_b = fy2, fy1
    return (x_a, y_a, z_a), (x_b, y_b, z_b)


def apply_multiview_hypotheses(
    nodes: NodeMap,
    bars: List[dict],
    hw_fn,
    *,
    sheet: str = "35A1-JC1-06",
    z_window: Optional[Tuple[float, float]] = None,
    level_source_label: Optional[str] = None,
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """06 段试点：生成 multiview_hypothesis 杆并写入模型。"""
    front = _collect_view_segments(
        nodes, bars, sheet=sheet, faces=("f",), z_window=z_window, axis="xz")
    side = _collect_view_segments(
        nodes, bars, sheet=sheet, faces=("l", "r"), z_window=z_window, axis="yz")
    pairs = associate_multiview_pairs(front, side)

    new_nodes: NodeMap = dict(nodes)
    new_bars = list(bars)
    gen: List[dict] = []
    counter = {"n": 950000}

    def _find_or_add(pos: Vec3) -> str:
        for nid, p in new_nodes.items():
            if all(abs(p[i] - pos[i]) <= 300.0 for i in range(3)):
                return nid
        counter["n"] += 1
        nid = f"mvn_{counter['n']}"
        new_nodes[nid] = (round(pos[0], 3), round(pos[1], 3), round(pos[2], 3))
        return nid

    for i, (f, s, score) in enumerate(pairs):
        a, b = _hypothesis_endpoints_3d(f["seg"], s["seg"], hw_fn)
        n1, n2 = _find_or_add(a), _find_or_add(b)
        if n1 == n2:
            continue
        bid = f"mvh_{sheet.replace('-', '_')}_{i}"
        gen.append({
            "id": bid,
            "from": n1,
            "to": n2,
            "role": "DIAG",
            "geometry_class": "reconstructed",
            "geometry_origin": "multiview_hypothesis",
            "level_source": level_source_label,
            "source_file": sheet,
            "multiview_pair": [f["bar_id"], s["bar_id"]],
            "multiview_score": round(score, 1),
            "face_front": f["face"],
            "face_side": s["face"],
        })
    new_bars.extend(gen)

    report = {
        "sheet": sheet,
        "z_window": list(z_window) if z_window else None,
        "n_front_segments": len(front),
        "n_side_segments": len(side),
        "n_pairs": len(pairs),
        "n_generated": len(gen),
        "pairs": [
            {"front": f["bar_id"], "side": s["bar_id"], "score": round(sc, 1)}
            for f, s, sc in pairs[:20]
        ],
    }
    return new_nodes, new_bars, report
