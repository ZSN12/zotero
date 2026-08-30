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
# reconstructed —— 由识别结果经确定性求解/镜像展开重建的物理杆件（含 mirrored，
#                  进 physical P/R，不进 recognition P/R）
# derived       —— 派生展示几何（corner_leg/diaphragm/center 轴），不进任何 P/R
# canonical     —— GT 权威塔，仅评测基准，不进生产建模

DERIVED_ORIGINS = frozenset({"derived_4face"})
# derived（纯展示几何，不进任何 P/R）：corner_leg / diaphragm / center 轴
DERIVED_EVIDENCE_STATUS = frozenset({"derived"})
# mirrored（镜像派生面 B/L/R）：进 physical P/R，但不进 recognition P/R
MIRRORED_EVIDENCE_STATUS = frozenset({"mirrored"})
# 整高合成角腿 / 自动 diaphragm 的显式标记
DERIVED_COMPONENT_FLAGS = ("corner_leg", "diaphragm", "auto_diaphragm")


def is_derived_bar(properties: Dict[str, Any]) -> bool:
    """判断一根杆件是否为派生展示几何（不进任何 Precision/Recall）。

    判定依据（任一命中即 derived）：
        * evidence_status == "derived"（corner_leg/diaphragm/center 轴）
        * 显式 corner_leg / diaphragm / auto_diaphragm 标记
        * face in {"diaphragm", "center", "corner"}

    注意：mirrored（镜像面 B/L/R）不是 derived——它们是 4-face 展开的正常
    重建产物，进 physical P/R（但不进 recognition P/R，见 is_recognized_bar）。
    """
    if properties.get("evidence_status") in DERIVED_EVIDENCE_STATUS:
        return True
    if properties.get("corner_leg") or properties.get("diaphragm") or properties.get("auto_diaphragm"):
        return True
    if properties.get("face") in ("diaphragm", "center", "corner"):
        return True
    return False


def is_recognized_bar(properties: Dict[str, Any], *, allow_legacy: bool = False) -> bool:
    """判断一根杆件是否为「直接识别」产物（进 recognition P/R）。

    阶段1.5 fail-closed 语义：必须显式标记 geometry_class=recognized 才计入识别
    召回；未标记语义（unknown）绝不默认视为 recognized。

    allow_legacy=True（对应 CLI --allow-legacy-semantics）时才兼容旧模型的
    evidence_status=recognized 回退。默认 False：正式评测只认 geometry_class。

    判定优先级：
        1. derived / canonical → False（排除）
        2. geometry_class 显式声明 → 以其为准
        3. allow_legacy=True 时：evidence_status=recognized → True；
           mirrored/reconstructed → False
        4. 均未声明（unknown）→ False（fail-closed）
    """
    if is_derived_bar(properties):
        return False
    if is_canonical_bar(properties):
        return False
    cls = properties.get("geometry_class")
    if cls is not None:
        return cls == "recognized"
    # 旧兼容（仅 allow_legacy=True 时生效）：evidence_status 显式声明时以其为准
    if allow_legacy:
        es = properties.get("evidence_status")
        if es == "recognized":
            return True
        if es in ("mirrored", "reconstructed", "derived"):
            return False
    # 未标记任何语义（unknown）→ fail-closed，不默认 recognized
    return False


def is_reconstructed_bar(properties: Dict[str, Any], *, allow_legacy: bool = False) -> bool:
    """判断杆件是否为「确定性重建」产物（进 reconstructed/physical P/R，非识别）。

    阶段1.5 fail-closed：必须显式 geometry_class=reconstructed 才计入；
    unknown 不默认。allow_legacy=True 时才兼容旧 evidence_status=mirrored/reconstructed。

    排除 derived（corner_leg/diaphragm/center）与 canonical（GT 权威）。
    """
    if is_derived_bar(properties):
        return False
    if is_canonical_bar(properties):
        return False
    cls = properties.get("geometry_class")
    if cls is not None:
        return cls == "reconstructed"
    # 旧兼容（仅 allow_legacy=True 时生效）
    if allow_legacy:
        if properties.get("evidence_status") in ("mirrored", "reconstructed"):
            return True
    return False


