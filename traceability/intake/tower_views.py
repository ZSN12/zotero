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
from .tower_spec import (
    view_regions,
    cross_file_z_ref,
    cross_file_allow_z_peer_interpolate,
    cross_file_synthetic_side_from_front,
    cross_file_synthetic_side_view_x_scale,
    cross_file_plan_sheets,
    cross_file_z_band_scale,
    cross_file_normalize_x,
)

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


def _region_meta(stem: str, overlay: Optional[str | Path | dict] = None) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for r in view_regions(stem, overlay=overlay):
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


def _norm_view_x(val: float, items: List[Component]) -> float:
    xs = [float(c.properties["view_x"]) for c in items if c.properties.get("view_x") is not None]
    if not xs:
        return 0.5
    lo, hi = min(xs), max(xs)
    return (val - lo) / (hi - lo) if hi - lo > 1e-6 else 0.5


def _propagate_front_y_via_bar_id(
    model: EngineeringModel,
    nodes_by_view: Dict[str, List[Tuple[str, Component]]],
    merged: Dict[str, Dict[str, Optional[float]]],
    eps: float,
) -> None:
    """同 bar_id 的 plan 杆件端点 y → front 杆件端点（cross_file 提升解算率）。"""
    from collections import defaultdict

    node_index = {cid: comp for cid, comp in _tower_nodes(model)}

    plan_bars: Dict[str, List[Component]] = defaultdict(list)
    front_bars: Dict[str, List[Component]] = defaultdict(list)
    for _, bar in _tower_bars(model):
        bid = str(bar.properties.get("bar_id") or "")
        if not bid or bid.startswith("UNLABELED"):
            continue
        vt = bar.properties.get("view_type")
        if vt == "plan":
            plan_bars[bid].append(bar)
        elif vt == "front":
            front_bars[bid].append(bar)

    def _match_endpoints(plan_bar: Component, front_bar: Component) -> List[Tuple[Component, Component]]:
        pairs: List[Tuple[Component, Component]] = []
        plan_ends: List[Component] = []
        front_ends: List[Component] = []
        for end in ("from_node", "to_node"):
            pnid = plan_bar.properties.get(end)
            fnid = front_bar.properties.get(end)
            if pnid and pnid in node_index:
                plan_ends.append(node_index[pnid])
            if fnid and fnid in node_index:
                front_ends.append(node_index[fnid])
        if not plan_ends or not front_ends:
            return pairs
        used: set = set()
        for pe in plan_ends:
            px = pe.properties.get("view_x")
            if px is None:
                continue
            best_f, best_d = None, float("inf")
            for fe in front_ends:
                if fe.id in used:
                    continue
                fx = fe.properties.get("view_x")
                if fx is None:
                    continue
                d = abs(
                    _norm_view_x(float(px), plan_ends) - _norm_view_x(float(fx), front_ends)
                )
                if d < best_d:
                    best_d, best_f = d, fe
            if best_f is not None and best_d <= 0.4:
                pairs.append((best_f, pe))
                used.add(best_f.id)
        return pairs

    for bid in set(plan_bars) & set(front_bars):
        for pbar in plan_bars[bid]:
            for fbar in front_bars[bid]:
                for fnode, pnode in _match_endpoints(pbar, fbar):
                    if fnode.properties.get("solve_status") == "solved":
                        continue
                    fx = fnode.properties.get("view_x")
                    fz = fnode.properties.get("view_y")
                    py = pnode.properties.get("view_y")
                    if fx is None or fz is None or py is None:
                        continue
                    solved = {
                        "x": round(float(fx), 2),
                        "y": round(float(py), 2),
                        "z": round(float(fz), 2),
                    }
                    fnode.properties.update({**solved, "solve_status": "solved"})
                    merged[fnode.id] = dict(solved)


