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
from typing import Any, Dict, List, Optional, Tuple

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


def _robust_segment_span(
    vy_values: List[float],
    lo_q: float = 0.02,
    hi_q: float = 0.98,
) -> Tuple[float, float]:
    """分段立面图的稳健竖向几何跨度（阶段5.1）。

    简单 min/max 会被图框刻度线/极值碎片拉扯（某段多一根 stray tick 就把
    整段高程归一化拉偏）。这里用分位数取上下界：默认 2%/98% 分位，抵抗
    少量极端碎片；样本过少（<5）时退回 min/max（分位数无意义）。
    返回 (lo, hi)，保证 hi > lo。
    """
    if len(vy_values) < 5:
        lo, hi = min(vy_values), max(vy_values)
        return lo, hi if hi > lo else (lo, lo + 1.0)
    s = sorted(vy_values)
    n = len(s)
    li = max(0, min(n - 1, int(n * lo_q)))
    hi_i = max(li + 1, min(n - 1, int(n * hi_q)))
    lo, hi = s[li], s[hi_i]
    if hi <= lo:
        lo, hi = s[0], s[-1]
    if hi <= lo:
        return lo, lo + 1.0
    return lo, hi


def _normalize_segment_view_y(
    nodes_by_view: Dict[str, List[Tuple[str, Component]]],
    overlay: Optional[str | Path | dict] = None,
    beat_anchors_by_view: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Tuple[float, float]]:
    """阶段5.1：把分段立面/侧立面的局部 view_y 归一化为全局 Z（原地改写 view_y）。

    多段塔（02/04/05/06/07/40 各带 z_offset / z_span_mm）的每张图局部 view_y
    0 点落在图框左上（region origin），塔段下方还有标注空间，所以直接把
    view_y 加 z_offset 会把段间接头重叠累加进高程，产生全塔高累积漂移
    ≈733.8mm。本函数按 (drawing_view, view_kind) 分组，用稳健分位数跨度
    [lo,hi] 把该段竖向几何线性归一化到标注段高 [0, span_mm]，再加 z_offset，
    原地改写 front/side 节点的 view_y 为全局 Z——这样下游所有
    `z = view_y` 的路径（front 单立面、front+side 配对、peer-fill、gusset
    锚定）都自动拿到正确高程，无需逐路径传参。

    P2.1 DIMENSION 节拍锚定（beat_anchors_by_view）：若该 drawing_view
    存在 DIMENSION 主节拍链锚点（tower_dxf 解析、drawing_file 属性透传，
    key = drawing_view 即 stem），则用节拍分段线性映射替代分位数线性——
    消除「节点分布跨度 ≠ 模块高度」的系统性畸变（06 册实测：节点跨度
    5544mm vs 模块高 5030mm，分位数线性在段底/顶各偏 300-550mm；
    节拍链 TP@500 0→19）。

    只改写「带 z_span_mm 声明」的分段图；无 z_span_mm 的单立面/详图不改。
    返回 {key: (lo, hi)}（key = (drawing_view, view_kind)），供审计。

    注意：原局部 view_y 会保存在 `view_y_local`（若非 None），保持可追溯。
    """
    from .tower_spec import view_z_offset, view_z_span_mm, view_region

    def _piecewise_beat_z(anchors: Dict[str, Any], vy: float) -> Optional[float]:
        """节拍锚点分段线性映射 view_y → z（锚点已按 vy 升序）。"""
        vys = anchors.get("vy") or []
        zs = anchors.get("z") or []
        if len(vys) < 2 or len(vys) != len(zs):
            return None
        pairs = sorted(zip(vys, zs))
        if vy <= pairs[0][0]:
            (y0, z0), (y1, z1) = pairs[0], pairs[1]
        elif vy >= pairs[-1][0]:
            (y0, z0), (y1, z1) = pairs[-2], pairs[-1]
        else:
            (y0, z0), (y1, z1) = pairs[0], pairs[1]
            for i in range(len(pairs) - 1):
                if pairs[i][0] <= vy <= pairs[i + 1][0]:
                    (y0, z0), (y1, z1) = pairs[i], pairs[i + 1]
                    break
        if y1 == y0:
            return z0
        # P2.1b（2026-09-04，修订 2）：锚点域外用**边缘段斜率线性延拓**。
        # 初版「域外一律 None → 分位数兜底」误伤了显式层位表的域外层
        # （07 册 6500 层：_y_of_z 反解到 y=-11265.4 < 首锚 -11239.3，
        # 归一化时被兜底错送到 z=7100）。反解（centerline_extract
        # _y_of_z）与正向（本函数）必须互逆——两者都允许域外一阶
        # 延拓。06 册段底裁剪线残留同样会被延拓到 ~11400，但它们不是
        # 横杆层、匹配不上 GT，只增加少量 FP（与 full-deliver 行为
        # 一致，不引入新的诚实性问题）。
        return z0 + (vy - y0) / (y1 - y0) * (z1 - z0)

    # 1) 按 (drawing_view, view_kind) 收集 front/side 节点的 view_y。
    #    只有声明了 z_span_mm 的段才参与归一化（否则保持原样）。
    groups: Dict[str, Dict[str, Any]] = {}
    for kind in ("front", "side"):
        for cid, comp in nodes_by_view.get(kind, []):
            p = comp.properties
            vy = p.get("view_y")
            if vy is None:
                continue
            dv = str(p.get("drawing_view") or "")
            if not dv:
                continue
            span_mm = view_z_span_mm(dv, kind, overlay=overlay)
            if span_mm is None:
                continue
            key = (dv, kind)
            g = groups.setdefault(key, {
                "span_mm": span_mm,
                "z_off": view_z_offset(dv, kind, overlay=overlay),
                "z_flip": bool((view_region(dv, kind, overlay=overlay) or {}).get("z_flip")),
                "z_axis_up": bool((view_region(dv, kind, overlay=overlay) or {}).get("z_axis_up")),
                "vals": [],
                "nodes": [],
            })
            g["vals"].append(float(vy))
            g["nodes"].append((cid, comp))

    beat_anchors_by_view = beat_anchors_by_view or {}
    bounds: Dict[str, Tuple[float, float]] = {}
    # 2) 逐组归一化并原地改写 view_y。
    for key, g in groups.items():
        dv, kind = key
        span_mm = float(g["span_mm"])
        z_off = float(g["z_off"])
        z_flip = bool(g["z_flip"])
        z_axis_up = bool(g.get("z_axis_up"))
        # z_invert（S2 前置发现）：该段的「宽端」画在图框上部（view_y 小=段底）。
        # 注意与 z_flip 的本质区别：z_flip=True 会同时触发 tower_dxf 的 ly=-ly
        # 与本函数的公式切换，两者数学上恰好抵消（lo/hi 随符号翻转镜像，
        # (hi-raw) 与 (raw'-lo') 代入后同值），对最终 Z 无净效果。
        # z_invert 只在归一化层把方向反过来（local → span-local），不触碰
        # DXF 层——用于「图框顶=宽端」的图纸（35A1-JC1 的 07/06/05/04 段，
        # 实测主腿锥度 +0.076/mm 与 GT -0.07/mm 镜像，翻转后吻合到 3~11mm）。
        z_invert = bool((view_region(dv, kind, overlay=overlay) or {}).get("z_invert"))
        # P2.1：DIMENSION 节拍锚点优先（仅 front 面——节拍链来自正立面
        # 竖向标注；side 面若无独立节拍链则回退分位数）。
        anchors = beat_anchors_by_view.get(dv) if kind == "front" else None
        lo, hi = _robust_segment_span(g["vals"])
        bounds[f"{dv}__{kind}"] = (lo, hi)
        n_beat_mapped = 0
        for cid, comp in g["nodes"]:
            p = comp.properties
            raw = float(p["view_y"])
            z_val = None
            if anchors is not None:
                z_val = _piecewise_beat_z(anchors, raw)
            if z_val is not None:
                p["view_y_local"] = p.get("view_y")
                p["view_y"] = round(float(z_val), 2)
                p["segment_z_normalized"] = True
                p["segment_z_beat_anchored"] = True
                n_beat_mapped += 1
                continue
            # view_y 语义：CAD 通常 Y 向下，view_y=0 落在段顶（几何窄端），
            # view_y=hi 落在段底（宽端）。若 region 未声明 z_flip（默认），
            # 需把几何翻转成「向上为正」：hi→0（段底=z_offset）、
            # lo→span_mm（段顶=z_offset+span_mm）。若 z_flip=True（tower_dxf
            # 已做过 ly=-ly），则 view_y=0 已是段底，直接 lo→0。
            if z_flip:
                local = (raw - lo) / (hi - lo) * span_mm
            elif z_axis_up:
                # P3.15（JC2 泛化）：图纸 Y 轴向上（tower_dxf 未做 ly=-ly，
                # view_y 小=段底）——正向映射 lo→z_offset。
                local = (raw - lo) / (hi - lo) * span_mm
            else:
                local = (hi - raw) / (hi - lo) * span_mm
            if z_invert:
                local = span_mm - local
            p["view_y_local"] = p.get("view_y")
            p["view_y"] = round(z_off + local, 2)
            p["segment_z_normalized"] = True
            if z_invert:
                p["segment_z_inverted"] = True
        if anchors is not None and g["nodes"]:
            # 审计：节拍映射覆盖率（0 = 锚点域异常，全部回退分位数）
            bounds[f"{dv}__{kind}__beat"] = (n_beat_mapped, len(g["nodes"]))
    return bounds


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
                # 阶段3 修复：同 Z 带 peer 全在单侧时 t 远超 [0,1]（实测 -23.58），
                # 外推出假深度（N02 y=17988mm）。钳位到 [0,1] 退化为取最近 peer 的 y。
                t = max(0.0, min(1.0, t))
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
                # 阶段3 修复：同 Z 带 peer 全在单侧时 t 远超 [0,1]（实测 -23.58），
                # 外推出假深度（N02 y=17988mm）。钳位到 [0,1] 退化为取最近 peer 的 y。
                t = max(0.0, min(1.0, t))
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

    # 阶段5.1：分段立面/侧立面局部 view_y → 全局 Z 归一化（原地改写 view_y）。
    # 必须在此处（所有分桶/配对之前）执行，否则 front+side 配对与 peer-fill
    # 路径会用未归一化的 view_y 当 Z，把段间接头重叠累加进高程（733.8mm 漂移）。
    # P2.1：优先用 tower_dxf 解析的 DIMENSION 节拍锚点（drawing_file 属性，
    # key = drawing_view/stem），节拍分段线性替代分位数线性。
    _df_beat = model.components.get("drawing_file")
    _beat_by_view: Dict[str, Dict[str, Any]] = {}
    if _df_beat is not None:
        _ba = _df_beat.properties.get("dimension_beat_anchors")
        if isinstance(_ba, dict) and _ba.get("vy") and _ba.get("z"):
            _stem = _model_stem(model) or str(_df_beat.properties.get("drawing_view") or "")
            if _stem:
                _beat_by_view[_stem] = _ba
        # 跨文件合并模型：各册锚点在 by_sheet 字典里（tower_batch 透传）
        _by_sheet = _df_beat.properties.get("dimension_beat_anchors_by_sheet")
        if isinstance(_by_sheet, dict):
            for _s, _a in _by_sheet.items():
                if isinstance(_a, dict) and _a.get("vy") and _a.get("z"):
                    _beat_by_view[str(_s)] = _a
    _normalize_segment_view_y(
        nodes_by_view, overlay=overlay, beat_anchors_by_view=_beat_by_view)

    # front/side/section 的分桶键：view_y（即 Z，已在上面归一化为全局）
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
        front_nodes = nodes_by_view.get("front", [])
        # 阶段5.1：view_y 已在 _normalize_segment_view_y 中归一化为全局 Z，
        # 这里直接 z = view_y（不再二次归一化）。
        for cid, comp in front_nodes:
            p = comp.properties
            ux, uz = p.get("view_x"), p.get("view_y")
            if ux is None or uz is None:
                continue
            uz_global = float(uz)
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

    # ---- 阶段3 修复：未配对的 side 节点不得泄漏原始图纸 x ----
    # side 视图的横向轴是塔身 Y（深度），其原始图纸 x（如 34557~34701）只是
    # 该视图在整张图纸上的水平位置，不是塔身 X。配对失败（front/side 各 z 带
    # 节点数不一致，匈牙利一对一留余）时，这些节点仍保留 tower_dxf 写入的
    # 原始 node["x"]/node["y"]，会污染下游半宽/bbox 度量（02 段半宽 ±34701 即此因）。
    # 这里把仍为 partial 的 side 节点的 x/y 清零（塔身 x 未知，y 才由 side 提供），
    # z 保持 None；不影响杆件绑定（绑定在 tower_dxf 提取期已用原始坐标完成）。
    for cid, comp in nodes_by_view.get("side", []):
        p = comp.properties
        if p.get("solve_status") == "partial" and (p.get("x") is not None or p.get("y") is not None):
            p["x"] = None
            p["y"] = None
            p["side_unpaired_xy_cleared"] = True

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