def is_canonical_bar(properties: Dict[str, Any]) -> bool:
    """判断杆件是否为 GT 权威拓扑（canonical，评测基准，不进生产 P/R）。"""
    if properties.get("gt_aligned"):
        return True
    if properties.get("geometry_class") == "canonical":
        return True
    if properties.get("geometry_origin") == "gim":
        return True
    return False


def _face_to_view(face: str) -> Optional[str]:
    """四面展开杆件的 face 字段 → view 映射。

    f → front；b/l/r → side（镜像侧视面）；corner/diaphragm/center → None（派生面）。
    未展开模型使用 view_type 字段，不走此映射。
    """
    f = (face or "").strip().lower()
    if f == "f":
        return "front"
    if f in ("b", "l", "r"):
        return "side"
    return None


def is_physical_bar(properties: Dict[str, Any], *, allow_legacy: bool = False) -> bool:
    """物理杆件（进 physical P/R）：非 derived、非 canonical。

    physical = recognized + reconstructed（含 mirrored 镜像面）。
    阶段1.5 fail-closed：排除 derived、canonical、以及未声明语义（unknown）。
    """
    if is_derived_bar(properties):
        return False
    if is_canonical_bar(properties):
        return False
    # 必须显式声明为 recognized 或 reconstructed 才进 physical
    return is_recognized_bar(properties, allow_legacy=allow_legacy) or is_reconstructed_bar(properties, allow_legacy=allow_legacy)


def model_has_gt_alignment(model: Dict[str, Any]) -> bool:
    """检测模型是否被 GT 对齐污染（阶段 0.2：评测拒绝 GT 泄漏）。

    任一 tower_bar / tower_node 的 properties.gt_aligned 为真即视为污染。
    阶段1.8 加强：除 gt_aligned 外，还检测：
        * geometry_class == "canonical"
        * geometry_origin == "gim"
        * source reference 指向 ground_truth / canonical

    正式评测必须在本函数返回 True 时退出失败。
    """
    comps = model.get("components") or {}
    if isinstance(comps, dict):
        for comp in comps.values():
            if not isinstance(comp, dict):
                continue
            props = comp.get("properties") if isinstance(comp, dict) else None
            if isinstance(props, dict):
                if props.get("gt_aligned"):
                    return True
                if props.get("geometry_class") == "canonical":
                    return True
                if props.get("geometry_origin") == "gim":
                    return True
            src = comp.get("source") if isinstance(comp, dict) else None
            if isinstance(src, dict):
                ref = str(src.get("reference", "") or "")
                if "ground_truth" in ref or "canonical" in ref:
                    return True
    df = model.get("drawing_file")
    if isinstance(df, dict) and (df.get("properties") or {}).get("gt_aligned"):
        return True
    return False


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


def _midpoint_dist_2d(a: Seg2D, b: Seg2D) -> float:
    """两线段中点距离。"""
    ma = ((a[0] + a[2]) / 2.0, (a[1] + a[3]) / 2.0)
    mb = ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)
    return math.hypot(ma[0] - mb[0], ma[1] - mb[1])


def _angle_diff_2d(a: Seg2D, b: Seg2D) -> float:
    """两无向线段方向夹角（弧度，值域 [0, π/2]）。

    用方向向量点积计算，避免 atan2 在跨越 ±π 时的符号翻转 bug：
        * 0° 与 180°（同一无向方向）→ 0
        * 179° 与 -179° → 约 2°
        * 水平正向与水平反向 → 0
        * 水平与垂直 → π/2
    退化线段（长度 ~0）方向无定义，返回 π/2（视为不相似，交由上层拒绝）。
    """
    dxa, dya = a[2] - a[0], a[3] - a[1]
    dxb, dyb = b[2] - b[0], b[3] - b[1]
    la = math.hypot(dxa, dya)
    lb = math.hypot(dxb, dyb)
    if la <= 1e-9 or lb <= 1e-9:
        return math.pi / 2.0
    uax, uay = dxa / la, dya / la
    ubx, uby = dxb / lb, dyb / lb
    # 无向线段：点积取绝对值，使 0°/180° 等价
    dot = abs(uax * ubx + uay * uby)
    dot = max(-1.0, min(1.0, dot))
    return math.acos(dot)


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
    """两线段综合代价（越小越相似），单位 mm。

    阶段 1.3 重构：先过硬门禁（角度/长度比/端点误差），不过返回 inf；
    过门禁后 cost = 双端点距离（主项），不再用「重叠奖励乘系数」扭曲 cost，
    避免 tolerance 语义被奖励项污染（tolerance 现在明确等于「每个对应端点的
    最大允许误差」）。
    """
    gates = segment_gates(a, b)
    if not gates["pass"]:
        return float("inf")
    # 过门禁后代价即端点误差（单位 mm），单调且非负
    return gates["endpoint_error_mm"]