def _interpolate_front_y_from_z_peers(
    model: EngineeringModel,
    merged: Dict[str, Dict[str, Optional[float]]],
    eps: float,
) -> None:
    """同 Z 带内已解算节点的 y，对剩余 front 节点按 view_x 线性插值（cross_file 补全）。"""
    front_nodes = [
        comp for _, comp in _tower_nodes(model)
        if comp.properties.get("view_type") == "front"
    ]
    solved = [
        c for c in front_nodes
        if c.properties.get("solve_status") == "solved" and c.properties.get("y") is not None
    ]
    for comp in front_nodes:
        if comp.properties.get("solve_status") == "solved":
            continue
        ux = comp.properties.get("view_x")
        uz = comp.properties.get("view_y")
        if ux is None or uz is None:
            continue
        peers = [
            s for s in solved
            if abs(float(s.properties.get("view_y") or 0) - float(uz)) <= eps
        ]
        if len(peers) < 2:
            continue
        peers.sort(key=lambda s: float(s.properties.get("view_x") or 0))
        left = [s for s in peers if float(s.properties.get("view_x") or 0) <= float(ux)]
        right = [s for s in peers if float(s.properties.get("view_x") or 0) >= float(ux)]
        if left and right:
            s1 = max(left, key=lambda s: float(s.properties.get("view_x") or 0))
            s2 = min(right, key=lambda s: float(s.properties.get("view_x") or 0))
            if s1.id == s2.id:
                s1, s2 = sorted(peers, key=lambda s: abs(float(s.properties.get("view_x") or 0) - float(ux)))[:2]
        else:
            s1, s2 = sorted(peers, key=lambda s: abs(float(s.properties.get("view_x") or 0) - float(ux)))[:2]
        if len(peers) > 1:
            x1, y1 = float(s1.properties["view_x"]), float(s1.properties["y"])
            x2, y2 = float(s2.properties["view_x"]), float(s2.properties["y"])
            if abs(x2 - x1) < 1e-6:
                y = y1
            else:
                t = (float(ux) - x1) / (x2 - x1)
                y = y1 + t * (y2 - y1)
        solved_dict = {
            "x": round(float(ux), 2),
            "y": round(float(y), 2),
            "z": round(float(uz), 2),
        }
        comp.properties.update({
            **solved_dict,
            "solve_status": "solved",
            "y_origin": "z_peer_interpolate",
            "y_review": "pending",
        })
        ao = dict(comp.properties.get("axis_origin") or {})
        ao["y"] = "derived"
        comp.properties["axis_origin"] = ao
        merged[comp.id] = dict(solved_dict)


def _synthesize_side_nodes_from_front(
    model: EngineeringModel,
    overlay: Optional[str | Path | dict] = None,
) -> int:
    """M5：无 side 分册时，从 front 生成 synthetic side 节点（供 front+side 解 y）。

    side.view_x = front.view_x * synthetic_side_view_x_scale。默认 1.0 保持 M5
    原「1:1 假侧视」行为；国网单立面设 0.0 可避免 y≈x 的 45° 斜片。
    """
    if not cross_file_synthetic_side_from_front(overlay=overlay):
        return 0
    if any(
        c.properties.get("view_type") == "side"
        for c in model.components.values()
        if c.kind == "tower_node"
    ):
        return 0

    from ..model import Component

    side_x_scale = cross_file_synthetic_side_view_x_scale(overlay=overlay)
    count = 0
    for cid, comp in list(model.components.items()):
        if comp.kind != "tower_node" or comp.properties.get("view_type") != "front":
            continue
        p = comp.properties
        if p.get("view_x") is None or p.get("view_y") is None:
            continue
        nid = p.get("node_id") or cid.split("__")[-1]
        side_id = f"{cid.rsplit('__', 1)[0]}__side_syn_{nid}" if "__" in cid else f"side_syn_{nid}"
        if side_id in model.components:
            continue
        model.add_component(Component(
            id=side_id,
            name=f"[syn-side] {comp.name}",
            kind="tower_node",
            source=comp.source,
            properties={
                "node_id": nid,
                "view_type": "side",
                "view_x": round(float(p.get("view_x")) * side_x_scale, 4),
                "view_y": p.get("view_y"),
                "source_file": p.get("source_file"),
                "drawing_view": p.get("drawing_view"),
                "synthetic_pair": cid,
                "y_origin": "synthetic_side_from_front",
                "solve_status": "partial",
                "axis_origin": dict(p.get("axis_origin") or {}),
            },
        ))
        count += 1
    df = model.components.get("drawing_file")
    if df is not None and count:
        df.properties["synthetic_side_nodes"] = count
        # synthetic side 也构成一个可参与 front+side 3D 解耦的视图；
        # 否则 tower_geometry_gate 的 require_front_and_side 会误判「缺 side」。
        vk = set(df.properties.get("view_kinds") or [])
        if "side" not in vk:
            vk.add("side")
            df.properties["view_kinds"] = sorted(vk)
    return count


