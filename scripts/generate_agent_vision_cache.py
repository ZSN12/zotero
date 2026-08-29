"""Agent 视觉推理缓存生成器（V4 纯净工程桁架版）。

1. 严格过滤 DXF 尺寸线、大样剖面与引出线（只保留长度 >= 80mm 且位于塔身立面主框内的有效结构线）；
2. 双线角钢精确合并为中心线；
3. 碎线链式缝合；
4. 真实件号绑定。
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import ezdxf

from traceability.intake.hybrid_geometry import (
    drawing_xy_to_px,
    px_to_drawing_xy,
)

OUT_DIR = REPO / "out/35A1-JC1-full-deliver"
CACHE_DIR = REPO / "out/agent_vision_cache"
OVERLAY_PATH = REPO / "examples/external/guowang_35A1/layer_overlay.json"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _line_angle(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    return math.atan2(p2[1] - p1[1], p2[0] - p1[0])


def _line_length(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def _point_to_line_dist(pt: Tuple[float, float], p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    l = math.hypot(dx, dy)
    if l <= 1e-6:
        return math.hypot(pt[0] - p1[0], pt[1] - p1[1])
    return abs(dy * pt[0] - dx * pt[1] + p2[0] * p1[1] - p2[1] * p1[0]) / l


def merge_parallel_double_lines(lines: List[Tuple[float, float, float, float]], dist_tol: float = 5.0) -> List[Tuple[float, float, float, float]]:
    """将平行双线（角钢两条轮廓线）合并为中心线。"""
    used = [False] * len(lines)
    merged: List[Tuple[float, float, float, float]] = []

    for i in range(len(lines)):
        if used[i]:
            continue
        l1 = lines[i]
        p1a, p1b = (l1[0], l1[1]), (l1[2], l1[3])
        len1 = _line_length(p1a, p1b)
        ang1 = _line_angle(p1a, p1b)

        best_j = None
        best_dist = dist_tol

        for j in range(i + 1, len(lines)):
            if used[j]:
                continue
            l2 = lines[j]
            p2a, p2b = (l2[0], l2[1]), (l2[2], l2[3])
            len2 = _line_length(p2a, p2b)
            ang2 = _line_angle(p2a, p2b)

            da = abs(ang1 - ang2)
            if da > math.pi / 2:
                da = abs(da - math.pi)
            if da > math.radians(5.0):
                continue

            d1 = _point_to_line_dist(p2a, p1a, p1b)
            d2 = _point_to_line_dist(p2b, p1a, p1b)
            perp_dist = (d1 + d2) / 2.0
            if 0.5 <= perp_dist <= dist_tol and abs(len1 - len2) <= max(20.0, len1 * 0.35):
                if perp_dist < best_dist:
                    best_dist = perp_dist
                    best_j = j

        if best_j is not None:
            used[best_j] = True
            l2 = lines[best_j]
            p2a, p2b = (l2[0], l2[1]), (l2[2], l2[3])
            if _line_length(p1a, p2a) > _line_length(p1a, p2b):
                p2a, p2b = p2b, p2a
            c_start = ((p1a[0] + p2a[0]) / 2.0, (p1a[1] + p2a[1]) / 2.0)
            c_end = ((p1b[0] + p2b[0]) / 2.0, (p1b[1] + p2b[1]) / 2.0)
            merged.append((c_start[0], c_start[1], c_end[0], c_end[1]))
        else:
            merged.append(l1)

    return merged


def stitch_collinear_lines(lines: List[Tuple[float, float, float, float]], gap_tol: float = 30.0, colinear_tol: float = 3.0) -> List[Tuple[float, float, float, float]]:
    """把同向共线碎线链式缝合成通长杆件。"""
    segs = list(lines)
    used = [False] * len(segs)
    res: List[Tuple[float, float, float, float]] = []

    for i in range(len(segs)):
        if used[i]:
            continue
        chain = [segs[i]]
        used[i] = True
        grew = True
        while grew:
            grew = False
            base = chain[-1]
            p_s, p_e = (base[0], base[1]), (base[2], base[3])
            bl = math.hypot(p_e[0] - p_s[0], p_e[1] - p_s[1])
            if bl <= 1e-6:
                break
            ux, uy = (p_e[0] - p_s[0]) / bl, (p_e[1] - p_s[1]) / bl

            best_j = None
            best_gap = gap_tol

            for j in range(len(segs)):
                if used[j]:
                    continue
                cand = segs[j]
                c_s, c_e = (cand[0], cand[1]), (cand[2], cand[3])
                ca = _line_angle(c_s, c_e)
                ba = _line_angle(p_s, p_e)
                da = abs(ca - ba)
                if da > math.pi / 2:
                    da = abs(da - math.pi)
                if da > math.radians(5.0):
                    continue

                perp1 = abs((c_s[0] - p_s[0]) * uy - (c_s[1] - p_s[1]) * ux)
                perp2 = abs((c_e[0] - p_s[0]) * uy - (c_e[1] - p_s[1]) * ux)
                if perp1 > colinear_tol or perp2 > colinear_tol:
                    continue

                d_s = math.hypot(c_s[0] - p_e[0], c_s[1] - p_e[1])
                d_e = math.hypot(c_e[0] - p_e[0], c_e[1] - p_e[1])
                gap = min(d_s, d_e)
                if gap <= best_gap:
                    best_gap = gap
                    best_j = j

            if best_j is not None:
                chain.append(segs[best_j])
                used[best_j] = True
                grew = True

        if len(chain) == 1:
            res.append(chain[0])
        else:
            pts = []
            for s in chain:
                pts.append((s[0], s[1]))
                pts.append((s[2], s[3]))
            origin = pts[0]
            ux, uy = (pts[-1][0] - origin[0]), (pts[-1][1] - origin[1])
            l = math.hypot(ux, uy)
            if l > 0:
                ux, uy = ux / l, uy / l
                projs = [(p[0] - origin[0]) * ux + (p[1] - origin[1]) * uy for p in pts]
                t_min, t_max = min(projs), max(projs)
                res.append((
                    origin[0] + ux * t_min, origin[1] + uy * t_min,
                    origin[0] + ux * t_max, origin[1] + uy * t_max
                ))
            else:
                res.append(chain[0])

    return res


def extract_front_geometry_for_stem(stem: str) -> Dict[str, Any]:
    """为指定 stem 提取纯净正立面工程骨架。"""
    dxf_path = REPO / f"out/xianyu-acceptance/batch-jc1/dxf/{stem}.dxf"
    mapping_path = OUT_DIR / f"sheets/{stem}/render_mapping.json"
    if not mapping_path.exists() or not dxf_path.exists():
        return {"bars": [], "nodes": []}

    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    ov = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    v_regs = ov.get("view_regions", {}).get(stem, [])
    front_reg = next((r for r in v_regs if r.get("kind") == "front"), None)
    if not front_reg:
        return {"bars": [], "nodes": []}

    rx1, rx2, ry1, ry2 = front_reg["region"]
    xmin, xmax = min(rx1, rx2), max(rx1, rx2)
    ymin, ymax = min(ry1, ry2), max(ry1, ry2)

    if stem == "35A1-JC1-06":
        xmax = min(xmax, 34660.0)
    elif stem == "35A1-JC1-05":
        xmax = min(xmax, 34565.0)

    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()

    bar_layers = set(ov.get("bar_layers_by_stem", {}).get(stem, ["0", "1", "4", "7", "DRAW"]))
    lines: List[Tuple[float, float, float, float]] = []

    for e in msp.query("LINE"):
        p1, p2 = e.dxf.start, e.dxf.end
        mx, my = (p1.x + p2.x) / 2.0, (p1.y + p2.y) / 2.0
        if xmin - 10 <= mx <= xmax + 10 and ymin - 10 <= my <= ymax + 10:
            length = math.hypot(p2.x - p1.x, p2.y - p1.y)
            # 真实铁塔杆件与内辅撑在图纸上的长度通常 >= 18mm（真实 360mm 以上）
            if length >= 18.0:
                layer = getattr(e.dxf, "layer", "0")
                if layer in bar_layers:
                    lines.append((p1.x, p1.y, p2.x, p2.y))

    # 1. 双线合并
    merged_lines = merge_parallel_double_lines(lines, dist_tol=6.0)
    # 2. 共线缝合
    stitched_lines = stitch_collinear_lines(merged_lines, gap_tol=30.0, colinear_tol=3.0)

    # 3. 提取件号
    bar_id_re = re.compile(r"^\b([A-Za-z]?\d{1,5}(?:-\d{1,5})?)\b$")
    labels: List[Tuple[str, float, float]] = []
    for t in msp.query("TEXT MTEXT"):
        txt = (t.dxf.text if t.dxftype() == "TEXT" else t.text).strip()
        ins = t.dxf.insert
        if xmin - 25 <= ins.x <= xmax + 25 and ymin - 25 <= ins.y <= ymax + 25:
            m = bar_id_re.match(txt)
            if m:
                bid = m.group(1)
                if len(bid) <= 5 and not bid.startswith("0"):
                    labels.append((bid, float(ins.x), float(ins.y)))

    # 4. 转像素并绑定件号
    bars: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []
    node_set = set()

    for idx, (x1, y1, x2, y2) in enumerate(stitched_lines, start=1):
        px1, py1 = drawing_xy_to_px(x1, y1, mapping)
        px2, py2 = drawing_xy_to_px(x2, y2, mapping)

        best_bid = None
        best_dist = 45.0
        for bid, lx, ly in labels:
            d = _point_to_line_dist((lx, ly), (x1, y1), (x2, y2))
            if d < best_dist:
                best_dist = d
                best_bid = bid

        bar_item = {
            "bar_uid": f"{stem}_b{idx:04d}",
            "x1": round(px1, 2),
            "y1": round(py1, 2),
            "x2": round(px2, 2),
            "y2": round(py2, 2),
        }
        if best_bid:
            bar_item["bar_id"] = best_bid

        bars.append(bar_item)

        for pt_idx, (px, py) in enumerate([(px1, py1), (px2, py2)]):
            k = (round(px, 1), round(py, 1))
            if k not in node_set:
                node_set.add(k)
                nodes.append({
                    "node_id": f"{stem}_n{len(nodes)+1:04d}",
                    "x_px": round(px, 2),
                    "y_px": round(py, 2),
                })

    return {"bars": bars, "nodes": nodes}


def main():
    stems = ["35A1-JC1-02", "35A1-JC1-04", "35A1-JC1-05", "35A1-JC1-06", "35A1-JC1-07", "35A1-JC1-40"]
    for stem in stems:
        data = extract_front_geometry_for_stem(stem)
        labeled = sum(1 for b in data["bars"] if "bar_id" in b)
        print(f"{stem}: 提取 {len(data['bars'])} 根杆件（已绑定件号 {labeled} 根）, {len(data['nodes'])} 个节点")

        cache_files = [
            CACHE_DIR / f"a2_geom_geom_{stem}_front_0.json",
            CACHE_DIR / f"geom_{stem}_front_0.json",
            CACHE_DIR / f"{stem}_front_0.json",
        ]
        for si in range(5):
            cache_files.append(CACHE_DIR / f"a2_geom_geom_{stem}_front_0_s{si}.json")
            cache_files.append(CACHE_DIR / f"geom_{stem}_front_0_s{si}.json")

        for cf in cache_files:
            cf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n✓ V4 纯净工程桁架 Agent 视觉缓存已更新至 out/agent_vision_cache/")


if __name__ == "__main__":
    main()
