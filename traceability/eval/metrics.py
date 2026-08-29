"""评测核心（M0/M1）：可重复、四套指标、derived 排除、tolerance sweep。

官网验收标准要求评测可信：
    * 一对一 Hungarian 最优匹配（非贪心）
    * 代价含双端点距离、角度、长度比、线段重叠
    * tolerance sweep（50/100/200/500mm）而非单一容差
    * A2 几何 / A1 标签 / A3 关联 / M3 物理 3D 四套指标不可混算
    * canonical / derived / diaphragm / 整高合成角腿 不得进入 recognition P/R

本模块提供：
    * segment_cost(a, b)         —— 两线段的综合代价（端点/角度/长度/重叠）
    * hungarian_match(gt, model, max_cost) —— 一对一最优匹配
    * eval_segment_pr(gt, model, tols)      —— tolerance sweep PR 曲线
    * 四套指标各自独立，共享同一匹配内核，互不污染。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 2D 线段：(x1, y1, x2, y2, ...metadata)
Seg2D = Tuple[float, float, float, float]
# 3D 线段端点：((x1,y1,z1), (x2,y2,z2), ...metadata)
Seg3D = Tuple[Tuple[float, float, float], Tuple[float, float, float]]

# --------------------------------------------------------------------------- #
# 语义冻结（阶段0）：构件四类语义
# --------------------------------------------------------------------------- #
# recognized    —— 直接从图纸识别出的杆件（识别真值，进 recognition P/R）
# reconstructed —— 由识别结果经确定性求解重建的杆件（进 reconstructed P/R，独立计）
# derived       —— 派生展示几何（镜像面/corner_leg/diaphragm），不进 physical P/R
# canonical     —— GT 权威塔，仅评测基准，不进生产建模

DERIVED_ORIGINS = frozenset({"derived_4face"})
DERIVED_EVIDENCE_STATUS = frozenset({"derived", "mirrored"})
# 整高合成角腿 / 自动 diaphragm 的显式标记
DERIVED_COMPONENT_FLAGS = ("corner_leg", "diaphragm", "auto_diaphragm")


def is_derived_bar(properties: Dict[str, Any]) -> bool:
    """判断一根杆件是否为派生展示几何（不计入 physical Precision/Recall）。

    判定依据（任一命中即 derived）：
        * geometry_origin in DERIVED_ORIGINS（derived_4face）
        * evidence_status in {"derived", "mirrored"}
        * 显式 corner_leg / diaphragm / auto_diaphragm 标记
    """
    if properties.get("geometry_origin") in DERIVED_ORIGINS:
        return True
    if properties.get("evidence_status") in DERIVED_EVIDENCE_STATUS:
        return True
    if properties.get("corner_leg") or properties.get("diaphragm") or properties.get("auto_diaphragm"):
        return True
    return False


def is_physical_bar(properties: Dict[str, Any]) -> bool:
    """物理杆件（进 physical P/R）：非 derived、非 canonical。"""
    if is_derived_bar(properties):
        return False
    if properties.get("gt_aligned") or properties.get("canonical"):
        return False
    return True


# --------------------------------------------------------------------------- #
# 线段几何代价
# --------------------------------------------------------------------------- #

def _seg_len_2d(a: Seg2D) -> float:
    return math.hypot(a[2] - a[0], a[3] - a[1])


def _endpoint_dist_2d(a: Seg2D, b: Seg2D) -> float:
    """双端点距离（正反顺序都试，取最小）：衡量两端点是否同时对齐。"""
    a1 = (a[0], a[1]); a2 = (a[2], a[3])
    b1 = (b[0], b[1]); b2 = (b[2], b[3])
    same = math.hypot(a1[0] - b1[0], a1[1] - b1[1]) + math.hypot(a2[0] - b2[0], a2[1] - b2[1])
    rev = math.hypot(a1[0] - b2[0], a1[1] - b2[1]) + math.hypot(a2[0] - b1[0], a2[1] - b1[1])
    return min(same, rev)


def _angle_diff_2d(a: Seg2D, b: Seg2D) -> float:
    da = math.atan2(a[3] - a[1], a[2] - a[0])
    db = math.atan2(b[3] - b[1], b[2] - b[0])
    d = abs(da - db)
    return min(d, math.pi - d)


def _length_ratio(a: Seg2D, b: Seg2D) -> float:
    la = _seg_len_2d(a); lb = _seg_len_2d(b)
    if la <= 0 or lb <= 0:
        return 1.0
    r = la / lb
    return r if r >= 1 else 1.0 / r  # 归一化到 >= 1


def _overlap_ratio(a: Seg2D, b: Seg2D) -> float:
    """共线重叠比例（投影到 a 方向上，重叠长度 / min(la, lb)）。

    用于识别「同一杆被碎片化 / 部分检测」的重复，以及「长杆 vs 短碎片」。
    值域 [0, 1]，1 表示完全重叠。
    """
    la = _seg_len_2d(a)
    if la <= 0:
        return 0.0
    # a 的单位方向
    ux = (a[2] - a[0]) / la
    uy = (a[3] - a[1]) / la
    # b 的端点投影到 a 的起点
    pa = 0.0
    pb = la
    t0 = (b[0] - a[0]) * ux + (b[1] - a[1]) * uy
    t1 = (b[2] - a[0]) * ux + (b[3] - a[1]) * uy
    lo = min(t0, t1); hi = max(t0, t1)
    # 与 [0, la] 的重叠区间
    overlap = max(0.0, min(hi, la) - max(lo, 0.0))
    lb = _seg_len_2d(b)
    denom = min(la, lb) if lb > 0 else la
    return overlap / denom if denom > 0 else 0.0


def segment_cost(a: Seg2D, b: Seg2D) -> float:
    """综合代价（越小越相似），单位 mm。

    组合：双端点距离（主项）+ 角度惩罚 + 长度比惩罚 + 重叠奖励。
    代价无穷大表示不可能匹配（角度差 > 45° 或长度比 > 3）。
    """
    end_dist = _endpoint_dist_2d(a, b)
    ang = _angle_diff_2d(a, b)
    lr = _length_ratio(a, b)
    if ang > math.radians(45.0) or lr > 3.0:
        return float("inf")
    # 重叠奖励：重叠越多，双端点距离的权重越弱（碎片 vs 长杆）
    ov = _overlap_ratio(a, b)
    cost = end_dist
    # 角度惩罚：角度差每 10° 加 10% 端点距离
    cost += end_dist * (ang / math.radians(10.0)) * 0.1
    # 长度比惩罚
    cost += end_dist * (lr - 1.0) * 0.1
    # 重叠奖励：降低碎片化造成的端点距离虚高
    cost *= (1.0 - 0.3 * ov)
    return cost


def _seg_len_3d(a: Seg3D) -> float:
    p, q = a
    return math.sqrt(sum((q[i] - p[i]) ** 2 for i in range(3)))


def _endpoint_dist_3d(a: Seg3D, b: Seg3D) -> float:
    a1, a2 = a; b1, b2 = b
    def d(p, q):
        return math.sqrt(sum((p[i] - q[i]) ** 2 for i in range(3)))
    return min(d(a1, b1) + d(a2, b2), d(a1, b2) + d(a2, b1))


def segment_cost_3d(a: Seg3D, b: Seg3D) -> float:
    """3D 线段综合代价（同 2D 语义）。"""
    end_dist = _endpoint_dist_3d(a, b)
    # 角度
    def unit(p, q):
        v = [q[i] - p[i] for i in range(3)]
        L = math.sqrt(sum(x * x for x in v))
        return tuple(x / L for x in v) if L > 1e-9 else (0.0, 0.0, 0.0)
    ua = unit(a[0], a[1]); ub = unit(b[0], b[1])
    dot = max(-1.0, min(1.0, abs(sum(ua[i] * ub[i] for i in range(3)))))
    ang = math.acos(dot)
    la = _seg_len_3d(a); lb = _seg_len_3d(b)
    lr = (la / lb if la >= lb else lb / la) if (la > 0 and lb > 0) else 1.0
    if ang > math.radians(45.0) or lr > 3.0:
        return float("inf")
    cost = end_dist
    cost += end_dist * (ang / math.radians(10.0)) * 0.1
    cost += end_dist * (lr - 1.0) * 0.1
    return cost


# --------------------------------------------------------------------------- #
# Hungarian 一对一最优匹配
# --------------------------------------------------------------------------- #

def hungarian_match(
    gt: Sequence[Any],
    model: Sequence[Any],
    cost_fn,
    max_cost: float,
) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    """一对一最优匹配（scipy.linear_sum_assignment）。

    返回 (matched_pairs, unmatched_gt_idx, unmatched_model_idx)。
    max_cost 以上的匹配被丢弃（视为不匹配）。
    """
    n_gt, n_m = len(gt), len(model)
    if n_gt == 0 or n_m == 0:
        return [], list(range(n_gt)), list(range(n_m))

    import numpy as np

    cost = np.full((n_gt, n_m), np.inf)
    for i, g in enumerate(gt):
        for j, m in enumerate(model):
            c = cost_fn(g, m)
            if c < max_cost:
                cost[i, j] = c

    from scipy.optimize import linear_sum_assignment
    row_ind, col_ind = linear_sum_assignment(cost)

    matched = []
    used_m = set()
    for i, j in zip(row_ind, col_ind):
        if cost[i, j] < max_cost and not math.isinf(cost[i, j]):
            matched.append((int(i), int(j)))
            used_m.add(int(j))

    unmatched_gt = [i for i in range(n_gt) if i not in {p[0] for p in matched}]
    unmatched_m = [j for j in range(n_m) if j not in used_m]
    return matched, unmatched_gt, unmatched_m


# --------------------------------------------------------------------------- #
# tolerance sweep PR
# --------------------------------------------------------------------------- #

DEFAULT_TOLS = (50.0, 100.0, 200.0, 500.0)


def eval_segment_pr(
    gt: Sequence[Any],
    model: Sequence[Any],
    cost_fn,
    tols: Sequence[float] = DEFAULT_TOLS,
) -> Dict[str, Any]:
    """tolerance sweep：对每个容差算 Precision/Recall，输出 PR 曲线。

    返回 {
        "n_gt": int, "n_model": int,
        "sweep": [{tol, tp, fp, fn, precision, recall}, ...],
        "matched_at_default": [...],  # 默认容差（最后一个）的匹配对
    }
    """
    sweep = []
    matched_at_default = []
    for tol in tols:
        matched, un_gt, un_m = hungarian_match(gt, model, cost_fn, max_cost=tol)
        tp = len(matched)
        fp = len(un_m)
        fn = len(un_gt)
        n_model = len(model)
        n_gt = len(gt)
        precision = tp / n_model if n_model else 0.0
        recall = tp / n_gt if n_gt else 0.0
        sweep.append({
            "tol": tol, "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4), "recall": round(recall, 4),
        })
        if tol == tols[-1]:
            matched_at_default = matched
    return {
        "n_gt": len(gt),
        "n_model": len(model),
        "sweep": sweep,
        "matched_at_default": matched_at_default,
    }


# --------------------------------------------------------------------------- #
# 四套指标：各自独立提取候选集，再走同一匹配内核
# --------------------------------------------------------------------------- #

def bars_from_model_2d(
    model: Dict[str, Any],
    *,
    view: Optional[str] = None,
    exclude_derived: bool = True,
) -> List[Tuple[Seg2D, Dict[str, Any]]]:
    """从 model.json 提取 2D 杆件（排除 derived 构件）。

    返回 [( (x1,y1,x2,y2), properties ), ...]，仅物理杆件（exclude_derived=True）。
    """
    comps = model.get("components", {})
    nodes = {cid: c for cid, c in comps.items() if c.get("kind") == "tower_node"}
    out: List[Tuple[Seg2D, Dict[str, Any]]] = []
    dedup: set = set()
    for cid, c in comps.items():
        if c.get("kind") != "tower_bar":
            continue
        p = c.get("properties", {})
        if exclude_derived and is_derived_bar(p):
            continue
        vt = p.get("view_type")
        if view is not None and vt is not None and vt != view:
            continue
        f, t = p.get("from_node"), p.get("to_node")
        nf = nodes.get(f) if f else None
        nt = nodes.get(t) if t else None
        if nf is None or nt is None:
            continue
        pf, pt = nf.get("properties", {}), nt.get("properties", {})
        if pf.get("view_x") is not None and pt.get("view_x") is not None:
            x1, y1 = pf["view_x"], pf.get("view_y", pf.get("y"))
            x2, y2 = pt["view_x"], pt.get("view_y", pt.get("y"))
        elif pf.get("z") is not None and pt.get("z") is not None:
            x1, y1 = pf.get("x"), pf.get("z")
            x2, y2 = pt.get("x"), pt.get("z")
        else:
            x1, y1 = pf.get("x"), pf.get("y")
            x2, y2 = pt.get("x"), pt.get("y")
        if None in (x1, y1, x2, y2):
            continue
        seg = (float(x1), float(y1), float(x2), float(y2))
        if (seg[0], seg[1]) > (seg[2], seg[3]):
            seg = (seg[2], seg[3], seg[0], seg[1])
        key = (round(seg[0]), round(seg[1]), round(seg[2]), round(seg[3]))
        if key in dedup:
            continue
        dedup.add(key)
        out.append((seg, p))
    return out


def bars_from_model_3d(
    model: Dict[str, Any],
    *,
    exclude_derived: bool = True,
) -> List[Tuple[Seg3D, Dict[str, Any]]]:
    """从 model.json 提取 3D 物理杆件（排除 derived 构件）。"""
    comps = model.get("components", {})
    nodes: Dict[str, Tuple[float, float, float]] = {}
    for cid, c in comps.items():
        if c.get("kind") == "tower_node":
            p = c.get("properties", {})
            if all(p.get(a) is not None for a in ("x", "y", "z")):
                nodes[cid] = (float(p["x"]), float(p["y"]), float(p["z"]))
    out: List[Tuple[Seg3D, Dict[str, Any]]] = []
    for cid, c in comps.items():
        if c.get("kind") != "tower_bar":
            continue
        p = c.get("properties", {})
        if exclude_derived and is_derived_bar(p):
            continue
        f, t = p.get("from_node"), p.get("to_node")
        if f in nodes and t in nodes:
            out.append(((nodes[f], nodes[t]), p))
    return out


def gt_bars_2d(gt: Dict[str, Any], view: str) -> List[Tuple[Seg2D, str, str]]:
    """GT 3D 杆件投影到 2D（去重），返回 [(seg, bar_id, section)]。"""
    nodes = gt["nodes"]
    seen: set = set()
    out = []
    for b in gt["bars"]:
        f = nodes.get(b["from"]); t = nodes.get(b["to"])
        if f is None or t is None:
            continue
        if view == "front":
            x1, z1 = f[0], f[2]; x2, z2 = t[0], t[2]
        elif view == "side":
            x1, z1 = f[1], f[2]; x2, z2 = t[1], t[2]
        else:
            raise ValueError(f"未知视图 {view}")
        if (x1, z1) > (x2, z2):
            x1, z1, x2, z2 = x2, z2, x1, z1
        key = (round(x1), round(z1), round(x2), round(z2))
        if key in seen:
            continue
        seen.add(key)
        out.append(((x1, z1, x2, z2), b["id"], b.get("section", "")))
    return out


def gt_bars_3d(gt: Dict[str, Any]) -> List[Tuple[Seg3D, str, str]]:
    """GT 3D 杆件。"""
    nodes = gt["nodes"]
    out = []
    for b in gt["bars"]:
        f = nodes.get(b["from"]); t = nodes.get(b["to"])
        if f is None or t is None:
            continue
        out.append(((tuple(f), tuple(t)), b["id"], b.get("section", "")))
    return out


# --------------------------------------------------------------------------- #
# 四套指标（独立评测，不混算）
# --------------------------------------------------------------------------- #

def eval_a2_geometry_2d(
    gt: Dict[str, Any],
    model: Dict[str, Any],
    view: str = "front",
    tols: Sequence[float] = DEFAULT_TOLS,
) -> Dict[str, Any]:
    """A2 几何检测（2D 投影）：GT 投影 vs 模型物理 2D 杆件。"""
    g = gt_bars_2d(gt, view)
    m = bars_from_model_2d(model, view=view, exclude_derived=True)
    gt_segs = [s for s, _, _ in g]
    model_segs = [s for s, _ in m]
    result = eval_segment_pr(gt_segs, model_segs, segment_cost, tols)
    # 件号 Exact Match（匹配对中，A1 标签 + A3 关联的产物）
    exact = 0
    for gi, mj in result["matched_at_default"]:
        gid = g[gi][1]
        mid = m[mj][1].get("bar_id", "")
        if mid and not str(mid).startswith("UNLABELED") and str(gid) == str(mid):
            exact += 1
    result["label_exact_match"] = {
        "matched": len(result["matched_at_default"]),
        "exact": exact,
        "rate": round(exact / len(result["matched_at_default"]), 4) if result["matched_at_default"] else 0.0,
    }
    return result


def eval_m3_physical_3d(
    gt: Dict[str, Any],
    model: Dict[str, Any],
    tols: Sequence[float] = (200.0, 500.0, 800.0),
) -> Dict[str, Any]:
    """M3 物理 3D：GT 3D vs 模型物理 3D 杆件（排除 derived）。"""
    g = gt_bars_3d(gt)
    m = bars_from_model_3d(model, exclude_derived=True)
    gt_segs = [s for s, _, _ in g]
    model_segs = [s for s, _ in m]
    result = eval_segment_pr(gt_segs, model_segs, segment_cost_3d, tols)
    result["derived_excluded"] = True
    # 按杆件类型细分召回（leg/diagonal/horizontal）
    from collections import Counter
    matched_gt = {gi for gi, _ in result["matched_at_default"]}
    by_type = Counter(); by_missed = Counter()
    for gi, s in enumerate(gt_segs):
        t = _classify_3d(s)
        by_type[t] += 1
        if gi not in matched_gt:
            by_missed[t] += 1
    result["recall_by_type"] = {
        t: {
            "total": by_type.get(t, 0),
            "missed": by_missed.get(t, 0),
            "recall": round((by_type[t] - by_missed.get(t, 0)) / by_type[t], 4) if by_type.get(t, 0) else 0.0,
        }
        for t in ("leg", "diagonal", "horizontal", "degenerate")
    }
    return result


def _classify_3d(seg: Seg3D) -> str:
    p, q = seg
    L = math.sqrt(sum((q[i] - p[i]) ** 2 for i in range(3)))
    if L < 1e-6:
        return "degenerate"
    dz = abs(q[2] - p[2]) / L
    if dz > 0.85:
        return "leg"
    if dz < 0.3:
        return "horizontal"
    return "diagonal"