def _recover_y_via_synthetic_side(
    model: EngineeringModel,
    merged: Dict[str, Dict[str, Optional[float]]],
    overlay: Optional[str | Path | dict] = None,
) -> int:
    """M5：front+synthetic side 同 view_x 时，用展开线性解耦恢复 y。"""
    if not cross_file_synthetic_side_from_front(overlay=overlay):
        return 0
    front_meta = _region_meta(_model_stem(model), overlay=overlay).get("front", {})
    # 无显式 y_expand 时默认 0（外部图 region 直接写 x/z 轴，不应强加展开量）。
    a = float(front_meta.get("y_expand", 0.0)) if front_meta else 0.0
    side_by_pair: Dict[str, Component] = {}
    for cid, comp in model.components.items():
        if comp.kind != "tower_node" or comp.properties.get("view_type") != "side":
            continue
        pair = comp.properties.get("synthetic_pair")
        if pair:
            side_by_pair[str(pair)] = comp

    recovered = 0
    for fcid, fcomp in list(model.components.items()):
        if fcomp.kind != "tower_node" or fcomp.properties.get("view_type") != "front":
            continue
        if fcomp.properties.get("solve_status") == "solved" and fcomp.properties.get("y") is not None:
            continue
        scomp = side_by_pair.get(fcid)
        if scomp is None:
            continue
        fp, sp = fcomp.properties, scomp.properties
        xp, yp = fp.get("view_x"), sp.get("view_x")
        z = fp.get("view_y")
        if xp is None or yp is None or z is None:
            continue
        try:
            x, y = _linear_solve(float(xp), float(yp), a)
        except (ZeroDivisionError, ValueError):
            continue
        solved = {
            "x": round(float(x), 2),
            "y": round(float(y), 2),
            "z": round(float(z), 2),
        }
        fp.update({
            **solved,
            "solve_status": "solved",
            "y_origin": "synthetic_side_from_front",
            "y_review": "verified",
        })
        ao = dict(fp.get("axis_origin") or {})
        ao["y"] = "derived"
        fp["axis_origin"] = ao
        merged[fcid] = dict(solved)
        recovered += 1
    return recovered


def _pair_front_plan_at_z(
    model: EngineeringModel,
    nodes_by_view: Dict[str, List[Tuple[str, Component]]],
    merged: Dict[str, Dict[str, Optional[float]]],
    *,
    z_ref: Optional[float],
    z_band: float,
    cost_threshold: float = 0.35,
) -> int:
    """front+plan 匈牙利配对（单 z 带），返回新解算节点数。"""
    plan_nodes_list = nodes_by_view.get("plan", [])
    front_at_z: List[Tuple[str, Component]] = []
    for cid, comp in nodes_by_view.get("front", []):
        if comp.properties.get("solve_status") == "solved":
            continue
        p = comp.properties
        z = p.get("view_y")
        if z is None:
            continue
        if z_ref is not None and abs(float(z) - float(z_ref)) > z_band:
            continue
        if p.get("view_x") is not None:
            front_at_z.append((cid, comp))

    if not front_at_z or not plan_nodes_list:
        return 0

    def _norm_x(val: float, items: List[Tuple[str, Component]]) -> float:
        xs = [c.properties["view_x"] for _, c in items if c.properties.get("view_x") is not None]
        if not xs:
            return 0.5
        lo, hi = min(xs), max(xs)
        return (val - lo) / (hi - lo) if hi - lo > 1e-6 else 0.5

    n_f, n_p = len(front_at_z), len(plan_nodes_list)
    n = max(n_f, n_p)
    cost = [[1.0] * n for _ in range(n)]
    for i, (_, fc) in enumerate(front_at_z):
        fx = fc.properties["view_x"]
        for j, (_, pc) in enumerate(plan_nodes_list):
            px = pc.properties.get("view_x")
            if px is None:
                cost[i][j] = float("inf")
                continue
            cost[i][j] = abs(_norm_x(float(fx), front_at_z) - _norm_x(float(px), plan_nodes_list))
    pairs = _hungarian(cost)
    paired = 0
    for i, j in pairs:
        if i >= n_f or j >= n_p:
            continue
        if cost[i][j] > cost_threshold:
            continue
        fcid, fcomp = front_at_z[i]
        _, pcomp = plan_nodes_list[j]
        fp, pp = fcomp.properties, pcomp.properties
        x = fp.get("view_x")
        z = fp.get("view_y")
        y = pp.get("view_y")
        if x is None or z is None or y is None:
            continue
        solved = {"x": round(float(x), 2), "y": round(float(y), 2), "z": round(float(z), 2)}
        merged[fcid] = dict(solved)
        fp.update({**solved, "solve_status": "solved"})
        paired += 1
    return paired