def segment_gates(a: Seg2D, b: Seg2D) -> Dict[str, Any]:
    """阶段 1.3：显式拆分代价与硬门禁。

    返回五个几何量 + 是否通过硬门禁：
        endpoint_error_mm   双端点距离（正反顺序取最小）
        midpoint_error_mm   中点距离
        angle_error_deg     无向方向夹角（角度）
        length_ratio        长度比（归一化 >= 1）
        overlap_ratio       共线重叠比例 [0,1]
        pass                是否通过硬门禁（角度 <=45° 且长度比 <=3 且非退化）
    """
    end_dist = _endpoint_dist_2d(a, b)
    ang = _angle_diff_2d(a, b)
    lr = _length_ratio(a, b)
    ov = _overlap_ratio(a, b)
    # 退化线段（长度 ~0）拒绝匹配
    la = _seg_len_2d(a)
    lb = _seg_len_2d(b)
    degenerate = (la <= 1e-9 or lb <= 1e-9)
    passed = (
        not degenerate
        and ang <= math.radians(45.0)
        and lr <= 3.0
    )
    return {
        "endpoint_error_mm": end_dist,
        "midpoint_error_mm": _midpoint_dist_2d(a, b),
        "angle_error_deg": math.degrees(ang),
        "length_ratio": lr,
        "overlap_ratio": ov,
        "degenerate": degenerate,
        "pass": passed,
    }


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
    """一对一最优匹配（scipy.linear_sum_assignment），支持 dummy 未匹配。

    阶段 1.4：用 dummy 增广矩阵替代「max_cost*10 填充非法配对」。dummy 配对
    代价固定为 dummy_cost（= max_cost，合法匹配上界），使 Hungarian 可显式选择
    「不匹配」，而不会为降低总成本去牺牲合法匹配（大矩阵里 max_cost*10 填充
    会让 solver 倾向把大量非法配对当 dummy 用，反而牺牲少数合法匹配）。

    返回 (matched_pairs, unmatched_gt_idx, unmatched_model_idx)。
    匹配 cost >= max_cost 的配对视为不匹配（等价于配对到 dummy）。
    """
    n_gt, n_m = len(gt), len(model)
    if n_gt == 0 or n_m == 0:
        return [], list(range(n_gt)), list(range(n_m))

    import numpy as np

    # dummy cost：一个合法匹配的最高代价。任何真实配对 cost >= max_cost 等价于
    # 放弃匹配；dummy 配对统一用 max_cost，确保「匹配一个略低于 max_cost 的合法
    # 对」总是优于「放弃」（因为真实配对 cost < max_cost < dummy）。
    dummy_cost = max_cost

    # 增广矩阵：(n_gt + n_m) x (n_m + n_gt)
    #   左上 (n_gt x n_m)：真实配对代价（< max_cost 才填，否则 dummy_cost）
    #   右上 (n_gt x n_gt)：GT 配对到 dummy model（未匹配 GT）
    #   左下 (n_m x n_m)：dummy GT 配对到 model（未匹配 model）
    #   右下 (n_m x n_gt)：dummy-dummy（恒 0，无意义但需填满）
    N = n_gt + n_m
    cost = np.full((N, N), 0.0)
    # 主匹配区 + dummy 区统一先填 dummy_cost（代表「不匹配」的代价）
    cost[:, :] = dummy_cost

    # 左上：真实配对（仅当 cost < max_cost 才值得匹配，否则保持 dummy_cost）
    for i, g in enumerate(gt):
        for j, m in enumerate(model):
            c = cost_fn(g, m)
            if c < max_cost:
                cost[i, j] = c

    # 左下（dummy GT -> model）：未匹配 model，代价 dummy_cost（已填）
    # 右上（GT -> dummy model）：未匹配 GT，代价 dummy_cost（已填）
    # 右下（dummy-dummy）：恒 0，使多余的 dummy 行/列能互相配对而不产生额外代价
    cost[n_gt:, n_m:] = 0.0

    from scipy.optimize import linear_sum_assignment
    row_ind, col_ind = linear_sum_assignment(cost)

    matched = []
    for i, j in zip(row_ind, col_ind):
        # 只保留「真实 GT <-> 真实 model」且代价 < max_cost 的配对
        if i < n_gt and j < n_m and cost[i, j] < max_cost:
            matched.append((int(i), int(j)))

    matched_gt = {i for i, _ in matched}
    matched_m = {j for _, j in matched}
    unmatched_gt = [i for i in range(n_gt) if i not in matched_gt]
    unmatched_m = [j for j in range(n_m) if j not in matched_m]
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
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        sweep.append({
            "tol": tol, "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4),
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
    mode: str = "physical",
    allow_legacy: bool = False,
) -> List[Tuple[Seg2D, Dict[str, Any]]]:
    """从 model.json 提取 2D 杆件。

    mode="recognition"：仅 recognized（直接识别，排除 mirrored/derived）——A2 几何检测。
    mode="physical"：非 derived（含 mirrored 镜像面）——M3 物理重建。
    allow_legacy=True（--allow-legacy-semantics）：兼容旧 evidence_status 语义。
    返回 [( (x1,y1,x2,y2), properties ), ...]。
    """
    if mode not in ("recognition", "physical"):
        raise ValueError(f"未知 mode={mode}，应为 recognition|physical")
    if mode == "recognition":
        def filter_fn(p):
            return is_recognized_bar(p, allow_legacy=allow_legacy)
    else:
        def filter_fn(p):
            return is_physical_bar(p, allow_legacy=allow_legacy)
    comps = model.get("components", {})
    nodes = {cid: c for cid, c in comps.items() if c.get("kind") == "tower_node"}
    out: List[Tuple[Seg2D, Dict[str, Any]]] = []
    for cid, c in comps.items():
        if c.get("kind") != "tower_bar":
            continue
        p = c.get("properties", {})
        if not filter_fn(p):
            continue
        # 阶段1.6 严格 view 过滤：指定 view 时，必须显式匹配。
        # view_type 缺失时回退到 face 字段映射（f→front, b/l/r→side 等）。
        # 两者都缺失 → unknown_view（不得静默进入指定 view 指标）。
        if view is not None:
            vt = p.get("view_type")
            face = p.get("face")
            # face → view 映射：四面展开后杆件只有 face，无 view_type
            resolved = None
            if vt is not None:
                resolved = vt
            elif face is not None:
                resolved = _face_to_view(str(face))
            if resolved is None or resolved != view:
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
        # 阶段 1.7：不按 round() 坐标静默去重（会吞掉投影重合的不同物理杆）。
        # 每个 component 都是独立物理杆件（physical identity = cid），投影重合
        # 的多根物理杆应保留 multiplicity，不做坐标去重。
        out.append((seg, p))
    return out


def bars_from_model_3d(
    model: Dict[str, Any],
    *,
    mode: str = "physical",
    allow_legacy: bool = False,
) -> List[Tuple[Seg3D, Dict[str, Any]]]:
    """从 model.json 提取 3D 杆件。

    mode="recognition"：仅 recognized；mode="physical"：非 derived（含 mirrored）。
    allow_legacy=True（--allow-legacy-semantics）：兼容旧 evidence_status 语义。
    """
    if mode not in ("recognition", "physical"):
        raise ValueError(f"未知 mode={mode}，应为 recognition|physical")
    if mode == "recognition":
        def filter_fn(p):
            return is_recognized_bar(p, allow_legacy=allow_legacy)
    else:
        def filter_fn(p):
            return is_physical_bar(p, allow_legacy=allow_legacy)
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
        if not filter_fn(p):
            continue
        f, t = p.get("from_node"), p.get("to_node")
        if f in nodes and t in nodes:
            out.append(((nodes[f], nodes[t]), p))
    return out


def gt_bars_2d(gt: Dict[str, Any], view: str) -> List[Tuple[Seg2D, str, str]]:
    """GT 3D 杆件投影到 2D，返回 [(seg, bar_id, section)]。

    阶段 1.7：不按 round() 坐标去重。每根 GT 杆件有唯一物理 ID（bar["id"]），
    投影重合的多根物理杆（如正立面前后重叠的对称杆）应保留 multiplicity，
    不做坐标静默去重。
    """
    nodes = gt["nodes"]
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
    allow_legacy: bool = False,
) -> Dict[str, Any]:
    """A2 几何检测（2D 投影）：GT 投影 vs 模型物理 2D 杆件。"""
    g = gt_bars_2d(gt, view)
    # A2 几何检测 = recognition 评测：只算直接识别的杆件（排除 mirrored/derived）
    m = bars_from_model_2d(model, view=view, mode="recognition", allow_legacy=allow_legacy)
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
    # M3 物理 3D = physical 评测：非 derived（含 mirrored 镜像面）
    m = bars_from_model_3d(model, mode="physical")
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
    # 语义分解（阶段1.9）：physical = recognized + reconstructed。
    # 只输出「可真实计算」的计数/精度分解，不伪造 missed/recall（无法判定
    # 某个 FN 应归属 recognized 还是 reconstructed，因为 GT 无此语义标签）。
    matched_model_idx = {mj for _, mj in result["matched_at_default"]}
    sem = {"recognized": 0, "reconstructed": 0}
    sem_matched = {"recognized": 0, "reconstructed": 0}
    for mi, (seg, p) in enumerate(m):
        if is_recognized_bar(p):
            sem["recognized"] += 1
            if mi in matched_model_idx:
                sem_matched["recognized"] += 1
        elif is_reconstructed_bar(p):
            sem["reconstructed"] += 1
            if mi in matched_model_idx:
                sem_matched["reconstructed"] += 1
    result["model_count_by_semantic"] = sem
    result["matched_model_count_by_semantic"] = sem_matched
    result["precision_by_semantic"] = {
        s: round(sem_matched[s] / sem[s], 4) if sem[s] else 0.0
        for s in ("recognized", "reconstructed")
    }
    # 镜像面分口径（审计补充）：physical P/R 含四面展开的镜像重建面（B/L/R）。
    # 当 3D 合并只来自正立面 sheet 时，镜像面几何是合成预测，与 GT 的 B/L/R 面
    # 天然对不上，会推高 FP——必须让这一失真在指标里可见，而不是静默混在总分里。
    # 只输出「可真实计算」的计数/精度分解；GT 无面标签，per-face recall 无法
    # 计算，不伪造。
    by_face: Counter = Counter()
    matched_by_face: Counter = Counter()
    for mi, (seg, p) in enumerate(m):
        face = str(p.get("generated_face") or p.get("face") or "unknown").upper()
        by_face[face] += 1
        if mi in matched_model_idx:
            matched_by_face[face] += 1
    result["model_count_by_face"] = dict(by_face)
    result["matched_model_count_by_face"] = {f: matched_by_face.get(f, 0) for f in by_face}
    result["precision_by_face"] = {
        f: round(matched_by_face.get(f, 0) / by_face[f], 4) if by_face[f] else 0.0
        for f in by_face
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


# --------------------------------------------------------------------------- #
# A1 件号识别 / A3 件号关联（独立评测，不混入几何 P/R）
# --------------------------------------------------------------------------- #

def _label_ids(gt: Dict[str, Any]) -> set:
    """GT 件号集合（bar id 去重）。

    注意（阶段2.1）：GT bar.id 是物理杆件 ID（PM_XXXX），不是图纸可见件号。
    A1 评测的「图纸可见件号」应由 caller 通过 gt_label_ids 参数显式传入
    （来自标注图纸 GT 或 master BOM 件号集合），而不是直接用 PM_XXXX。
    此函数仅作为「无 BOM 时的回退」，语义上已不推荐用于 A1。
    """
    return {b.get("id") for b in gt.get("bars", []) if b.get("id")}


def _model_label_ids(model: Dict[str, Any]) -> set:
    """模型识别件号集合（tower_bar 的 bar_id，排除 UNLABELED/derived/canonical）。"""
    comps = model.get("components", {})
    ids: set = set()
    for c in comps.values():
        if c.get("kind") != "tower_bar":
            continue
        p = c.get("properties", {})
        if not is_physical_bar(p):
            continue
        bid = p.get("bar_id")
        if bid and not str(bid).startswith("UNLABELED"):
            ids.add(str(bid))
    return ids


def eval_a1_labels(
    gt: Dict[str, Any],
    model: Dict[str, Any],
    *,
    id_mapping: Optional[Dict[str, str]] = None,
    gt_label_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """A1 件号识别：图纸可见件号集合 vs 模型识别件号集合（Exact Match）。

    阶段2.1 GT 语义修正：
        * gt_label_ids：图纸可见件号集合（来自标注图纸 GT 或 master BOM 件号）。
          这是 A1 的正确 GT 基准，不是 GT 物理 ID（PM_XXXX）。
        * 未传 gt_label_ids 时回退到 gt bar.id（物理 ID），语义上仅用于调试。

    id_mapping：图纸数字件号 → 物理 ID 的一对多映射（build_bar_id_mapping 产物），
    用于「物理 ID 映射」口径，不用于 A1 的 Exact Match 主口径。

    注意：A1 只评测「件号是否被识别出来」，不评测几何位置（那是 A2/A3 的职责）。
    """
    if gt_label_ids is not None:
        gt_ids = set(gt_label_ids)
    else:
        gt_ids = _label_ids(gt)
    model_ids = _model_label_ids(model)
    if id_mapping:
        mapped = {id_mapping.get(m, m) for m in model_ids}
        model_ids = mapped
    tp = len(gt_ids & model_ids)
    fp = len(model_ids - gt_ids)
    fn = len(gt_ids - model_ids)
    return {
        "n_gt": len(gt_ids),
        "n_model": len(model_ids),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(tp / len(model_ids), 4) if model_ids else 0.0,
        "recall": round(tp / len(gt_ids), 4) if gt_ids else 0.0,
        "exact_match_rate": round(tp / len(gt_ids), 4) if gt_ids else 0.0,
    }


def eval_a3_association(
    gt: Dict[str, Any],
    model: Dict[str, Any],
    view: str = "front",
    tols: Sequence[float] = DEFAULT_TOLS,
    *,
    id_mapping: Optional[Dict[str, str]] = None,
    allow_legacy: bool = False,
) -> Dict[str, Any]:
    """A3 件号关联：几何匹配对中，件号是否也正确关联到对应杆件。

    先做 A2 几何一对一匹配，再在「匹配对」里检查件号是否一致（经 id_mapping
    归一化后）。这评测的是「识别出的件号贴在正确杆件上」的能力，而非单纯识别。

    注意：本指标依赖几何匹配（A2），但不与 A2 的几何 P/R 混算——它是独立的口径。
    """
    g = gt_bars_2d(gt, view)
    m = bars_from_model_2d(model, view=view, mode="recognition", allow_legacy=allow_legacy)
    gt_segs = [s for s, _, _ in g]
    model_segs = [s for s, _ in m]
    result = eval_segment_pr(gt_segs, model_segs, segment_cost, tols)
    matched = result["matched_at_default"]
    correct = 0
    for gi, mj in matched:
        gid = g[gi][1]
        mid = m[mj][1].get("bar_id", "")
        if not mid or str(mid).startswith("UNLABELED"):
            continue
        if id_mapping:
            target_ids = id_mapping.get(str(mid))
            if isinstance(target_ids, (set, list, tuple)):
                if str(gid) in [str(x) for x in target_ids]:
                    correct += 1
                    continue
            elif target_ids is not None:
                if str(gid) == str(target_ids):
                    correct += 1
                    continue
        if str(gid) == str(mid):
            correct += 1
    n = len(matched)
    return {
        "matched_pairs": n,
        "correct_association": correct,
        "association_rate": round(correct / n, 4) if n else 0.0,
        "n_gt": result["n_gt"],
        "n_model": result["n_model"],
        "fn": result["sweep"][-1]["fn"],
        "fp": result["sweep"][-1]["fp"],
    }
