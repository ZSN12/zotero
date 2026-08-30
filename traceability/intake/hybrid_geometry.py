"""Hybrid Agent 几何层：像素↔图纸坐标变换 + MLLM/Hough 杆件注入模型。

从 hybrid_dxf_agent.py 拆分出的纯几何职责（P1 模块拆分）：
  * 预览图像素 ↔ DXF 图纸坐标（与杆件 node x/y 同系）
  * overlay view_regions 图纸区域 → 像素 bbox
  * MLLM/Hough 检测出的像素杆件 → 图纸坐标杆件
  * 图纸绝对坐标 → 视图局部坐标（复刻 ezdxf 的 region_scale_xy + z_flip 语义）
  * MLLM/Hough 杆件注入 EngineeringModel（含 view_x/view_y 与空间哈希去重）

这些函数无状态、不依赖 hybrid_dxf_agent 的其他函数，可安全独立复用/测试。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel, SourceRef, SourceType


def merge_parallel_double_lines(lines: List[Tuple[float, float, float, float]], dist_tol: float = 6.0) -> List[Tuple[float, float, float, float]]:
    """将平行双线（角钢两条轮廓线）合并为中心线。"""
    def _ang(p1, p2): return math.atan2(p2[1] - p1[1], p2[0] - p1[0])
    def _len(p1, p2): return math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    def _dist(pt, p1, p2):
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        l = math.hypot(dx, dy)
        if l <= 1e-6: return math.hypot(pt[0] - p1[0], pt[1] - p1[1])
        return abs(dy * pt[0] - dx * pt[1] + p2[0] * p1[1] - p2[1] * p1[0]) / l

    used = [False] * len(lines)
    merged: List[Tuple[float, float, float, float]] = []

    for i in range(len(lines)):
        if used[i]: continue
        l1 = lines[i]
        p1a, p1b = (l1[0], l1[1]), (l1[2], l1[3])
        len1 = _len(p1a, p1b)
        ang1 = _ang(p1a, p1b)

        best_j, best_dist = None, dist_tol
        for j in range(i + 1, len(lines)):
            if used[j]: continue
            l2 = lines[j]
            p2a, p2b = (l2[0], l2[1]), (l2[2], l2[3])
            len2 = _len(p2a, p2b)
            ang2 = _ang(p2a, p2b)

            da = abs(ang1 - ang2)
            if da > math.pi / 2: da = abs(da - math.pi)
            if da > math.radians(5.0): continue

            d1 = _dist(p2a, p1a, p1b)
            d2 = _dist(p2b, p1a, p1b)
            perp_dist = (d1 + d2) / 2.0
            if 0.5 <= perp_dist <= dist_tol and abs(len1 - len2) <= max(20.0, len1 * 0.4):
                if perp_dist < best_dist:
                    best_dist = perp_dist
                    best_j = j

        if best_j is not None:
            used[best_j] = True
            l2 = lines[best_j]
            p2a, p2b = (l2[0], l2[1]), (l2[2], l2[3])
            if _len(p1a, p2a) > _len(p1a, p2b): p2a, p2b = p2b, p2a
            c_start = ((p1a[0] + p2a[0]) / 2.0, (p1a[1] + p2a[1]) / 2.0)
            c_end = ((p1b[0] + p2b[0]) / 2.0, (p1b[1] + p2b[1]) / 2.0)
            merged.append((c_start[0], c_start[1], c_end[0], c_end[1]))
        else:
            merged.append(l1)
    return merged


def px_to_drawing_xy(px: float, py: float, mapping: Dict[str, Any]) -> Tuple[float, float]:
    """预览图像素坐标 → DXF 图纸坐标（与杆件 node x/y 同系）。"""
    xmin, xmax = mapping["xlim"]
    ymin, ymax = mapping["ylim"]
    w, h = float(mapping["width"]), float(mapping["height"])
    if w <= 0 or h <= 0:
        return px, py
    x = xmin + (px / w) * (xmax - xmin)
    y = ymax - (py / h) * (ymax - ymin)
    return x, y


def drawing_xy_to_px(x: float, y: float, mapping: Dict[str, Any]) -> Tuple[float, float]:
    """DXF 图纸坐标 → 预览图像素（供调试/可视化）。"""
    xmin, xmax = mapping["xlim"]
    ymin, ymax = mapping["ylim"]
    w, h = float(mapping["width"]), float(mapping["height"])
    if xmax == xmin or ymax == ymin:
        return 0.0, 0.0
    px = (x - xmin) / (xmax - xmin) * w
    py = (ymax - y) / (ymax - ymin) * h
    return px, py


def region_drawing_bbox(region: List[float]) -> Tuple[float, float, float, float]:
    """overlay region [x1,x2,y1,y2] → (xmin, xmax, ymin, ymax)。"""
    x1, x2, y1, y2 = [float(v) for v in region[:4]]
    return min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)


def drawing_region_to_pixel_bbox(
    region: List[float],
    mapping: Dict[str, Any],
    *,
    pad_px: int = 8,
) -> List[int]:
    """把 overlay 图纸区域转为预览图上的像素 bbox [x0,y0,x1,y1]。"""
    xmin, xmax, ymin, ymax = region_drawing_bbox(region)
    corners = [
        drawing_xy_to_px(xmin, ymin, mapping),
        drawing_xy_to_px(xmax, ymin, mapping),
        drawing_xy_to_px(xmin, ymax, mapping),
        drawing_xy_to_px(xmax, ymax, mapping),
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    w, h = int(mapping["width"]), int(mapping["height"])
    x0 = max(0, int(min(xs)) - pad_px)
    y0 = max(0, int(min(ys)) - pad_px)
    x1 = min(w, int(max(xs)) + pad_px)
    y1 = min(h, int(max(ys)) + pad_px)
    if x1 <= x0 or y1 <= y0:
        return [0, 0, w, h]
    return [x0, y0, x1, y1]


def bars_px_to_drawing(
    bars_px: List[Dict[str, Any]],
    mapping: Dict[str, Any],
    view_type: str,
) -> List[Dict[str, Any]]:
    """预览图像素杆件 → 图纸坐标 mm。"""
    out: List[Dict[str, Any]] = []
    for i, bar in enumerate(bars_px, start=1):
        x1, y1 = px_to_drawing_xy(float(bar["x1"]), float(bar["y1"]), mapping)
        x2, y2 = px_to_drawing_xy(float(bar["x2"]), float(bar["y2"]), mapping)
        out.append({
            "bar_uid": bar.get("bar_uid") or f"mllm_{i:04d}",
            "component_id": None,
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "view_type": bar.get("view_type") or view_type,
            "geometry_origin": bar.get("geometry_origin") or "mllm_geom",
        })
    return out


def stitch_mllm_diagonals(
    bars: List[Dict[str, Any]],
    *,
    angle_tol_deg: float = 6.0,
    gap_tol_mm: float = 45.0,
    colinear_tol_mm: float = 4.0,
    min_diag_angle_deg: float = 20.0,
) -> Tuple[List[Dict[str, Any]], int]:
    """把 MLLM 检测出的碎片化斜材按「同向共线 + 端点相邻」拼成通长斜材。

    背景：MLLM 常把一根通长斜材在与其他斜材交叉处断成多段（碎片化），导致
    斜材中位长度远短于 GT（06 段 887mm vs GT 3601mm）。本函数在 MLLM 输出后
    做几何后处理拼接，不依赖 MLLM 重新检测。

    拼接条件（与 tower_dxf._merge_collinear_fragments 同源思想）：
        * 方向夹角 <= angle_tol_deg；
        * 点到对方所在直线垂直距离 <= colinear_tol_mm（共线）；
        * 端点沿方向投影间距 <= gap_tol_mm（相邻/相接/少量重叠）；
        * 只拼接斜材（与水平/竖直夹角 >= min_diag_angle_deg），避免误拼
          主腿（竖直）或水平杆。

    返回 (stitched_bars, merged_count)；未参与拼接的杆件原样保留。
    """
    import math

    def _dxdy(b):
        return float(b["x2"]) - float(b["x1"]), float(b["y2"]) - float(b["y1"])

    def _ang(b):
        dx, dy = _dxdy(b)
        return math.atan2(dy, dx)

    def _span(b):
        dx, dy = _dxdy(b)
        return math.hypot(dx, dy)

    def _is_diag(b):
        # 与水平轴的夹角：斜材应显著偏离水平/竖直
        dx, dy = _dxdy(b)
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return False
        ang = abs(math.degrees(math.atan2(abs(dy), abs(dx))))
        return min_diag_angle_deg <= ang <= (90.0 - min_diag_angle_deg)

    work = [dict(b) for b in bars]
    used = [False] * len(work)
    out: List[Dict[str, Any]] = []
    merged_count = 0
    ang_tol = math.radians(angle_tol_deg)

    for i in range(len(work)):
        if used[i]:
            continue
        b = work[i]
        if not _is_diag(b):
            out.append(b)
            used[i] = True
            continue
        chain = [b]
        used[i] = True
        grew = True
        while grew:
            grew = False
            base = chain[-1]
            ba = _ang(base)
            bx1, by1 = float(base["x1"]), float(base["y1"])
            bx2, by2 = float(base["x2"]), float(base["y2"])
            bl = math.hypot(bx2 - bx1, by2 - by1)
            if bl <= 0:
                break
            ux, uy = (bx2 - bx1) / bl, (by2 - by1) / bl
            best_j, best_dist = None, gap_tol_mm
            for j in range(len(work)):
                if used[j] or not _is_diag(work[j]):
                    continue
                cand = work[j]
                da = abs(_ang(cand) - ba)
                if da > ang_tol and abs(da - math.pi) > ang_tol:
                    continue
                cx, cy = float(cand["x1"]), float(cand["y1"])
                perp = abs((cx - bx1) * uy - (cy - by1) * ux)
                if perp > colinear_tol_mm:
                    continue
                # 候选两端沿主轴投影，取离当前端点最近的一端
                proj_cur = (bx2 - bx1) * ux + (by2 - by1) * uy
                proj_c1 = (float(cand["x1"]) - bx1) * ux + (float(cand["y1"]) - by1) * uy
                proj_c2 = (float(cand["x2"]) - bx1) * ux + (float(cand["y2"]) - by1) * uy
                gap = min(abs(proj_c1 - proj_cur), abs(proj_c2 - proj_cur))
                if gap < best_dist:
                    best_dist, best_j = gap, j
            if best_j is not None:
                chain.append(work[best_j])
                used[best_j] = True
                grew = True

        if len(chain) == 1:
            out.append(b)
            continue
        # 整条链的 span：所有端点沿主轴投影极值
        pts = []
        for s in chain:
            pts.append((float(s["x1"]), float(s["y1"])))
            pts.append((float(s["x2"]), float(s["y2"])))
        origin = (float(chain[0]["x1"]), float(chain[0]["y1"]))
        ax = float(chain[-1]["x2"]) - origin[0]
        ay = float(chain[-1]["y2"]) - origin[1]
        if math.hypot(ax, ay) <= 0:
            out.append(b)
            continue
        ux, uy = ax / math.hypot(ax, ay), ay / math.hypot(ax, ay)
        projs = [(p[0] - origin[0]) * ux + (p[1] - origin[1]) * uy for p in pts]
        t0, t1 = min(projs), max(projs)
        stitched = dict(b)
        stitched["x1"] = origin[0] + ux * t0
        stitched["y1"] = origin[1] + uy * t0
        stitched["x2"] = origin[0] + ux * t1
        stitched["y2"] = origin[1] + uy * t1
        stitched["stitched_fragments"] = len(chain)
        out.append(stitched)
        merged_count += len(chain) - 1
    return out, merged_count


def hough_bars_to_drawing(
    png_path: str,
    mapping: Dict[str, Any],
    view_type: str = "detail",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """A2 栅格回退：霍夫线检测，像素坐标转图纸坐标。"""
    from .tower_agent_pipeline import _detect_geometry

    bars_px, nodes_px, meta = _detect_geometry(png_path, filter_noise=True, use_preprocess=True)
    bars: List[Dict[str, Any]] = []
    for i, bar in enumerate(bars_px, start=1):
        x1, y1 = px_to_drawing_xy(float(bar["x1"]), float(bar["y1"]), mapping)
        x2, y2 = px_to_drawing_xy(float(bar["x2"]), float(bar["y2"]), mapping)
        bars.append({
            "bar_uid": f"hough_{i:04d}",
            "component_id": None,
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "view_type": view_type,
            "geometry_origin": "hough_fallback",
        })
    meta["nodes_px"] = len(nodes_px)
    return bars, meta


def drawing_xy_to_view_xy(
    region: Optional[Dict[str, Any]],
    x: float,
    y: float,
) -> Tuple[float, float]:
    """DXF 图纸绝对坐标 → 视图局部坐标（view_x, view_y），复刻 ezdxf 语义。

    ezdxf 路径（tower_dxf._region_local + region_scale_xy + z_flip）：
        local = (abs - origin)
        view_x = local.x * scale_x
        view_y = local.y * scale_y（z_flip 时取负）
    MLLM 几何检测得到的是整图像素坐标，经 px_to_drawing_xy 转成 DXF 图纸
    绝对坐标后，与 ezdxf 节点同系；这里再套用同一套 region 换算，确保
    merge_view_coordinates 能读到与 ezdxf 一致的 view_x/view_y。
    """
    if not region:
        return x, y
    from .tower_spec import region_scale_xy

    ox, oy = region.get("origin", (0.0, 0.0))
    sx, sy = region_scale_xy(region)
    vx = (float(x) - float(ox)) * sx
    vy = (float(y) - float(oy)) * sy
    if region.get("z_flip"):
        vy = -vy
    return vx, vy


def inject_mllm_bars_into_model(
    model: EngineeringModel,
    bars: List[Dict[str, Any]],
    *,
    view_type: str = "front",
    stem: Optional[str] = None,
    layer_map_path: Optional[str | Path] = None,
) -> int:
    """把 MLLM/Hough 图纸坐标杆件写入 EngineeringModel。

    与 ezdxf 节点对齐：除 x/y（图纸绝对坐标）外，还写入 view_x/view_y/
    drawing_view，使 merge_view_coordinates 的 front 单立面分支能按
    view_x→x、view_y→z 解算（含 z_offset/z_span_mm 分段堆叠）。
    """
    from .tower_spec import view_region

    added = 0
    node_idx = sum(1 for c in model.components.values() if c.kind == "tower_node")
    bar_idx = sum(1 for c in model.components.values() if c.kind == "tower_bar")

    # 按 view_type 缓存 region，避免每个节点重复查 overlay
    region_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def _region_for(vt: str) -> Optional[Dict[str, Any]]:
        if vt in region_cache:
            return region_cache[vt]
        r = None
        if stem:
            r = view_region(stem, vt, overlay=layer_map_path)
        region_cache[vt] = r
        return r

    # P2 性能 + 正确性：_ensure_node 空间哈希（坐标桶），桶键含 view_type，
    # 命中的候选再按真实欧氏距离复验（1mm 容差），避免跨视图/超距误合并。
    node_buckets: Dict[tuple, str] = {}
    # 桶内记录 (x, y) 供距离复验；桶键 (vt, round(x), round(y))。
    node_coords: Dict[str, Tuple[float, float]] = {}
    _SNAP_MM = 1.0  # 视为同节点的距离容差（mm）

    def _ensure_node(x: float, y: float, vt: str) -> str:
        nonlocal node_idx
        cx, cy = round(x), round(y)
        # 3×3 邻域桶（覆盖 1mm 容差内跨桶边界的坐标），按 view_type 隔离
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                hit = node_buckets.get((vt, cx + dx, cy + dy))
                if hit is not None:
                    # 真实距离复验：桶内 1 格最多 ~1.42mm 对角，须 ≤1mm 才算同节点
                    hx, hy = node_coords[hit]
                    if (x - hx) ** 2 + (y - hy) ** 2 <= _SNAP_MM * _SNAP_MM:
                        return hit
        node_idx += 1
        nid = f"node_M{node_idx:03d}"
        node_buckets[(vt, cx, cy)] = nid
        node_coords[nid] = (x, y)
        region = _region_for(vt)
        vx, vy = drawing_xy_to_view_xy(region, x, y)
        model.add_component(Component(
            id=nid,
            name=f"节点 {nid}",
            kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "mllm_geom", confidence=0.6),
            properties={
                "node_id": nid, "x": round(x, 2), "y": round(y, 2),
                "z": None,
                "solve_status": "partial",
                "view_type": vt,
                "view_x": round(vx, 2) if vx is not None else None,
                "view_y": round(vy, 2) if vy is not None else None,
                "drawing_view": stem,
                "geometry_origin": "mllm_inject",
            },
        ))
        return nid

    for bar in bars:
        bar_idx += 1
        vt = bar.get("view_type") or view_type
        fn = _ensure_node(float(bar["x1"]), float(bar["y1"]), vt)
        tn = _ensure_node(float(bar["x2"]), float(bar["y2"]), vt)
        cid = f"bar_M{bar_idx:04d}"
        bar_bid = bar.get("bar_id") or f"UNLABELED_{bar.get('bar_uid', cid)}"
        model.add_component(Component(
            id=cid,
            name=f"杆件 {bar.get('bar_uid', cid)}",
            kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "mllm_geom", confidence=0.6),
            properties={
                "from_node": fn, "to_node": tn,
                "view_type": vt,
                "geometry_origin": bar.get("geometry_origin") or "mllm_geom",
                "drawing_view": stem,
                "bar_id": bar_bid,
            },
        ))
        bar["component_id"] = cid
        added += 1
    return added


def remove_x_crossing_nodes(model: EngineeringModel, *, angle_tol_deg: float = 8.0) -> int:
    """阶段2.5：X 交叉默认不是节点——把「两条斜材单纯交叉」处的伪节点解耦。

    背景：MLLM/Hough 常在两根斜材交叉处各自断成两段，注入时 `_ensure_node`
    在交叉点合成一个度 4 节点，使本该「穿过彼此」的通长斜材被劈成两段、
    且多出一个不存在的结构节点。本函数识别这类节点：

        * 节点的入射杆件恰为 4 根；
        * 这 4 根能两两配成「共线对」（两对方向近似相反，即两条斜材各被
          交叉点分成两段，方向在同一直线上）；
        * 每对的两个远端点（非交叉点）连成一根通长斜材，穿过交叉点；
        * 删除交叉点节点与其上的 4 根碎杆，代之以 2 根通长斜材。

    返回解耦的交叉节点数。纯几何后处理，只改节点/杆件拓扑，不重算坐标。
    """
    import math as _math

    nodes = {cid: c for cid, c in model.components.items() if c.kind == "tower_node"}
    bars = {cid: c for cid, c in model.components.items() if c.kind == "tower_bar"}

    incident: Dict[str, List[str]] = {}
    for cid, b in bars.items():
        fn = b.properties.get("from_node")
        tn = b.properties.get("to_node")
        if fn in nodes:
            incident.setdefault(fn, []).append(cid)
        if tn in nodes:
            incident.setdefault(tn, []).append(cid)

    ang_tol = _math.radians(angle_tol_deg)
    uncoupled = 0

    for nid, bar_ids in list(incident.items()):
        if len(bar_ids) != 4:
            continue
        npos = (float(nodes[nid].properties.get("x") or 0.0),
                float(nodes[nid].properties.get("y") or 0.0))

        # 每根入射杆的远端（另一端）坐标 + 从节点指向远端的方向
        arms = []
        for cid in bar_ids:
            b = bars[cid]
            fn, tn = b.properties.get("from_node"), b.properties.get("to_node")
            if fn == nid:
                other = tn
            elif tn == nid:
                other = fn
            else:
                other = None
            if other is None or other not in nodes:
                arms = None
                break
            op = nodes[other].properties
            dx = float(op.get("x") or 0.0) - npos[0]
            dy = float(op.get("y") or 0.0) - npos[1]
            arms.append({"bar": cid, "other": other, "dx": dx, "dy": dy})
        if arms is None or len(arms) != 4:
            continue

        # 两两配对：方向相反的臂（夹角接近 pi）构成一条通长斜材
        used = [False] * 4
        pairs = []
        for i in range(4):
            if used[i]:
                continue
            ai = arms[i]
            bi = None
            for j in range(i + 1, 4):
                if used[j]:
                    continue
                aj = arms[j]
                # 方向相反：点积为负且接近 -|a||b|（夹角 ~pi）
                dot = ai["dx"] * aj["dx"] + ai["dy"] * aj["dy"]
                na = _math.hypot(ai["dx"], ai["dy"])
                nb = _math.hypot(aj["dx"], aj["dy"])
                if na <= 1e-6 or nb <= 1e-6:
                    continue
                cos_ang = dot / (na * nb)
                if cos_ang <= -_math.cos(ang_tol):
                    bi = j
                    break
            if bi is None:
                pairs = None
                break
            used[i] = used[bi] = True
            pairs.append((ai, arms[bi]))

        if pairs is None or len(pairs) != 2:
            continue

        # 解耦：每对生成一根通长斜材（远端点 ↔ 远端点），删除 4 根碎杆 + 交叉节点
        pair_idx = 0
        for ai, aj in pairs:
            pair_idx += 1
            op_i = nodes[ai["other"]].properties
            op_j = nodes[aj["other"]].properties
            new_bar_id = f"bar_X{uncoupled + 1:04d}_{pair_idx}"
            model.add_component(Component(
                id=new_bar_id,
                name=f"通长斜材(穿过交叉节点 {nid})",
                kind="tower_bar",
                source=SourceRef(SourceType.DRAWING, "x_crossing_uncouple", confidence=0.6),
                properties={
                    "from_node": ai["other"], "to_node": aj["other"],
                    "view_type": bars[ai["bar"]].properties.get("view_type"),
                    "geometry_origin": "x_crossing_uncouple",
                    "bar_id": bars[ai["bar"]].properties.get("bar_id")
                              or f"UNLABELED_{new_bar_id}",
                    "uncoupled_from": [ai["bar"], aj["bar"]],
                },
            ))
        for cid in bar_ids:
            model.components.pop(cid, None)
        model.components.pop(nid, None)
        uncoupled += 1

    return uncoupled