def _pair_front_side_at_z(
    model: EngineeringModel,
    nodes_by_view: Dict[str, List[Tuple[str, Component]]],
    merged: Dict[str, Dict[str, Optional[float]]],
    *,
    eps: float = 50.0,
    cost_threshold: float = 0.35,
    expand: float = 0.0,
) -> int:
    """P0-4：同图 front+side 按标高 Z 配对，解算唯一 (x, y, z)。

    front.view_x = X，side.view_x = Y，两者的 view_y = Z（同一 Z 带内）。
    无 section 视图做判据时，用归一化 view_x 排序做匈牙利一对一配对：
        x = front.view_x，y = side.view_x，z = 平均标高。
    expand != 0 时（图面带展开量），用 2x2 线性解耦恢复干净 (x, y)。

    只使用真实 side 节点（synthetic_pair 标记的假侧视不参与本路径），
    已解算的 front 节点跳过。
    """
    real_side = [
        (cid, comp) for cid, comp in nodes_by_view.get("side", [])
        if not comp.properties.get("synthetic_pair")
    ]
    if not real_side:
        return 0

    fb = defaultdict(list)
    for cid, comp in nodes_by_view.get("front", []):
        if comp.properties.get("solve_status") == "solved":
            continue
        z = comp.properties.get("view_y")
        if z is None:
            continue
        fb[round(float(z) / eps)].append((cid, comp))
    sb = defaultdict(list)
    for cid, comp in real_side:
        z = comp.properties.get("view_y")
        if z is None:
            continue
        sb[round(float(z) / eps)].append((cid, comp))

    def _norm_x(val: float, items: List[Tuple[str, Component]]) -> float:
        xs = [c.properties["view_x"] for _, c in items if c.properties.get("view_x") is not None]
        if not xs:
            return 0.5
        lo, hi = min(xs), max(xs)
        return (val - lo) / (hi - lo) if hi - lo > 1e-6 else 0.5

    paired = 0
    for k in sorted(set(fb) & set(sb)):
        F, S = fb[k], sb[k]
        n_f, n_s = len(F), len(S)
        n = max(n_f, n_s)
        cost = [[1.0] * n for _ in range(n)]
        for i, (_, fc) in enumerate(F):
            fx = fc.properties.get("view_x")
            if fx is None:
                continue
            for j, (_, sc) in enumerate(S):
                sy = sc.properties.get("view_x")
                if sy is None:
                    cost[i][j] = float("inf")
                    continue
                cost[i][j] = abs(_norm_x(float(fx), F) - _norm_x(float(sy), S))
        pairs = _hungarian(cost)
        for i, j in pairs:
            if i >= n_f or j >= n_s:
                continue
            if cost[i][j] > cost_threshold:
                continue
            fcid, fcomp = F[i]
            scid, scomp = S[j]
            xp = fcomp.properties.get("view_x")
            yp = scomp.properties.get("view_x")
            zf = fcomp.properties.get("view_y")
            zs = scomp.properties.get("view_y")
            if xp is None or yp is None or zf is None:
                continue
            if expand:
                try:
                    x, y = _linear_solve(float(xp), float(yp), expand)
                except (ZeroDivisionError, ValueError):
                    continue
            else:
                x, y = float(xp), float(yp)
            z = float(zf)
            if zs is not None:
                z = (float(zf) + float(zs)) / 2.0
            solved = {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}
            for cid, comp in ((fcid, fcomp), (scid, scomp)):
                comp.properties.update({
                    **solved,
                    "solve_status": "solved",
                    "solve_method": "front_side_z_pair",
                })
                merged[cid] = dict(solved)
            paired += 1
    return paired