# --------------------------------------------------------------------------- #
# 阶段1.1' / 1.3（JC1 单塔修复计划）：来源段门禁（fail-closed）。
#
# 归因记录（2026-08-30，484 根 dz>8m 杆）：merge_view_coordinates 的
# `uz_global = z_off + view_y` 对 region 拿错的节点（整塔单线图 / 图纸角部
# 图号章区）量纲爆炸——06 段节点 z 被算进 25000-30000（04 段范围）。
# 本门禁在四面展开前按 source_sheet 的段 Z 范围剔除越界杆，并给全部物理杆
# 写 source_sheet / source_z_range / interface_bar 溯源属性。
# --------------------------------------------------------------------------- #

# 35A1-JC1 六段塔身默认段高表（mm）。可被 overlay 的 module_z_ranges 覆盖。
DEFAULT_MODULE_Z_RANGES: Dict[str, Tuple[float, float]] = {
    "35A1-JC1-40": (0.0, 5500.0),
    "35A1-JC1-07": (5500.0, 11000.0),
    "35A1-JC1-06": (11000.0, 16000.0),
    "35A1-JC1-05": (16000.0, 23000.0),
    "35A1-JC1-04": (23000.0, 30000.0),
    "35A1-JC1-02": (30000.0, 36600.0),
}


