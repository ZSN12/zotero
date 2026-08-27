"""铁塔跨视图读数合并（Phase 2）。

从立面图/平面图/剖面图中提取同一节点的不同轴坐标，合并为三维坐标：
    * 立面图提供 Z（高度）
    * 平面图提供 X, Y（水平定位）
    * 剖面图补充缺失维

110kV 施工图的三视图带微小展开量（front: x' = x + a*y；side: y' = y + a*x），
本模块用 front+side+section 三视图做线性解耦，恢复干净的 (x, y, z)；
再用平面图锚点校验。仅单视图可见的轴保留 None（placeholder），不臆造。

原则：
    * 合并结果 origin=derived, confidence=0.85
    * 解不唯一/解不出 → 保留 placeholder，不编造
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel
from .tower_spec import view_regions

# 三视图展开系数（与 schema/tower_layer_map.json 生成器约定一致）
DEFAULT_EXPAND = 0.08


def _model_stem(model: EngineeringModel) -> str:
    """从模型推导「图纸 stem」，用于查 view_regions overlay。

    P1-3：批量合并模型的 name 是 tower-batch-merged，与 overlay 里按原始
    文件 stem 写的 view_regions 对不上。因此优先从 drawing_file 的
    drawing_view / path stem 取，回退到 model.name 剥离前缀。
    """
    df = model.components.get("drawing_file")
    if df is not None:
        props = df.properties or {}
        dv = props.get("drawing_view")
        if dv and isinstance(dv, str) and dv.strip():
            return dv.strip()
        path = props.get("path")
        if path and isinstance(path, str):
            stem = Path(path).stem
            if stem:
                return stem
    name = model.name or ""
    for prefix in ("tower-", "compiled-tower-", "compiled-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _region_meta(stem: str) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for r in view_regions(stem):
        kind = r.get("kind")
        if kind:
            out[kind] = r
    return out


def _tower_nodes(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_node":
            yield cid, comp


def _tower_bars(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_bar":
            yield cid, comp


def _linear_solve(xp: float, yp: float, a: float) -> Tuple[float, float]:
    """x' = x + a*y，y' = y + a*x 的解。"""
    denom = 1.0 - a * a
    x = (xp - a * yp) / denom
    y = (yp - a * xp) / denom
    return x, y


def _linear_sum_assignment_fallback(cost: List[List[float]]) -> List[Tuple[int, int]]:
    """无 scipy 时的贪心最小代价匹配（小规模足够）。"""
    n = len(cost)
    if n == 0:
        return []
    used_cols = set()
    pairs = []
    rows = sorted(range(n), key=lambda i: min(cost[i]))
    for i in rows:
        best_j, best_v = None, float("inf")
        for j in range(n):
            if j in used_cols:
                continue
            if cost[i][j] < best_v:
                best_j, best_v = j, cost[i][j]
        if best_j is not None:
            pairs.append((i, best_j))
            used_cols.add(best_j)
    return pairs


def _hungarian(cost: List[List[float]]) -> List[Tuple[int, int]]:
    try:
        from scipy.optimize import linear_sum_assignment
        ri, cj = linear_sum_assignment(cost)
        return list(zip([int(i) for i in ri], [int(j) for j in cj]))
    except Exception:
        return _linear_sum_assignment_fallback(cost)


def merge_view_coordinates(model: EngineeringModel) -> Dict[str, Dict[str, Optional[float]]]:
    """合并跨视图坐标，返回 {node_component_id: {"x","y","z"}}。

    - front(elevation) 提供 Z，并联合 side 恢复 (x, y)（带展开量时解 2x2 线性系统）
    - section 提供干净的 x，用于给 front×side 配对做判据
    - plan 直接提供该层的 x, y, z
    """
    stem = _model_stem(model)
    meta = _region_meta(stem)

    eps = 50.0
    nodes_by_view: Dict[str, List[Tuple[str, Component]]] = defaultdict(list)
    for cid, comp in _tower_nodes(model):
        vk = comp.properties.get("view_type")
        if vk:
            nodes_by_view[vk].append((cid, comp))

    # front/side/section 的分桶键：view_y（即 Z）
    def bucket(z: Optional[float]) -> Optional[int]:
        return None if z is None else round(float(z) / eps)

    merged: Dict[str, Dict[str, Optional[float]]] = {}

    # ---- plan 视图：x/y/z 三轴直接可得 ----
    for cid, comp in nodes_by_view.get("plan", []):
        p = comp.properties
        z = p.get("z_level")
        x, y = p.get("view_x"), p.get("view_y")
        merged[cid] = {"x": x, "y": y, "z": z}
        if x is not None and y is not None and z is not None:
            p["x"], p["y"], p["z"] = round(x, 2), round(y, 2), round(float(z), 2)
            p["solve_status"] = "solved"

    # ---- elevation（demo）：view_x=x, view_y=z，与 plan 的 x 对齐 ----
    for cid, comp in nodes_by_view.get("elevation", []):
        p = comp.properties
        z = p.get("view_y")
        x = p.get("view_x")
        merged[cid] = {"x": x, "y": p.get("y"), "z": z}
        # 找 plan 中 x 相同（且 y<=0 侧优先）的节点补 y
        plan_nodes = nodes_by_view.get("plan", [])
        y_plan = None
        best_d = float("inf")
        for _pcid, pc in plan_nodes:
            px, py = pc.properties.get("view_x"), pc.properties.get("view_y")
            if px is None or x is None:
                continue
            d = abs(px - x)
            if d < best_d:
                best_d = d
                y_plan = py
        if x is not None and z is not None and y_plan is not None:
            p["x"], p["y"], p["z"] = round(x, 2), round(float(y_plan), 2), round(float(z), 2)
            p["solve_status"] = "solved"
            merged[cid] = {"x": x, "y": y_plan, "z": z}

    # ---- front + side + section 三视图线性解耦 ----
    front_meta = meta.get("front", {})
    side_meta = meta.get("side", {})
    a_f = float(front_meta.get("y_expand", DEFAULT_EXPAND)) if front_meta else 0.0
    a_s = float(side_meta.get("x_expand", DEFAULT_EXPAND)) if side_meta else 0.0
    a = a_f or a_s

    fb = defaultdict(list)
    for cid, comp in nodes_by_view.get("front", []):
        fb[bucket(comp.properties.get("view_y"))].append((cid, comp))
    sb = defaultdict(list)
    for cid, comp in nodes_by_view.get("side", []):
        sb[bucket(comp.properties.get("view_y"))].append((cid, comp))
    tb = defaultdict(list)
    for cid, comp in nodes_by_view.get("section", []):
        tb[bucket(comp.properties.get("view_y"))].append((cid, comp))

    for k in sorted(set(fb) | set(sb) | set(tb)):
        F, S, T = fb.get(k, []), sb.get(k, []), tb.get(k, [])
        if not F or not S or not T:
            continue
        if len(F) != len(S):
            continue
        n = len(F)
        # 每个 front×side 配对解出 (x, y)；判据是 x 必须落在 section 的干净 x 集合里
        cost = [[0.0] * n for _ in range(n)]
        for i in range(n):
            xp = F[i][1].properties.get("view_x")
            if xp is None:
                cost[i] = [float("inf")] * n
                continue
            for j in range(n):
                yp = S[j][1].properties.get("view_x")
                if yp is None:
                    cost[i][j] = float("inf")
                    continue
                xs, _ys = _linear_solve(float(xp), float(yp), a)
                cost[i][j] = min((xs - tc[1].properties.get("view_x")) ** 2
                                 for tc in T if tc[1].properties.get("view_x") is not None)
        pairs = _hungarian(cost)
        for i, j in pairs:
            xp = F[i][1].properties.get("view_x")
            yp = S[j][1].properties.get("view_x")
            if xp is None or yp is None:
                continue
            xs, ys = _linear_solve(float(xp), float(yp), a)
            err = min(abs(xs - tc[1].properties.get("view_x"))
                      for tc in T if tc[1].properties.get("view_x") is not None)
            if err > eps * 2:
                continue
            z = F[i][1].properties.get("view_y")
            if z is None:
                continue
            solved = {"x": round(xs, 2), "y": round(ys, 2), "z": round(float(z), 2)}
            merged[F[i][0]] = dict(solved)
            merged[S[j][0]] = dict(solved)
            for cid in (F[i][0], S[j][0]):
                comp = model.components[cid]
                comp.properties.update({"x": solved["x"], "y": solved["y"], "z": solved["z"],
                                        "solve_status": "solved"})

    return merged


def _bar_3d_length(bar: Component, model: EngineeringModel) -> Optional[float]:
    f = bar.properties.get("from_node")
    t = bar.properties.get("to_node")
    nf = model.components.get(f)
    nt = model.components.get(t)
    if not nf or not nt:
        return None
    pf, pt = nf.properties, nt.properties
    if None in (pf.get("x"), pf.get("y"), pf.get("z"), pt.get("x"), pt.get("y"), pt.get("z")):
        return None
    return math.sqrt((pf["x"] - pt["x"]) ** 2 + (pf["y"] - pt["y"]) ** 2 + (pf["z"] - pt["z"]) ** 2)


def merge_view_bars(model: EngineeringModel) -> EngineeringModel:
    """把跨视图投影合并为物理杆件。

    主视图（front/elevation）每个物理杆件只画一次，因此以主视图为骨架：
        * 仅保留主视图的 tower_node / tower_bar，删除其它视图投影
        * 用已解算的节点三轴坐标计算 length_mm_3d
        * 用 BOM 长度把 UNLABELED 主视图杆件匹配回 bar_id（唯一匹配才接受）
        * BOM 维度的 applies_to 重指到合并后的杆件
    """
    bars = [c for _, c in _tower_bars(model)]
    if not bars:
        return model
    counts: Dict[str, int] = defaultdict(int)
    for c in bars:
        counts[c.properties.get("view_type") or "_all"] += 1
    primary = "front" if counts.get("front") else "elevation" if counts.get("elevation") else (
        max(counts, key=lambda k: counts[k])
    )

    primary_nodes = {cid for cid, c in _tower_nodes(model)
                     if c.properties.get("view_type") == primary}
    primary_bars = [c for c in bars if c.properties.get("view_type") == primary]

    # BOM 长度表（dim_bom_length_*）与截面表
    bom_len: Dict[str, float] = {}
    bom_sec: Dict[str, str] = {}
    used_ids: set = set()
    for did, d in model.dimensions.items():
        if did.startswith("dim_bom_length_"):
            bid = did[len("dim_bom_length_"):]
            if d.value is not None:
                bom_len[bid] = float(d.value)
        elif did.startswith("dim_bom_section_"):
            bid = did[len("dim_bom_section_"):]
            bom_sec[bid] = str(d.value) if d.value is not None else ""

    # 先给已编号杆件算 3D 长度/截面
    unlabeled: List[Component] = []
    for bar in primary_bars:
        bid = bar.properties.get("bar_id", "")
        if bid.startswith("UNLABELED"):
            unlabeled.append(bar)
            continue
        used_ids.add(bid)
        ln = _bar_3d_length(bar, model)
        if ln is not None:
            bar.properties["length_mm_3d"] = round(ln, 2)
        if bid in bom_sec:
            bar.properties["section"] = bom_sec[bid]

    # UNLABELED 用 BOM 长度唯一匹配
    for bar in unlabeled:
        ln = _bar_3d_length(bar, model)
        if ln is not None:
            bar.properties["length_mm_3d"] = round(ln, 2)
        candidates = []
        if ln is not None:
            for bid, bl in bom_len.items():
                if bid in used_ids or bl <= 0:
                    continue
                if abs(ln - bl) / bl <= 0.01:
                    candidates.append(bid)
        if len(candidates) == 1:
            bid = candidates[0]
            bar.properties["bar_id"] = bid
            bar.properties["section"] = bom_sec.get(bid)
            used_ids.add(bid)

    # 重建组件：只保留主视图节点/杆件 + 图纸上下文 + BOM 行
    keep_components = {}
    for cid, c in model.components.items():
        if c.kind == "drawing_file" or c.kind == "bom_row":
            keep_components[cid] = c
    for cid in sorted(primary_nodes):
        keep_components[cid] = model.components[cid]
    for bar in primary_bars:
        keep_components[bar.id] = bar

    # 修正 BOM 维度的 applies_to 与依赖
    merged_by_bid: Dict[str, List[str]] = defaultdict(list)
    for bar in primary_bars:
        bid = bar.properties.get("bar_id", "")
        if not bid.startswith("UNLABELED"):
            merged_by_bid[bid].append(bar.id)

    valid_nodes = set(keep_components) | set(model.dimensions) | set(model.connections) | set(model.rules)
    new_deps: Dict[str, set] = defaultdict(set)
    for did, d in model.dimensions.items():
        if did.startswith("dim_bom_length_") or did.startswith("dim_bom_section_"):
            bid = did.rsplit("_", 1)[-1]
            bars_for_bid = merged_by_bid.get(bid, [])
            d.applies_to = bars_for_bid[0] if bars_for_bid else f"bom_{bid}"
            if d.applies_to not in valid_nodes:
                d.applies_to = f"bom_{bid}"
    for bar in primary_bars:
        bid = bar.properties.get("bar_id", "")
        if bid and not bid.startswith("UNLABELED"):
            up = [f"dim_bom_length_{bid}", f"dim_bom_section_{bid}"]
            new_deps[bar.id].update(u for u in up if u in model.dimensions)

    model.components = keep_components
    model.dependencies = {k: set(v) for k, v in new_deps.items()}
    # 清理失效表
    model.staleness = {cid: st for cid, st in model.staleness.items() if cid in model.all_nodes()}

    # 规则 applies_to 重指到合并后的构件
    bar_ids = [c.id for c in primary_bars]
    node_ids = list(primary_nodes)
    for r in model.rules.values():
        if r.id in ("r_topology_closed", "r_bom_length_match", "r_bom_section_match",
                    "r_no_duplicate_bar_id"):
            r.applies_to = bar_ids
        elif r.id == "r_node_fully_solved":
            r.applies_to = node_ids

    return model