def _fill_unpaired_front_y(
    model: EngineeringModel,
    nodes_by_view: Dict[str, List[Tuple[str, Component]]],
    merged: Dict[str, Dict[str, Optional[float]]],
    eps: float = 50.0,
) -> int:
    """P0-3：front(X,Z)+side(Y,Z) 配对后，仍有 front 节点只缺 Y 时，
    用同 Z 带内已解算 front 节点的 Y 做线性插值补齐（X 最近邻）。

    这样 strict export 不再因 front 节点缺 Y 而卡死；Y 来源标记为 side_peer_fill。
    """
    front_nodes = nodes_by_view.get("front", [])
    solved = [
        c for _, c in front_nodes
        if c.properties.get("solve_status") == "solved"
        and c.properties.get("y") is not None
    ]
    filled = 0
    for cid, comp in front_nodes:
        if comp.properties.get("solve_status") == "solved":
            continue
        ux = comp.properties.get("view_x")
        uz = comp.properties.get("view_y")
        if ux is None or uz is None:
            continue
        peers = [
            c for c in solved
            if abs(float(c.properties.get("view_y") or 0) - float(uz)) <= eps
        ]
        if not peers:
            # 同 Z 带无已解算节点时，退到全高最近 Z 的两个节点插值
            peers = sorted(
                solved,
                key=lambda c: abs(float(c.properties.get("view_y") or 0) - float(uz)),
            )[:2]
        if not peers:
            continue
        if len(peers) == 1:
            y = float(peers[0].properties["y"])
        else:
            peers.sort(key=lambda c: float(c.properties.get("view_x") or 0))
            left = [c for c in peers if float(c.properties.get("view_x") or 0) <= float(ux)]
            right = [c for c in peers if float(c.properties.get("view_x") or 0) >= float(ux)]
            if left and right:
                s1 = max(left, key=lambda c: float(c.properties["view_x"]))
                s2 = min(right, key=lambda c: float(c.properties["view_x"]))
            else:
                s1, s2 = sorted(
                    peers, key=lambda c: abs(float(c.properties.get("view_x") or 0) - float(ux)),
                )[:2]
        if len(peers) > 1:
            x1, y1 = float(s1.properties["view_x"]), float(s1.properties["y"])
            x2, y2 = float(s2.properties["view_x"]), float(s2.properties["y"])
            if abs(x2 - x1) < 1e-6:
                y = y1
            else:
                t = (float(ux) - x1) / (x2 - x1)
                y = y1 + t * (y2 - y1)
        solved_dict = {
            "x": round(float(ux), 2),
            "y": round(float(y), 2),
            "z": round(float(uz), 2),
        }
        comp.properties.update({
            **solved_dict,
            "solve_status": "solved",
            "solve_method": "side_peer_fill",
            "y_origin": "side_peer_fill",
        })
        ao = dict(comp.properties.get("axis_origin") or {})
        ao["y"] = "derived"
        comp.properties["axis_origin"] = ao
        merged[cid] = dict(solved_dict)
        filled += 1
    return filled