def enforce_source_segment_gate(
    model: EngineeringModel,
    *,
    overlay: Optional[str | Path | dict] = None,
    tol_mm: float = 1000.0,
    ranges: Optional[Dict[str, Tuple[float, float]]] = None,
) -> Dict[str, Any]:
    """来源段门禁：物理杆两端 Z 必须落在 source_sheet 的段范围内。

    规则（对齐 JC1 修复计划 阶段1.3）：
      * 只检查 recognized / reconstructed 物理杆；derived/helper 不参与
        门禁（它们也不进 P/R）。
      * source_sheet 的段范围查 overlay 的 module_z_ranges（缺省用
        DEFAULT_MODULE_Z_RANGES）；不在段表内的 sheet（如平面图）跳过。
      * 边界容差 tol_mm 只吸收 Z 映射的段间漂移（实测 ≈734mm 累积），
        不是评测容差；跨段污染（如 06 节点 z≈25000-30000）远超此容差。
      * interface_bar=true 的杆豁免（相邻模块接口杆在阶段 5 拼接）。
      * 违规杆 fail-closed：打 segment_gate_failed 标记后**删除**，并清理
        悬空的 connections / rules / dimensions 引用；绝不静默保留。

    返回报告 dict（checked / removed / removed_ids / removed_by_sheet /
    no_z_skipped / kept_interface），供 run_manifest 与 review_queue 记录。
    """
    from .tower_spec import load_tower_spec

    if ranges:
        rng = {str(k): (float(v[0]), float(v[1])) for k, v in ranges.items()}
    else:
        raw = load_tower_spec(overlay).get("module_z_ranges")
        rng = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    rng[str(k)] = (float(v[0]), float(v[1]))
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
        if not rng:
            rng = {k: (float(v[0]), float(v[1])) for k, v in DEFAULT_MODULE_Z_RANGES.items()}

    nodes = {
        cid: c for cid, c in model.components.items() if c.kind == "tower_node"
    }
    checked = removed = no_z = kept_interface = 0
    removed_ids: List[str] = []
    by_sheet: Dict[str, int] = {}

    for cid, comp in list(model.components.items()):
        if comp.kind != "tower_bar":
            continue
        p = comp.properties
        if p.get("geometry_class") not in ("recognized", "reconstructed"):
            continue
        raw_sheet = str(p.get("source_file") or p.get("drawing_view") or "")
        stem = raw_sheet.replace("\\", "/").rsplit("/", 1)[-1]
        r = rng.get(stem)
        # 阶段1.3 溯源属性：无论是否命中段表都写（无段表写 None）
        p["source_sheet"] = stem or None
        p["source_z_range"] = [r[0], r[1]] if r else None
        if "interface_bar" not in p:
            p["interface_bar"] = False
        if r is None:
            continue
        fn, tn = p.get("from_node"), p.get("to_node")
        nf, nt = nodes.get(fn), nodes.get(tn)
        zf = nf.properties.get("z") if nf is not None else None
        zt = nt.properties.get("z") if nt is not None else None
        if zf is None or zt is None:
            no_z += 1
            continue
        checked += 1
        if p.get("interface_bar"):
            kept_interface += 1
            continue
        lo = float(r[0]) - float(tol_mm)
        hi = float(r[1]) + float(tol_mm)
        if lo <= float(zf) <= hi and lo <= float(zt) <= hi:
            continue
        # 越界：fail-closed 剔除
        removed += 1
        removed_ids.append(cid)
        by_sheet[stem] = by_sheet.get(stem, 0) + 1
        p["segment_gate_failed"] = True

    if removed_ids:
        removed_set = set(removed_ids)
        for cid in removed_ids:
            del model.components[cid]
            model.staleness.pop(cid, None)
        if model.connections:
            drop = [
                k for k, c in model.connections.items()
                if c.from_component in removed_set or c.to_component in removed_set
            ]
            for k in drop:
                del model.connections[k]
                model.staleness.pop(k, None)
        if model.rules:
            drop = [
                k for k, rule in model.rules.items()
                if rule.applies_to and any(a in removed_set for a in rule.applies_to)
            ]
            for k in drop:
                del model.rules[k]
                model.staleness.pop(k, None)
        if model.dimensions:
            drop = [k for k, d in model.dimensions.items() if d.applies_to in removed_set]
            for k in drop:
                del model.dimensions[k]
                model.staleness.pop(k, None)

    return {
        "checked": checked,
        "removed": removed,
        "removed_ids": removed_ids[:200],
        "removed_by_sheet": by_sheet,
        "no_z_skipped": no_z,
        "kept_interface": kept_interface,
        "tol_mm": float(tol_mm),
    }


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

    # ---- P2.4b side 直读冻结（freeze-only，零共享态修改）----
    # side 视图画线是 l/r 面 GT 杆的唯一直读证据（front 投影结构性退化为
    # 竖线/点）。把「两端点 y/z 均已解算」的 side 杆几何冻结到
    # drawing_file.side_reads，供管线末端（四面展开之后）以全新组件注入：
    #   * x 已解算（front_side_z_pair 双向解）→ x 原值；
    #   * x 未解算 → x = -hw(z)（直读面 l），镜像孪生 +hw（面 r）。
    # 只冻结、不改动任何现有组件——避免污染四面展开的输入分布。
    _side_reads: List[Dict[str, Any]] = []
    _hw_freeze = None
    try:
        from ..solve.tower_geometry import fit_tower_half_width_from_face
        _fr_nodes = {}
        for _ncid, _nc in model.components.items():
            if _nc.kind != "tower_node":
                continue
            _np = _nc.properties
            if _np.get("view_type") != "front":
                continue
            if any(_np.get(_a) is None for _a in ("x", "y", "z")):
                continue
            _fr_nodes[_ncid] = (float(_np["x"]), float(_np["y"]), float(_np["z"]))
        _fr_bars = [
            {"from": _bc.properties.get("from_node"),
             "to": _bc.properties.get("to_node")}
            for _, _bc in _tower_bars(model)
            if _bc.properties.get("view_type") == "front"
        ]
        if _fr_nodes and _fr_bars:
            _hw_freeze = fit_tower_half_width_from_face(
                _fr_nodes, _fr_bars, method="taper")
    except Exception:
        _hw_freeze = None
    for _cid, _c in model.components.items():
        if _c.kind != "tower_bar" or _c.properties.get("view_type") != "side":
            continue
        _fn, _tn = _c.properties.get("from_node"), _c.properties.get("to_node")
        _nf = model.components.get(_fn) if _fn else None
        _nt = model.components.get(_tn) if _tn else None
        if _nf is None or _nt is None or _nf is _nt:
            continue
        _pf, _pt = _nf.properties, _nt.properties
        # P2.4b-2：未配对 side 节点的视图域求解（本地计算，零共享态修改）。
        # _normalize_segment_view_y 已把 side 节点 view_y 原地改写为全局 Z
        # （同 front 同域，节拍/锚分段线性），view_x 即 y 深度轴（与 z_pair
        # 的 yp 同一变量，expand=0 时 y'=yp）。据此 z=view_y、y=view_x 可
        # 独立解算，无需 front 配对。
        # 门禁：仅当该节点组确实被归一化过（view_y_local 已写入）才信任
        # view_y 是全局 Z——ZC1 实测：side 组未归一化（z_axis_up 等特殊
        # 布置）时 view_y 是局部值。一切只在本地计算，绝不回写组件属性
        # （先前回写版本曾让 ZC1 05 册跨册合并从 73 杆崩到 25 杆）。
        _zys = []
        for _pp in (_pf, _pt):
            _zz = _pp.get("z")
            if _zz is None and _pp.get("view_y") is not None \
                    and _pp.get("view_y_local") is not None:
                _zz = round(float(_pp["view_y"]), 2)
            _yy = _pp.get("y")
            if _yy is None and _pp.get("view_x") is not None:
                _yy = round(float(_pp["view_x"]), 2)
            if _zz is None or _yy is None:
                _zys = None
                break
            _zys.append((_yy, _zz))
        if _zys is None:
            continue
        # P2.4b-3：z 端点层位吸附（z-only 设计常数，同 marker_synth 纪律）。
        # 实测（02 册）：配对平均 z 落在 GT 层位中间（如 34452 介于 34200/
        # 34900 之间），端点误差 +130~+500mm 系统性超差；把读取端点 z 吸附
        # 到来源册 beam_marker_levels_mm（±300mm 内）消差后近失 FP 转 TP。
        _stem = str(_c.properties.get("source_file") or _c.properties.get("drawing_view") or "")
        _lv = []
        try:
            from .centerline_extract import _overlay_cfg
            _ce = _overlay_cfg(_stem, overlay) or {}
            _lv = [float(v) for v in (_ce.get("beam_marker_levels_mm") or [])]
        except Exception:
            _lv = []
        if _lv:
            _zys = [
                (_yy, round(min(_lv, key=lambda v: abs(v - _zz)), 2)
                 if abs(min(_lv, key=lambda v: abs(v - _zz)) - _zz) <= 300.0 else _zz)
                for _yy, _zz in _zys]
        (_y1, _z1), (_y2, _z2) = _zys
        _x1, _x2 = _pf.get("x"), _pt.get("x")
        _x_src = "z_pair"
        _z_snapped = _lv and (
            any(abs(_z1 - v) < 1e-6 for v in _lv) or
            any(abs(_z2 - v) < 1e-6 for v in _lv))
        if _x1 is None or _x2 is None:
            if _hw_freeze is None:
                continue
            try:
                _h1, _h2 = float(_hw_freeze(_z1)), float(_hw_freeze(_z2))
            except Exception:
                continue
            if _h1 < 50 or _h2 < 50:
                continue
            _x1, _x2 = -_h1, -_h2
            _x_src = "face_plane"
        _side_reads.append({
            "from": [round(float(_x1), 2), round(_y1, 2), round(_z1, 2)],
            "to": [round(float(_x2), 2), round(_y2, 2), round(_z2, 2)],
            "x_source": _x_src,
            "z_snapped": bool(_z_snapped),
            "bar_id": _c.properties.get("bar_id"),
            "section": _c.properties.get("section"),
            "source_file": _c.properties.get("source_file") or _c.properties.get("drawing_view"),
            "handle": _c.properties.get("handle"),
            "geometry_origin": _c.properties.get("geometry_origin"),
            "geometry_class": _c.properties.get("geometry_class"),
            "source_extractor": _c.properties.get("source_extractor"),
            "confidence": (_c.source.confidence if _c.source else None) or 0.5,
            "reference": (_c.source.reference if _c.source else None) or "",
        })
    if _side_reads:
        _dfz = model.components.get("drawing_file")
        if _dfz is not None:
            _dfz.properties["side_reads"] = _side_reads
            _dfz.properties["side_reads_n"] = len(_side_reads)
            _dfz.properties["side_reads_hw_fit"] = _hw_freeze is not None

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
        # 阶段 5.2：多候选时用截面（section）作为第三属性消歧。
        # 件号缺失 + 长度碰撞的对称杆件，常可通过截面类型（角钢/圆钢/规格）区分。
        if len(target_bar_ids) > 1:
            p_sec = c.properties.get("section")
            if p_sec:
                sec_matches = [
                    bid_ for bid_ in target_bar_ids
                    if model.components.get(bid_, Component(id="", name="", kind="")).properties.get("section") == p_sec
                ]
                if len(sec_matches) == 1:
                    target_bar_ids = sec_matches
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


