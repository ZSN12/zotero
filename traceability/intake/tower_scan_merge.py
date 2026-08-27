"""多视图扫描融合（P2-4）。

front + side 两张扫描图 → 融合为一张候选模型（同 DXF merge 的线性解耦思路）：
    * front 图提供 (x_px, z_px)：水平轴 x，垂直轴 z
    * side  图提供 (y_px, z_px)：水平轴 y，垂直轴 z
    * 按 z_px（高度）配对 front/side 节点（Hungarian / 贪心最近邻）
    * 融合节点坐标 (x_px, y_px, z_px)，仍为 pixel，solve_status=pending_review
    * 杆件保留 front 主骨架，端点重指向融合后的节点

原则：扫描融合产物仍是人工复核候选，不进终版 3D（除非 confirm + allow_scan）。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel, SourceRef, SourceType


def _nodes(model: EngineeringModel):
    for cid, c in model.components.items():
        if c.kind == "tower_node":
            yield cid, c


def _bars(model: EngineeringModel):
    for cid, c in model.components.items():
        if c.kind == "tower_bar":
            yield cid, c


def _hungarian_pairs(cost: List[List[float]]) -> List[Tuple[int, int]]:
    try:
        from scipy.optimize import linear_sum_assignment
        ri, cj = linear_sum_assignment(cost)
        return list(zip([int(i) for i in ri], [int(j) for j in cj]))
    except Exception:
        n = len(cost)
        used = set()
        pairs = []
        for i in sorted(range(n), key=lambda i: min(cost[i])):
            best_j, best_v = None, float("inf")
            for j in range(n):
                if j in used:
                    continue
                if cost[i][j] < best_v:
                    best_j, best_v = j, cost[i][j]
            if best_j is not None:
                pairs.append((i, best_j))
                used.add(best_j)
        return pairs


def merge_scan_views(
    front_model: EngineeringModel,
    side_model: EngineeringModel,
    z_eps: float = 30.0,
    scale: Optional[float] = None,
    mm_per_px: Optional[float] = None,
) -> EngineeringModel:
    """融合 front + side 扫描模型，返回新的候选模型。"""
    front_nodes = [(cid, c) for cid, c in _nodes(front_model)]
    side_nodes = [(cid, c) for cid, c in _nodes(side_model)]

    def z_of(comp):
        return comp.properties.get("y_px")

    f_buckets: Dict[int, List] = {}
    for item in front_nodes:
        z = z_of(item[1])
        if z is not None:
            f_buckets.setdefault(round(float(z) / z_eps), []).append(item)
    s_buckets: Dict[int, List] = {}
    for item in side_nodes:
        z = z_of(item[1])
        if z is not None:
            s_buckets.setdefault(round(float(z) / z_eps), []).append(item)

    model = EngineeringModel(name=f"scan-merged-{front_model.name or 'front'}")
    # 图纸上下文
    model.add_component(Component(
        id="scan_file",
        name="front+side 扫描融合",
        kind="scan_file",
        source=SourceRef(SourceType.DRAWING, "front+side", confidence=1.0),
        properties={"path": "front+side"},
    ))

    merged: Dict[str, Dict] = {}
    used_front: set = set()
    used_side: set = set()

    for key in sorted(set(f_buckets) | set(s_buckets)):
        F, S = f_buckets.get(key, []), s_buckets.get(key, [])
        if not F or not S:
            # 单视图可见的层：保留该视图信息，另一轴缺 None（不臆造）
            for cid, comp in F:
                if cid not in used_front:
                    merged[f"node_{comp.properties.get('node_id')}"] = {
                        "x_px": comp.properties.get("x_px"),
                        "y_px": None,
                        "z_px": comp.properties.get("y_px"),
                    }
                    used_front.add(cid)
            for cid, comp in S:
                if cid not in used_side:
                    merged[f"node_{comp.properties.get('node_id')}"] = {
                        "x_px": None,
                        "y_px": comp.properties.get("x_px"),
                        "z_px": comp.properties.get("y_px"),
                    }
                    used_side.add(cid)
            continue

        n = min(len(F), len(S))
        cost = [[0.0] * n for _ in range(n)]
        for i in range(n):
            xz_i = F[i][1].properties.get("x_px")
            if xz_i is None:
                cost[i] = [float("inf")] * n
                continue
            for j in range(n):
                xz_j = S[j][1].properties.get("x_px")
                if xz_j is None:
                    cost[i][j] = float("inf")
                else:
                    cost[i][j] = 0.0  # 同一高度层内按出现顺序配对（扫描图无干净 section 判据）
        pairs = _hungarian_pairs(cost)
        for i, j in pairs:
            fc = F[i][1]
            sc = S[j][1]
            z = round((float(fc.properties.get("y_px") or 0) + float(sc.properties.get("y_px") or 0)) / 2, 2)
            nid = f"N{len(merged) + 1:03d}"
            merged[nid] = {
                "x_px": round(float(fc.properties.get("x_px") or 0), 2),
                "y_px": round(float(sc.properties.get("x_px") or 0), 2),
                "z_px": z,
            }
            used_front.add(F[i][0])
            used_side.add(S[j][0])

    # 生成融合节点组件
    node_ids: List[str] = []
    for nid, coord in merged.items():
        cid = f"node_{nid}"
        node_ids.append(cid)
        props = {
            "node_id": nid,
            "x_px": coord["x_px"],
            "y_px": coord["y_px"],
            "z_px": coord["z_px"],
            "unit": "px",
            "solve_status": "pending_review",
        }
        if mm_per_px is not None:
            props.update({
                "x_mm": round((coord["x_px"] or 0) * mm_per_px, 2),
                "y_mm": round((coord["y_px"] or 0) * mm_per_px, 2),
                "z_mm": round((coord["z_px"] or 0) * mm_per_px, 2),
                "unit": "mm",
            })
        model.add_component(Component(
            id=cid, name=f"融合节点 {nid}", kind="tower_node",
            source=SourceRef(SourceType.DERIVED, "front+side", detail="扫描视图融合", confidence=0.5),
            properties=props,
        ))

    # front 骨架杆件重指向
    def nearest_node(x, y):
        best, best_d = None, 20.0
        for cid, comp in _nodes(model):
            px = comp.properties.get("x_px")
            pz = comp.properties.get("z_px")
            if x is None or y is None or px is None or pz is None:
                continue
            d = math.hypot(x - px, y - pz)
            if d < best_d:
                best_d, best = d, cid
        return best

    for cid, bar in _bars(front_model):
        f = bar.properties.get("from_node")
        t = bar.properties.get("to_node")
        nf = front_model.components.get(f)
        nt = front_model.components.get(t)
        x1 = nf.properties.get("x_px") if nf else None
        y1 = nf.properties.get("y_px") if nf else None
        x2 = nt.properties.get("x_px") if nt else None
        y2 = nt.properties.get("y_px") if nt else None
        new_f = nearest_node(x1, y1) if x1 is not None else None
        new_t = nearest_node(x2, y2) if x2 is not None else None
        length = math.hypot((x2 or 0) - (x1 or 0), (y2 or 0) - (y1 or 0))
        model.add_component(Component(
            id=f"bar_scan_merged_{len(model.components):04d}",
            name=bar.name, kind="tower_bar",
            source=bar.source,
            properties={
                "bar_id": bar.properties.get("bar_id", "UNLABELED"),
                "length_px": round(length, 2),
                "unit": "px",
                "view_type": "front",
                "from_node": new_f,
                "to_node": new_t,
                "solve_status": "pending_review",
            },
        ))
    return model