def merge_view_coordinates(
    model: EngineeringModel,
    overlay: Optional[str | Path | dict] = None,
) -> Dict[str, Dict[str, Optional[float]]]:
    """合并跨视图坐标，返回 {node_component_id: {"x","y","z"}}。

    - front(elevation) 提供 Z，并联合 side 恢复 (x, y)（带展开量时解 2x2 线性系统）
    - section 提供干净的 x，用于给 front×side 配对做判据
    - plan 直接提供该层的 x, y, z

    overlay：per-project 图层/视图规范（P0-1）。国网等外部图的 view_regions
    写在 overlay 里而非 schema/tower_layer_map.json，因此必须下传，否则
    _region_meta 读不到 front/side 的 y_expand/x_expand 等展开元数据。
    """
    stem = _model_stem(model)
    meta = _region_meta(stem, overlay=overlay)
    from .tower_spec import canonical_view_type

    eps = 50.0
    nodes_by_view: Dict[str, List[Tuple[str, Component]]] = defaultdict(list)
    for cid, comp in _tower_nodes(model):
        vk = comp.properties.get("view_type")
        if vk:
            # P2 统一视图类型：front/elevation 归一化为 front，避免 elevation
            # 来源节点被分到 nodes_by_view["elevation"] 而查不到 "front"。
            nodes_by_view[canonical_view_type(vk)].append((cid, comp))

    # front/side/section 的分桶键：view_y（即 Z）
    def bucket(z: Optional[float]) -> Optional[int]:
        return None if z is None else round(float(z) / eps)

    merged: Dict[str, Dict[str, Optional[float]]] = {}
    df = model.components.get("drawing_file")

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

    # ---- front 单立面（无 side 视图）：view_x=x, view_y=z ----
    # 国网 35A1-JC1-02 只有正立面、无独立侧立面。此类图走四向镜像展开
    # （expand_4_face_symmetry_model 会从节点 x/z 重建 (x, y=0, z)），
    # 因此这里只需把 view_x→x、view_y→z 落进节点属性，y 先置 0（镜像展开时
    # 会重新生成 4 面 y）。若存在 plan 视图，则优先用 plan 的 y 补半宽。
    side_kinds_present = bool(nodes_by_view.get("side") or nodes_by_view.get("section"))
    if not side_kinds_present:
        from .tower_spec import view_z_offset, view_z_span_mm
        front_nodes = nodes_by_view.get("front", [])
        # 分段立面图沿 Z 堆叠：每张图局部 view_y 的 0 点未必在塔段底部
        # （region origin 通常在图纸左上角，塔段下方还有标注空间），因此先按
        # drawing_view 求该段局部 view_y 最小值/最大值（=该段塔身几何上下界），
        # 再线性归一化到标注段高 z_span_mm（消除相邻段接头重叠），最后加 z_offset。
        seg_span = {}
        for _cid, comp in front_nodes:
            dv = comp.properties.get("drawing_view") or stem
            vy = comp.properties.get("view_y")
            if vy is None:
                continue
            lo, hi = seg_span.get(dv, (float("inf"), float("-inf")))
            seg_span[dv] = (min(lo, float(vy)), max(hi, float(vy)))

        for cid, comp in front_nodes:
            p = comp.properties
            ux, uz = p.get("view_x"), p.get("view_y")
            if ux is None or uz is None:
                continue
            node_stem = p.get("drawing_view") or stem
            z_off = view_z_offset(str(node_stem), "front", overlay=overlay)
            span_mm = view_z_span_mm(str(node_stem), "front", overlay=overlay)
            # 阶段3.1：分段立面图 Z 映射。
            # 若 view_y 本身已在 region 局部坐标中（origin 在段底部，即 min_y 对应 Z=0），
            # 直接 uz_global = z_off + float(uz)。
            # 只有当 view_y 跨度严重漂移时，才做归一化。
            # 直接使用真实的图纸几何毫米偏移，避免极值碎片拉扯整段高程。
            local = float(uz)
            uz_global = z_off + local
            # 尝试从 plan 视图按 x 就近取 y（四向镜像前只是参考，展开会重算）
            y = 0.0
            plan_nodes = nodes_by_view.get("plan", [])
            best_d = float("inf")
            for _pcid, pc in plan_nodes:
                px = pc.properties.get("view_x")
                py = pc.properties.get("view_y")
                if px is None or py is None:
                    continue
                d = abs(float(px) - float(ux))
                if d < best_d:
                    best_d = d
                    y = float(py)
            p["x"], p["y"], p["z"] = round(float(ux), 2), round(float(y), 2), round(uz_global, 2)
            p["solve_status"] = "solved"
            p.setdefault("solve_method", "single_front")
            merged[cid] = {"x": float(ux), "y": float(y), "z": uz_global}

    # ---- front + side 同 Z 带配对（P0-4：同图双视图真 3D 解）----
    # 有 section 视图时，下面的三视图线性解耦更严格，不要提前抢占。
    front_meta = meta.get("front", {})
    side_meta = meta.get("side", {})
    a_f = float(front_meta.get("y_expand", 0.0)) if front_meta else 0.0
    a_s = float(side_meta.get("x_expand", 0.0)) if side_meta else 0.0
    a = a_f or a_s
    _synthesize_side_nodes_from_front(model, overlay=overlay)
    if not nodes_by_view.get("section"):
        paired_fs = _pair_front_side_at_z(model, nodes_by_view, merged, eps=eps, expand=a)
        if df is not None and paired_fs:
            df.properties["front_side_pairings"] = paired_fs
        filled_y = _fill_unpaired_front_y(model, nodes_by_view, merged, eps=eps)
        if df is not None and filled_y:
            df.properties["side_peer_filled_y"] = filled_y

    # ---- front + plan 跨文件配对（M3/M5：多 z 平面 plan_sheets）----
    z_band = eps * cross_file_z_band_scale(overlay=overlay)
    plan_specs = cross_file_plan_sheets(overlay=overlay)
    plan_nodes_all = nodes_by_view.get("plan", [])
    paired_total = 0
    if plan_specs and plan_nodes_all:
        for spec in plan_specs:
            z_ref = spec.get("z_level")
            if z_ref is None:
                z_ref = cross_file_z_ref(overlay=overlay)
            plan_at_z = [
                (cid, comp) for cid, comp in plan_nodes_all
                if z_ref is None
                or comp.properties.get("z_level") is None
                or abs(float(comp.properties.get("z_level", 0)) - float(z_ref)) <= z_band
            ]
            if not plan_at_z:
                plan_at_z = list(plan_nodes_all)
            nodes_by_view_plan = dict(nodes_by_view)
            nodes_by_view_plan["plan"] = plan_at_z
            paired_total += _pair_front_plan_at_z(
                model, nodes_by_view_plan, merged, z_ref=z_ref, z_band=z_band,
            )
    if df is not None and paired_total:
        df.properties["plan_pairings"] = paired_total

    # ---- M5：synthetic side → front+side 解 y ----
    syn_recovered = _recover_y_via_synthetic_side(model, merged, overlay=overlay)
    if df is not None and syn_recovered:
        df.properties["y_synthetic_side"] = syn_recovered

    # ---- front + plan bar_id 端点传播 y（共享件号杆件）----
    _propagate_front_y_via_bar_id(model, nodes_by_view, merged, eps)

    # ---- cross_file：同 Z 带 y 插值（需 overlay 显式开启）----
    if cross_file_allow_z_peer_interpolate(overlay=overlay):
        _interpolate_front_y_from_z_peers(model, merged, eps)

    # ---- front + side + section 三视图线性解耦（a 已在上面计算）----
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