# --------------------------------------------------------------------------- #
# P2.4b side 直读注入（四面展开之后执行）
# --------------------------------------------------------------------------- #

def side_horiz_synth(
    model: "EngineeringModel",
    overlay: Optional[str | Path | dict],
    dxf_path_by_stem: Dict[str, str],
) -> int:
    """P2.4j：侧立面横杆直读通道（y_member 捕获）。

    体段侧立面（04/05/06/07 册）把横杆画成全跨/半跨水平线（实测 05 册
    z=19000：全跨 2646mm 双线 + 半跨 1204mm×2 对），但通用管线节点聚类
    把长线劈成 96~367mm 碎片（每一根交叉斜线端点都成节点），y_member GT
    是全跨 [-hw,+hw] / 半跨 [±hw,0] 杆——碎片恒失配。

    本通道绕过节点聚类，直接读侧立面 region 的水平线：
      * 双线合并（平行对 ≤8u → 中心线）；
      * 图纸 y → z 用 dimension_beat_anchor 锚链（与 side 节点归一化同链，
        05 实测侧读 z 簇与层位对齐已验证该映射可用）；
      * z 吸附 beam_marker_levels_mm（±300mm，同 P2.4b-3 纪律），吸附
        失败的水平线丢弃（非层位处的线是标注/尺寸线）；
      * y 深度吸附半宽锥：全跨线 → [-hw(z), +hw(z)]；半跨线（端点在
        中心附近）→ [±hw(z), 0]。半宽锥由 front 腿节点拟合（taper，
        fit_tower_half_width_from_face），图纸证据，零 GT 注入；
      * 冻结为 side_reads 附加条目（x=-hw(z) face_plane 语义），由
        apply_side_reads 注入为 side_direct 3D 组件（纯口径 recognized）。

    开关：overlay 顶层 side_horiz_synth（默认关）。返回追加的读取数。
    """
    try:
        import ezdxf
        import json as _json
        from .centerline_extract import collect_segments, seg_len, _overlay_cfg
        from .tower_spec import view_regions as _view_regions
    except Exception:
        return 0
    try:
        from ..solve.tower_geometry import fit_tower_half_width_from_face
    except Exception:
        return 0
    import math as _m

    # 1) 开关
    try:
        spec = overlay if isinstance(overlay, dict) else (
            _json.loads(Path(overlay).read_text(encoding="utf-8"))
            if overlay and Path(overlay).exists() else {})
    except Exception:
        return 0
    if not spec.get("side_horiz_synth"):
        return 0

    # 2) 半宽锥（front 节点拟合；与 merge_view_bars side_reads 同法）
    _fr_nodes = {}
    for _ncid, _nc in model.components.items():
        if _nc.kind != "tower_node":
            continue
        _np = _nc.properties
        if _np.get("view_type") != "front":
            continue
        if any(_np.get(_a) is None for _a in ("x", "y", "z")):
            continue
        _fr_nodes[_ncid] = (float(_np["x"]), float(_np["y"]), float(_np["z"]))
    _fr_bars = [
        {"from": _bc.properties.get("from_node"),
         "to": _bc.properties.get("to_node")}
        for _, _bc in _tower_bars(model)
        if _bc.properties.get("view_type") == "front"
    ]
    if _fr_nodes and _fr_bars:
        try:
            hw_fit = fit_tower_half_width_from_face(_fr_nodes, _fr_bars, method="taper")
        except Exception:
            hw_fit = None
    else:
        hw_fit = None
    if hw_fit is None:
        return 0

    df = model.components.get("drawing_file")
    if df is None:
        return 0
    reads = df.properties.setdefault("side_reads", [])

    added = 0
    for stem, dxf_path in sorted(dxf_path_by_stem.items()):
        # 该册必须显式开（体段册逐一启用，防 02 头部已直读区重复）
        _ce = _overlay_cfg(stem, overlay) or {}
        if not _ce.get("side_horiz_synth"):
            continue
        # region 来源：专用键 side_horiz_synth_region（bbox/origin/scale_x），
        # 与 view_regions 解耦——常规提取不会再产生碎片 side_direct FP，
        # 本通道自读 DXF。
        _sr = _ce.get("side_horiz_synth_region") or {}
        if not _sr.get("region"):
            continue
        side_region = {
            "region": _sr["region"],
            "origin": _sr.get("origin") or [(_sr["region"][0] + _sr["region"][1]) / 2.0,
                                            _sr["region"][2]],
            "scale_x": _sr.get("scale_x") or 20.0,
        }
        if not dxf_path or not Path(dxf_path).exists():
            continue
        bbox = tuple(float(v) for v in side_region["region"])
        x_lo, x_hi, y_lo, y_hi = bbox
        # 3) 水平线收集（≥40u=800mm，|dy|<1.2u）
        try:
            segs = [s for s in collect_segments(dxf_path, bbox)
                    if seg_len(s) >= 3 and abs(s[3] - s[1]) < 1.2
                    and abs(s[2] - s[0]) >= 40.0]
        except Exception:
            continue
        if not segs:
            continue
        # 4) 双线合并：同 y（±4u）近平行对 → 中心
        merged: List[Tuple[float, float, float]] = []  # (yc, x1, x2)
        used = [False] * len(segs)
        for i, si in enumerate(segs):
            if used[i]:
                continue
            yc, a1, a2 = (si[1] + si[3]) / 2, min(si[0], si[2]), max(si[0], si[2])
            for j in range(i + 1, len(segs)):
                if used[j]:
                    continue
                sj = segs[j]
                yj = (sj[1] + sj[3]) / 2
                if abs(yj - yc) <= 4.0:
                    b1, b2 = min(sj[0], sj[2]), max(sj[0], sj[2])
                    ov = min(a2, b2) - max(a1, b1)
                    if ov >= 0.8 * min(a2 - a1, b2 - b1):
                        used[i] = used[j] = True
                        yc = (yc + yj) / 2
                        a1, a2 = min(a1, b1), max(a2, b2)
                        break
            merged.append((yc, a1, a2))
        if not merged:
            continue
        # 5) y→z 映射：优先专用 z_anchors（内容锚定，体段侧立面实测
        # region 边界含杂线、beat 链漂移 ±300mm）；缺省回落 beat 链。
        _anchors = _sr.get("z_anchors") or []
        if len(_anchors) >= 2:
            pairs = sorted((float(a[0]), float(a[1])) for a in _anchors)
        else:
            try:
                from .tower_dxf import dimension_beat_anchors as _dba
                import ezdxf as _ez
                _doc = _ez.readfile(dxf_path)
                _bcfg = (spec.get("dimension_beat_anchor") or {}).get(stem) or {}
                ba = _dba(
                    _doc.modelspace(), side_region,
                    float(_bcfg.get("z_base_mm", 0) or 0),
                    beat_min_mm=float(_bcfg.get("beat_min_mm", 350) or 350),
                    beat_max_mm=float(_bcfg.get("beat_max_mm", 800) or 800),
                    mode=str(_bcfg.get("mode", "beats")),
                    z_span_mm=tuple(_bcfg.get("z_span_mm") or ())
                    if _bcfg.get("z_span_mm") else None,
                )
            except Exception:
                ba = None
            if not ba or not ba.get("y_draw") or len(ba.get("z") or []) < 2:
                continue
            pairs = sorted(zip((float(v) for v in ba["y_draw"]),
                               (float(v) for v in ba["z"])))

        def _z_of(u_y: float) -> Optional[float]:
            if u_y <= pairs[0][0]:
                (y0, z0), (y1, z1) = pairs[0], pairs[1]
            elif u_y >= pairs[-1][0]:
                (y0, z0), (y1, z1) = pairs[-2], pairs[-1]
            else:
                (y0, z0), (y1, z1) = pairs[0], pairs[1]
                for i in range(len(pairs) - 1):
                    if pairs[i][0] <= u_y <= pairs[i + 1][0]:
                        (y0, z0), (y1, z1) = pairs[i], pairs[i + 1]
                        break
            if y1 == y0:
                return z0
            return z0 + (u_y - y0) / (y1 - y0) * (z1 - z0)

        levels = [float(v) for v in (_ce.get("beam_marker_levels_mm") or [])]
        # 6) 生成 y_member 读取
        for yc, a1, a2 in merged:
            z_raw = _z_of(yc)
            if z_raw is None:
                continue
            if levels:
                z_snap = min(levels, key=lambda v: abs(v - z_raw))
                if abs(z_snap - z_raw) > 300.0:
                    continue  # 非层位 → 标注线，弃
            else:
                z_snap = round(z_raw)
            try:
                hw = float(hw_fit(z_snap))
            except Exception:
                continue
            if hw < 100 or hw > 5000:
                continue
            # 深度判定：图纸 x 轴（region 内）→ 深度 mm（相对 origin x）
            ox = float(side_region["origin"][0])
            sc = float(side_region.get("scale_x") or 20.0)
            d1, d2 = (a1 - ox) * sc, (a2 - ox) * sc
            lo_d, hi_d = min(d1, d2), max(d1, d2)
            span = hi_d - lo_d
            if abs(span - 2 * hw) <= max(400.0, 0.25 * hw):
                # 全跨画线按 GT 拓扑拆两根半跨（层位横杆以半跨构件为主）
                _spans_add = [(-hw, 0.0), (0.0, hw)]
                for _sy1, _sy2 in _spans_add:
                    _key = (round(_sy1), round(_sy2), round(z_snap))
                    if any(
                        (round(float(r["from"][1])), round(float(r["to"][1])),
                         round(float(r["from"][2]))) == _key
                        for r in reads if isinstance(r, dict) and r.get("from")
                    ):
                        continue
                    reads.append({
                        "from": [round(-hw, 2), round(float(_sy1), 2), round(float(z_snap), 2)],
                        "to": [round(-hw, 2), round(float(_sy2), 2), round(float(z_snap), 2)],
                        "x_source": "face_plane", "z_snapped": True, "bar_id": None,
                        "section": None, "source_file": stem, "handle": None,
                        "geometry_origin": "side_horiz_synth",
                        "geometry_class": "recognized",
                        "source_extractor": "centerline_extract",
                        "confidence": 0.5, "reference": "side_horiz_synth",
                    })
                    added += 1
                continue
            elif abs(span - hw) <= max(350.0, 0.25 * hw) and abs(lo_d) <= max(400.0, 0.3 * hw):
                y1r, y2r = 0, hw            # 半跨（中心→+hw）
            elif abs(span - hw) <= max(350.0, 0.25 * hw) and abs(hi_d) <= max(400.0, 0.3 * hw):
                y1r, y2r = -hw, 0           # 半跨（-hw→中心）
            else:
                continue  # 跨度不合半宽拓扑 → 非横杆线，弃
            # 去重（同 (y1,y2,z) 已有读取则跳过）
            key = (round(y1r), round(y2r), round(z_snap))
            if any(
                (round(float(r["from"][1])), round(float(r["to"][1])),
                 round(float(r["from"][2]))) == key
                for r in reads if isinstance(r, dict) and r.get("from")
            ):
                continue
            reads.append({
                "from": [round(-hw, 2), round(float(y1r), 2), round(float(z_snap), 2)],
                "to": [round(-hw, 2), round(float(y2r), 2), round(float(z_snap), 2)],
                "x_source": "face_plane",
                "z_snapped": True,
                "bar_id": None,
                "section": None,
                "source_file": stem,
                "handle": None,
                "geometry_origin": "side_horiz_synth",
                "geometry_class": "recognized",
                "source_extractor": "centerline_extract",
                "confidence": 0.5,
                "reference": "side_horiz_synth",
            })
            added += 1
    if added:
        df.properties["side_reads_n"] = len(reads)
    return added