def merge_view_bars(
    model: EngineeringModel,
    overlay: Optional[str | Path | dict] = None,
) -> EngineeringModel:
    """把跨视图投影合并为物理杆件。

    主视图（front/elevation）每个物理杆件只画一次，因此以主视图为骨架：
        * 仅保留主视图的 tower_node / tower_bar，删除其它视图投影
        * 用已解算的节点三轴坐标计算 length_mm_3d
        * 用 BOM 长度把 UNLABELED 主视图杆件匹配回 bar_id（唯一匹配才接受）
        * BOM 维度的 applies_to 重指到合并后的杆件

    overlay：per-project 视图规范（P0-1），下传给 merge_view_coordinates。
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
    primary_bars = [
        c for c in bars
        if c.properties.get("view_type") == primary
        and c.properties.get("from_node") != c.properties.get("to_node")
    ]

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

    # 重建组件：保留主视图节点/杆件 + 图纸上下文 + BOM + 连接详图
    _KEEP_KINDS = frozenset({
        "drawing_file", "bom_row", "gusset_plate", "bolt_group", "detail_view",
    })
    keep_components = {}
    for cid, c in model.components.items():
        if c.kind in _KEEP_KINDS:
            keep_components[cid] = c

    # P2-6 跨视图身份：删除非主视图投影前，把它们的二维投影来源挂到主物理杆件。
    # 严禁静默丢弃 side/plan/detail 投影——每条投影必须要么挂到匹配的主杆件
    # （projection_refs），要么进入 unresolved_projection_refs 供人工复核。
    projection_refs_by_bar: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    unresolved_projection_refs: List[Dict[str, Any]] = []

    # 按「件号 + 3D 长度」把非主视图投影匹配回主杆件；匹配不了则记录 unresolved。
    for cid, c in model.components.items():
        if c.kind != "tower_bar":
            continue
        vt = c.properties.get("view_type")
        if vt == primary:
            continue
        # 非主视图投影的件号与来源信息
        bid = c.properties.get("bar_id", "")
        if bid.startswith("UNLABELED"):
            bid = ""
        proj_ref = {
            "sheet_id": c.properties.get("source_file") or c.properties.get("drawing_view") or cid,
            "view_type": vt,
            "component_id": cid,
            "source_reference": (c.source.reference if c.source else None) or "",
            "confidence": (c.source.confidence if c.source else None) or 0.5,
            "geometry_origin": c.properties.get("geometry_origin") or (
                "dxf_geom" if c.source and c.source.source_type != "derived" else "derived"
            ),
        }
        # 匹配：同件号且同 3D 长度（容差 1%）的主杆件
        target_bar_ids = []
        if bid:
            for bar in primary_bars:
                if bar.properties.get("bar_id", "") == bid:
                    target_bar_ids.append(bar.id)
        # 未按件号匹配时，尝试按 3D 长度匹配（更松）
        if not target_bar_ids:
            p_ln = _bar_3d_length(c, model)
            if p_ln is not None:
                for bar in primary_bars:
                    b_ln = bar.properties.get("length_mm_3d")
                    if b_ln is not None and abs(p_ln - b_ln) / b_ln <= 0.01:
                        target_bar_ids.append(bar.id)
        if len(target_bar_ids) == 1:
            projection_refs_by_bar[target_bar_ids[0]].append(proj_ref)
        elif len(target_bar_ids) > 1:
            # 多个候选：无法唯一定位，进入 unresolved
            unresolved_projection_refs.append({**proj_ref, "candidates": target_bar_ids})
        else:
            unresolved_projection_refs.append(proj_ref)

    for cid in sorted(primary_nodes):
        keep_components[cid] = model.components[cid]
    for bar in primary_bars:
        # 把匹配到的投影引用挂到主物理杆件
        if projection_refs_by_bar.get(bar.id):
            existing = list(bar.properties.get("projection_refs") or [])
            existing.extend(projection_refs_by_bar[bar.id])
            bar.properties["projection_refs"] = existing
        keep_components[bar.id] = bar

    # unresolved 投影不静默丢弃：写入 drawing_file 供报告/人工复核。
    if unresolved_projection_refs:
        df = keep_components.get("drawing_file")
        if df is not None:
            df.properties["unresolved_projection_refs"] = unresolved_projection_refs

    # 主立面 X/Y 中心归零（view_align.*.normalize_x）：国网/闲鱼立面图坐标常
    # 落在图纸绝对坐标（如 x≈34000），归零后 X/Y 关于 0 对称、GLB 不再偏在一边。
    # Y 也一起归零：front(X,Z)+side(Y,Z) 合并后，侧视 Y 轴同样需要居中。
    if cross_file_normalize_x(overlay=overlay):
        # 只用「已解算三轴」的主视图节点做居中；未解算节点仍保留图纸绝对坐标，
        # 若混入会把中心拉到 ~34000 的图纸坐标系里。
        solved_nodes = [
            c for c in keep_components.values()
            if c.kind == "tower_node"
            and c.properties.get("view_type") == primary
            and all(c.properties.get(a) is not None for a in ("x", "y", "z"))
        ]
        xs = [float(c.properties["x"]) for c in solved_nodes]
        ys = [float(c.properties["y"]) for c in solved_nodes]
        if xs:
            x_center = (min(xs) + max(xs)) / 2
            for c in keep_components.values():
                if c.kind == "tower_node" and c.properties.get("x") is not None:
                    c.properties["x"] = round(float(c.properties["x"]) - x_center, 2)
        if ys:
            y_center = (min(ys) + max(ys)) / 2
            for c in keep_components.values():
                if c.kind == "tower_node" and c.properties.get("y") is not None:
                    c.properties["y"] = round(float(c.properties["y"]) - y_center, 2)

    # 阶段 5.3：多段立面拼接处段边界节点缝合（≤5mm 自动共享合并，消除连通分量与悬空断裂）
    _stitch_multisheet_boundary_nodes(keep_components, snap_tol_mm=25.0)

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


def _stitch_multisheet_boundary_nodes(
    components: Dict[str, Component],
    snap_tol_mm: float = 25.0,
) -> None:
    """阶段 5.3：在多段立面（如 02/04/05/06/07/40）拼接处，将空间位置极其接近（<=snap_tol_mm）
    的相邻段边界节点合并为共享节点，重指杆件 from/to，消除跨段断裂。
    """
    nodes = {
        cid: c for cid, c in components.items()
        if c.kind == "tower_node"
        and all(c.properties.get(a) is not None for a in ("x", "y", "z"))
    }
    if len(nodes) < 2:
        return

    # 按空间坐标空间聚类
    node_ids = list(nodes.keys())
    coords = [
        (float(nodes[nid].properties["x"]), float(nodes[nid].properties["y"]), float(nodes[nid].properties["z"]))
        for nid in node_ids
    ]

    # 找出等价重叠节点对并构建并查集 / 映射表
    node_remap: Dict[str, str] = {}
    visited = set()
    for i in range(len(node_ids)):
        if i in visited:
            continue
        canon_id = node_ids[i]
        c1 = coords[i]
        for j in range(i + 1, len(node_ids)):
            if j in visited:
                continue
            c2 = coords[j]
            # 快速检查 z 差、x 差、y 差
            if abs(c1[2] - c2[2]) <= snap_tol_mm and abs(c1[0] - c2[0]) <= snap_tol_mm and abs(c1[1] - c2[1]) <= snap_tol_mm:
                dist = ((c1[0]-c2[0])**2 + (c1[1]-c2[1])**2 + (c1[2]-c2[2])**2)**0.5
                if dist <= snap_tol_mm:
                    visited.add(j)
                    target_id = node_ids[j]
                    node_remap[target_id] = canon_id

    if not node_remap:
        return

    # 1. 删除被合并的冗余节点
    for redundant_id in node_remap:
        if redundant_id in components:
            del components[redundant_id]

    # 2. 重指杆件 from_node / to_node，并消除合并后产生的零长自环杆与重叠重复杆
    bars = [c for c in components.values() if c.kind == "tower_bar"]
    seen_bar_pairs = set()
    bars_to_delete = set()

    for bar in bars:
        fn = bar.properties.get("from_node")
        tn = bar.properties.get("to_node")
        new_fn = node_remap.get(fn, fn)
        new_tn = node_remap.get(tn, tn)
        bar.properties["from_node"] = new_fn
        bar.properties["to_node"] = new_tn

        if new_fn == new_tn:
            # 自环退化杆
            bars_to_delete.add(bar.id)
            continue

        pair_key = (min(str(new_fn), str(new_tn)), max(str(new_fn), str(new_tn)))
        if pair_key in seen_bar_pairs:
            # 重叠重合横杆/连接杆
            bars_to_delete.add(bar.id)
        else:
            seen_bar_pairs.add(pair_key)

    for bid in bars_to_delete:
        if bid in components:
            del components[bid]


# --------------------------------------------------------------------------- #
# Phase 2  单立面 -> 四面封闭空间网架（EngineeringModel 包装）
# --------------------------------------------------------------------------- #

# P1 拆分：四向镜像展开已迁到 tower_symmetry，这里 re-import 保留旧名。
from .tower_symmetry import expand_4_face_symmetry_model  # noqa: F401,E402