def apply_side_reads(model: "EngineeringModel") -> int:
    """把 merge_view_bars 冻结的 side_reads 注入为全新 3D 组件。

    在 expand_4_face_symmetry_model 之后调用（管线末端、dedup 之前）：
    每条 side_read 生成 face='l' 直读杆（geometry_origin='side_direct'，
    x 为 z_pair 解算值或 -hw(z) 面平面）+ face='r' 镜像孪生
    （side_mirror，reconstructed 层）。节点按坐标去重（容差 1mm），
    全新 id 前缀 sidegen__，与既有组件零共享态。
    """
    df = model.components.get("drawing_file")
    if df is None:
        return 0
    reads = df.properties.get("side_reads") or []
    if not reads:
        return 0
    from ..model import Component

    def _mknode_key(p):
        return (round(float(p[0]), 1), round(float(p[1]), 1), round(float(p[2]), 1))

    node_map: Dict[Tuple[float, float, float], str] = {}
    made = 0
    for i, r in enumerate(reads):
        try:
            p1, p2 = list(r["from"]), list(r["to"])
            x1, y1, z1 = float(p1[0]), float(p1[1]), float(p1[2])
            x2, y2, z2 = float(p2[0]), float(p2[1]), float(p2[2])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        # 同一物理杆两个 3D 形态：l 面直读（x 原值/面平面）与 r 面镜像
        for face, m, origin, cls in (
            ("l", 1.0, "side_direct", "recognized"),
            ("r", -1.0, "side_mirror", "reconstructed"),
        ):
            # 镜像：x 翻转（直读面 l 的 x 已含符号；镜像面 = x 取反）
            qx1, qx2 = x1 * m if face == "r" else x1, x2 * m if face == "r" else x2
            nids = []
            for j, (qx, qy, qz) in enumerate(((qx1, y1, z1), (qx2, y2, z2))):
                key = _mknode_key((qx, qy, qz))
                nid = node_map.get(key)
                if nid is None:
                    nid = f"sidegen__n{len(node_map):05d}"
                    node_map[key] = nid
                    model.add_component(Component(
                        id=nid, name=f"[sidegen] n{len(node_map)}",
                        kind="tower_node",
                        source=None,
                        properties={
                            "x": round(qx, 2), "y": round(qy, 2), "z": round(qz, 2),
                            "node_id": nid,
                            "view_type": "side",
                            "face": face,
                            "solve_status": "solved",
                            "solve_method": "side_read_inject",
                            "axis_origin": {"x": "side_read", "y": "side_view",
                                            "z": "side_view"},
                        },
                        tags=["side_read"],
                    ))
                nids.append(nid)
            if nids[0] == nids[1]:
                continue
            bid = r.get("bar_id")
            if not bid or str(bid).startswith("UNLABELED"):
                bid = f"UNLABELED_SIDE{i:04d}"
            model.add_component(Component(
                id=f"sidegen__b{i:04d}_{face}",
                name=f"[sidegen] {face} bar {i}",
                kind="tower_bar",
                source=None,
                properties={
                    "bar_id": bid,
                    "section": r.get("section"),
                    "from_node": nids[0],
                    "to_node": nids[1],
                    "view_type": "side",
                    "face": face,
                    "geometry_origin": origin,
                    "geometry_class": cls,
                    "source_extractor": r.get("source_extractor") or "centerline_extract",
                    "source_file": r.get("source_file"),
                    "handle": r.get("handle"),
                    "side_promoted": True,
                    "x_source": r.get("x_source"),
                    "confidence": r.get("confidence") or 0.5,
                    "length_mm_3d": round(
                        ((qx1 - qx2) ** 2 + (y1 - y2) ** 2 + (z1 - z2) ** 2) ** 0.5, 2),
                },
                tags=["side_read"],
            ))
            made += 1
    df.properties["side_reads_applied"] = made
    return made
