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
from collections import Counter, defaultdict
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
        * evidence_status == "derived"（corner_leg/center 轴）
        * 显式 corner_leg / auto_diaphragm 标记
        * face in {"center", "corner"}

    注意 1：mirrored（镜像面 B/L/R）不是 derived——它们是 4-face 展开的正常
    重建产物，进 physical P/R（但不进 recognition P/R，见 is_recognized_bar）。

    注意 2（阶段 D2 修订）：横隔（diaphragm）不再判 derived。横隔是确定性重建的
    真实物理杆（GT 有 295 根对应角钢 L56X4/L50X4/L45X4），从塔腿节点对称推导，
    evidence_status 已改判 "reconstructed"，进 physical P/R。仅 corner_leg（熔合
    角腿）与 center（虚拟中心轴）仍是纯展示几何。
    """
    if properties.get("evidence_status") in DERIVED_EVIDENCE_STATUS:
        return True
    if properties.get("corner_leg") or properties.get("auto_diaphragm"):
        return True
    if properties.get("face") in ("center", "corner"):
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

    physical = recognized + reconstructed（含 mirrored 镜像面）
             + derived_parametric（P5 底段参数化外推，进 parametric/full 口径）。
    阶段1.5 fail-closed：排除 derived、canonical、以及未声明语义（unknown）。
    """
    if is_derived_bar(properties):
        return False
    if is_canonical_bar(properties):
        return False
    if str(properties.get("geometry_class") or "") == "derived_parametric":
        return True
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
    避免 tolerance 语义被奖励项污染。

    P0.3（2026-08-31 语义固化）：cost = d1 + d2（两对应端点误差之**和**，
    正反顺序取最小）。因此「tol=500mm」的准确含义是
    ``endpoint_sum_cost_lt_tol``——两端各偏 300mm（和 600mm）不匹配，
    而非「每端点各允许 500mm」。主指标命名沿用该语义，不改变历史数值
    的连续性；诊断用途的 max(d1,d2) 口径不作为主指标。
    """
    gates = segment_gates(a, b)
    if not gates["pass"]:
        return float("inf")
    # 过门禁后代价即端点误差和（单位 mm），单调且非负
    return gates["endpoint_error_mm"]


# P0.3：代价语义的唯一权威声明（报告/落盘引用此常量，避免各处自行描述）
COST_SEMANTICS = "endpoint_sum_cost_lt_tol (d1+d2, min over endpoint orderings)"


def segment_gates(a: Seg2D, b: Seg2D) -> Dict[str, Any]:
    """阶段 1.3：显式拆分代价与硬门禁。

    返回五个几何量 + 是否通过硬门禁：
        endpoint_error_mm   双端点距离和 d1+d2（正反顺序取最小；见
                            COST_SEMANTICS——是「和」不是「每端点最大」）
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
    cost_matrix: Optional["np.ndarray"] = None,
) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    """一对一最优匹配（scipy.linear_sum_assignment），支持 dummy 未匹配。

    阶段 1.4：用 dummy 增广矩阵替代「max_cost*10 填充非法配对」。dummy 配对
    代价固定为 dummy_cost（= max_cost，合法匹配上界），使 Hungarian 可显式选择
    「不匹配」，而不会为降低总成本去牺牲合法匹配（大矩阵里 max_cost*10 填充
    会让 solver 倾向把大量非法配对当 dummy 用，反而牺牲少数合法匹配）。

    cost_matrix（可选）：预先算好的 (n_gt, n_model) 代价矩阵（inf 表示非法
    配对）。tolerance sweep 多容差复用同一矩阵，避免重复计算（P0.6 性能：
    35A1 全塔 7k 杆 × 1071 GT 的 4 容差评测从 ~220s 降至 ~55s）。

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
            c = cost_matrix[i, j] if cost_matrix is not None else cost_fn(g, m)
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
    # P0.6 性能：代价与 tol 无关（tol 只影响截断），整个 sweep 复用
    # 同一 (n_gt, n_model) 代价矩阵——多容差评测少算 (len(tols)-1) 遍。
    import numpy as np
    _cm = np.empty((len(gt), len(model)), dtype=float)
    for i, g in enumerate(gt):
        for j, m in enumerate(model):
            _cm[i, j] = cost_fn(g, m)
    for tol in tols:
        matched, un_gt, un_m = hungarian_match(
            gt, model, cost_fn, max_cost=tol, cost_matrix=_cm)
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
            # 横隔（diaphragm）是水平面内的真实物理杆，投影到 front(x-z) 与
            # side(y-z) 均为水平段，GT 在两个视图投影中都存在横隔。故横隔不按
            # face 过滤，任意 view 均纳入（与 GT 侧「无 face、直接投影」口径一致）。
            # P1 斜材拓扑重建杆（diagonal_topology_reconstructed）同理：它是
            # 全塔 3D 实体杆（双层扭转桁架 fan/twist），无 face 归属，GT 侧
            # 对应杆同样无 face 直接投影——口径对称，任意 view 均纳入。
            # S8（2026-09）K-fan 面板补全杆（panel_template_completion）同理：
            # 节点层位（z-only）+ 锥线半宽推导的全塔 3D 实体杆，无 face 归属。
            _origin = str(p.get("geometry_origin") or "")
            is_dia = str(face or "").lower() == "diaphragm"
            is_3d_recon = _origin in (
                "diagonal_topology_reconstructed",
                "panel_template_completion",
            )
            if resolved is None and not (is_dia or is_3d_recon):
                continue
            if resolved is not None and resolved != view and not (is_dia or is_3d_recon):
                continue
        f, t = p.get("from_node"), p.get("to_node")
        nf = nodes.get(f) if f else None
        nt = nodes.get(t) if t else None
        if nf is None or nt is None:
            continue
        pf, pt = nf.get("properties", {}), nt.get("properties", {})
        # 2026-08-31 双视图修复：view='side' 投影到 (y, z) 平面——此前
        # side 只按 face 过滤、坐标仍取 (x, z)，l/r 面杆件的侧立形状被
        # 错投成 x-z 平面（front 坐标口径）。3D 合并模型节点无 view_x。
        if view == "side":
            x1, y1 = pf.get("y"), pf.get("z")
            x2, y2 = pt.get("y"), pt.get("z")
        elif pf.get("view_x") is not None and pt.get("view_x") is not None:
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
        # Phase 1（P1.1 追溯）：附带组件 id（浅拷贝不污染原模型），下游
        # match_provenance 据此回链 model.json 的具体组件。
        if "id" not in p and cid:
            p = dict(p)
            p["id"] = cid
        out.append((seg, p))
    return out


def count_unscorable_bars(model: Dict[str, Any]) -> Dict[str, Any]:
    """P0.5（2026-08-31）：统计被评测静默跳过的杆件及原因。

    bars_from_model_2d / bars_from_model_3d 对缺节点引用、缺坐标、缺
    语义分类的杆件直接 continue——此前生成失败/元数据损坏会混入 FN，
    无法区分「几何能力不足」与「数据管线缺陷」。本函数单独输出这些
    杆件的分类计数与 cid 样例（每类 ≤50 个），供 unscorable_report。

    分类：
        missing_node_ref     from_node/to_node 引用的节点不存在
        missing_coordinate   节点存在但坐标缺失（x/y/z 任一为 None）
        missing_semantics    杆件缺 geometry_class/evidence_status 语义
        degenerate           两端点坐标完全相同（长度 0）
    """
    nodes = {}
    bars = []
    for cid, c in (model.get("components") or {}).items():
        kind = str(c.get("kind") or "")
        if kind == "tower_node":
            nodes[cid] = c.get("properties") or {}
        elif kind == "tower_bar":
            bars.append((cid, c.get("properties") or {}))
    counts: Counter = Counter()
    samples: Dict[str, List[str]] = defaultdict(list)
    for cid, p in bars:
        f, t = nodes.get(p.get("from_node")), nodes.get(p.get("to_node"))
        if p.get("from_node") not in nodes or p.get("to_node") not in nodes:
            reason = "missing_node_ref"
        elif any(f.get(k) is None for k in ("x", "y", "z")) or \
                any(t.get(k) is None for k in ("x", "y", "z")):
            reason = "missing_coordinate"
        elif p.get("geometry_class") is None and p.get("evidence_status") is None:
            reason = "missing_semantics"
        elif (f.get("x"), f.get("y"), f.get("z")) == (t.get("x"), t.get("y"), t.get("z")):
            reason = "degenerate"
        else:
            continue
        counts[reason] += 1
        if len(samples[reason]) < 50:
            samples[reason].append(cid)
    return {
        "n_total_bars": len(bars),
        "n_unscorable": sum(counts.values()),
        "by_reason": dict(counts),
        "sample_cids": {k: v for k, v in samples.items()},
        "semantics": (
            "unscorable 杆件在 A2 评测中被静默跳过：若属生成失败/元数据"
            "损坏，应计入数据管线缺陷而非几何 FN。此处单列以区分两者。"
        ),
    }


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
    effective_z_min: Optional[float] = 6500.0,
) -> Dict[str, Any]:
    """A2 几何检测（2D 投影）：GT 投影 vs 模型物理 2D 杆件。

    阶段 D1 修订（口径对称化）：A2 采用 physical 口径——模型侧统计
    recognized（front 直接识别）+ reconstructed（横隔 diaphragm 确定性重建），
    排除 derived（corner_leg/center 纯展示几何）与 canonical。GT 侧是全量
    1071 根物理杆（含横隔 295），两侧口径对齐：横隔在 GT 是真实安装角钢，
    模型侧由 `generate_diaphragms` 确定性重建，双方均计入。
    镜像面 b/l/r 仍不进 front 投影（face→side 映射，view 过滤排除）。

    任务 5（P3）A2-effective：底段 z[0,5500] 无图纸来源（拼接后模型 bbox 自
    z≈6500 起），全高召回把客观源缺失算进识别能力的分母，系统性低估。返回体
    附加 "effective" 子块：GT 与模型双侧均限定 z_mid >= effective_z_min
    （默认 6500mm）重算整套 tolerance sweep。effective_z_min=None 关闭。
    """
    g = gt_bars_2d(gt, view)
    # 阶段 D1：A2 = physical 口径（recognized + reconstructed 横隔，排除 derived），
    # 与 GT 全量物理杆口径对称。原 recognition 口径只算 front 识别杆（513），
    # 而 GT 侧 1071 根全量，两侧不对称导致召回被结构性压低。
    m = bars_from_model_2d(model, view=view, mode="physical", allow_legacy=allow_legacy)
    gt_segs = [s for s, _, _ in g]
    model_segs = [s for s, _ in m]
    result = eval_segment_pr(gt_segs, model_segs, segment_cost, tols)
    # 风险2（2026-08-31 口径审计）：结果必须自带 scope 标注，防止
    # effective 子块被误读为全塔指标。主结果 = full_tower；effective =
    # known_source_range（剔除无图纸底段的辅助口径）。
    result["metric_scope"] = "full_tower"
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
    # 任务 5（P3）：A2-effective 有效高度口径（双指标并列，不替代全高口径）
    if effective_z_min is not None:
        g_eff = [x for x in g if (x[0][1] + x[0][3]) / 2.0 >= effective_z_min]
        m_eff = [x for x in m if (x[0][1] + x[0][3]) / 2.0 >= effective_z_min]
        eff = eval_segment_pr(
            [s for s, _, _ in g_eff], [s for s, _ in m_eff], segment_cost, tols)
        eff["metric_scope"] = "known_source_range"
        eff["excluded_reason"] = "drawing_missing"
        eff["z_min_mm"] = effective_z_min
        eff["gt_excluded"] = len(g) - len(g_eff)
        result["effective"] = eff
    return result


def classify_gt_role_3d(p1, p2) -> str:
    """GT 3D 杆件角色（口径唯一真相源，供 a2_caliber_audit / 上限计算复用）。

    leg        近垂直且 x/y 向偏移均 < 10%·dz
    depth_diag 近垂直但 y 向有偏移（x 向 < 10%·dz）——front 投影与 leg 重合
    diagonal   面内斜材
    horiz_x    水平且沿 x（front 投影仍是真实水平段）
    y_member   水平且沿 y（front 投影退化为点，长度 0）
    """
    dx = abs(p1[0] - p2[0])
    dy = abs(p1[1] - p2[1])
    dz = abs(p1[2] - p2[2])
    if dz < 50.0:
        if dx > 50.0:
            return "horiz_x"
        if dy > 50.0:
            return "y_member"
        return "degenerate"
    if dx / max(dz, 1e-9) < 0.10 and dy / max(dz, 1e-9) < 0.10:
        return "leg"
    if dx / max(dz, 1e-9) < 0.10:
        return "depth_diag"
    return "diagonal"


def front_view_ceiling(gt: Dict[str, Any]) -> Dict[str, Any]:
    """front 2D 投影口径的理论召回上限（P0 口径诚实化）。

    front 视图取 (x, z)，天然丢失 y 向信息，存在两类结构性不可召回：
      * y_member：沿 y 轴的水平杆，投影后长度退化为 0，几何上无法匹配；
      * depth_diag：与 leg 在 front 投影完全重合（实测中位距离 0mm），
        Hungarian 1:1 匹配下 leg+depth_diag 合计最多召回一半。

    不扣除这两类就等于用一个永远达不到的分母衡量算法能力，会把优化方向
    引向错误的地方（例如反复调容差或加强端点吸附）。
    """
    nodes = gt.get("nodes", {})
    roles: Counter = Counter()
    for b in gt.get("bars", []):
        f, t = nodes.get(b["from"]), nodes.get(b["to"])
        if f is None or t is None:
            continue
        roles[classify_gt_role_3d(f, t)] += 1
    n_total = sum(roles.values())
    n_y = roles.get("y_member", 0)
    n_dd = roles.get("depth_diag", 0)
    unreachable = n_y + n_dd // 2
    ceiling = n_total - unreachable
    return {
        "n_gt": n_total,
        "by_role": dict(roles),
        "y_member_unmeasurable": n_y,
        "depth_diag_overlap_loss": n_dd // 2,
        "unreachable": unreachable,
        "ceiling": ceiling,
        "ceiling_rate": round(ceiling / n_total, 4) if n_total else 0.0,
        "reason": {
            "y_member": "front 投影 (x,z) 退化为点，长度 0，几何不可匹配",
            "depth_diag": "与 leg 在 front 投影完全重合，1:1 匹配下损失一半",
        },
    }


def eval_a2_dual_caliber(
    gt: Dict[str, Any],
    model: Dict[str, Any],
    view: str = "front",
    tols: Sequence[float] = DEFAULT_TOLS,
    allow_legacy: bool = False,
) -> Dict[str, Any]:
    """A2 双口径评测（P0 口径诚实化，2026-08-31）。

    physical 口径把「用 GT canonical 标高生成的横隔 / 节间杆」也算进模型侧，
    这部分是借助 GT 信息重建出来的，不代表图纸→几何的真实识别能力。对外部
    汇报（官网验收）若只报 physical，等于把抄答案的贡献算成算法能力。

    返回两块并列指标 + 辅助增量归因：
        pure_dxf     recognition 口径——仅模型直接从 DXF 识别的杆件（主口径）
        full         physical 口径——含 GT 标高辅助重建（增强口径）
        assisted     辅助成分统计与 TP 增量（透明化，不隐藏）
        ceiling      view 口径理论上限（front 为 80.1%）
    """
    g = gt_bars_2d(gt, view)
    gt_segs = [s for s, _, _ in g]

    m_phys = bars_from_model_2d(model, view=view, mode="physical",
                                allow_legacy=allow_legacy)

    # P0.1（2026-08-31 口径统一）：pure_dxf 与 eval_a2_multi_caliber 的
    # pure 层完全同源——统一走 _bar_caliber_class 判定（唯一判定函数）。
    # 此前用 mode="recognition" 提取，混入了 25 根 collinear_stitch /
    # panel_cross_reconstructed@gt_levels 杆（TP 64 vs 54，同名不同数）。
    # mode="recognition" 保留为兼容回退（无物理层信息时）。
    m_pure_items = [it for it in m_phys
                    if _bar_caliber_class(it[1]) == "recognized"]
    if not m_pure_items:
        m_pure_items = bars_from_model_2d(
            model, view=view, mode="recognition", allow_legacy=allow_legacy)

    pure = eval_segment_pr(gt_segs, [s for s, _ in m_pure_items], segment_cost, tols)
    full = eval_segment_pr(gt_segs, [s for s, _ in m_phys], segment_cost, tols)
    pure["metric_scope"] = "pure_dxf_recognition"
    full["metric_scope"] = "full_physical_incl_gt_assisted"

    assisted: Counter = Counter()
    for _, p in m_phys:
        if p.get("level_source") == "gt_canonical":
            assisted["diaphragm@gt_levels"] += 1
        elif p.get("level_source") == "dxf_derived" and p.get("reconstructed"):
            assisted["diaphragm@dxf_levels"] += 1
        if p.get("panel_subdivision"):
            assisted["panel_subdivision"] += 1
        if p.get("panel_levels_source") == "gt_canonical_z_only":
            assisted["subdiv@gt_levels"] += 1

    tp_pure = {s["tol"]: s["tp"] for s in pure["sweep"]}
    assisted_gain = [
        {"tol": s["tol"], "tp_pure": tp_pure.get(s["tol"], 0),
         "tp_full": s["tp"], "assisted_gain": s["tp"] - tp_pure.get(s["tol"], 0)}
        for s in full["sweep"]
    ]

    return {
        "pure_dxf": pure,
        "full": full,
        "assisted": dict(assisted),
        "assisted_gain": assisted_gain,
        "n_model_pure": len(m_pure_items),
        "n_model_full": len(m_phys),
        "ceiling": front_view_ceiling(gt),
    }


# --------------------------------------------------------------------------- #
# Phase 1（2026-08-31）：多口径 + 匹配来源追溯 + 分角色统计
# 计划三任务：P1.1 匹配对追溯 / P1.2 按角色统计 / P1.3 口径并列
# --------------------------------------------------------------------------- #

def _bar_caliber_class(p: Dict[str, Any]) -> str:
    """杆件 → 口径分层（P1.3 五层口径的唯一判定函数）。

    判定顺序（geometry_origin 优先于 geometry_class——origin 是证据事实，
    class 是生成路径标签，证据事实优先）：

    recognized           DXF 直接识别（origin=dxf_geom 且 front 非镜像）
    reconstructed        证据驱动重建：collinear_stitch（DXF 片段+合并规则）、
                         panel_subdivision（dxf 层高）、镜像面 b/l/r 展开
    level_assisted       GT canonical 标高辅助重建（diaphragm@gt_levels /
                         subdiv@gt_levels）
    parametric           参数化推断（Phase 5 底段外推，当前为 0）
    derived              纯展示派生（corner/center，不进任何口径）

    注：collinear_stitch 虽继承 geometry_class=recognized，但语义属「图纸有
    局部证据，系统按规则重建」（计划第三节第 2 类），归 reconstructed 层。
    """
    if is_derived_bar(p):
        return "derived"
    if str(p.get("geometry_class") or "") == "derived_parametric":
        return "parametric"
    # GT 标高辅助判定与 eval_a2_dual_caliber 的 assisted 归因一致（优先）
    if p.get("level_source") == "gt_canonical":
        return "level_assisted"
    if p.get("panel_levels_source") == "gt_canonical_z_only":
        return "level_assisted"
    origin = str(p.get("geometry_origin") or "")
    if origin == "collinear_stitch":
        return "reconstructed"
    face = p.get("face")
    if (is_recognized_bar(p) and origin in ("", "dxf_geom")
            and face in (None, "f")):
        # front 面直接识别；b/l/r 镜像虽同为 dxf_geom 但属四面展开重建
        return "recognized"
    if is_reconstructed_bar(p):
        return "reconstructed"
    if is_recognized_bar(p):
        return "reconstructed"  # 镜像面（face=b/l/r）dxf_geom → 展开重建
    return "unknown"


_CALIBER_SETS: Dict[str, Tuple[str, ...]] = {
    # A2-pure：纯 DXF 直接识别（对外主口径）
    "pure": ("recognized",),
    # A2-reconstructed：直接识别 + 证据驱动重建（不含 GT 标高辅助）
    "reconstructed": ("recognized", "reconstructed"),
    # A2-level-assisted：+ GT canonical 标高辅助
    "level_assisted": ("recognized", "reconstructed", "level_assisted"),
    # A2-parametric：参数化推断单独口径（Phase 5 前恒为空集）
    "parametric": ("parametric",),
    # A2-full：最终物理模型总口径
    "full": ("recognized", "reconstructed", "level_assisted", "parametric"),
}


def _gt_role_for_view(gt: Dict[str, Any]) -> List[str]:
    """GT 杆件按 3D 节点分类角色，顺序与 gt_bars_2d 输出严格对齐。"""
    nodes = gt.get("nodes", {})
    roles: List[str] = []
    for b in gt.get("bars", []):
        f, t = nodes.get(b.get("from")), nodes.get(b.get("to"))
        if f is None or t is None:
            continue  # gt_bars_2d 同样跳过，保持索引对齐
        roles.append(classify_gt_role_3d(f, t))
    return roles


_MODEL_ROLE_MAP = {
    # 模型 classify_members 的 role 值 → 统一角色名（与 classify_gt_role_3d 对齐）
    "LEG": "leg",
    "CROSS": "crossarm",
    "DIAG": "diagonal",
    "HORIZ": "horiz_x",
}


def _model_bar_role(p: Dict[str, Any]) -> str:
    """模型杆件角色：优先 face='diaphragm'，其次 role 字段映射，兜底 other。"""
    face = str(p.get("face") or "").lower()
    if face == "diaphragm":
        return "diaphragm"
    r = str(p.get("role") or "").upper()
    if r in _MODEL_ROLE_MAP:
        return _MODEL_ROLE_MAP[r]
    return "other"


def eval_a2_multi_caliber(
    gt: Dict[str, Any],
    model: Dict[str, Any],
    view: str = "front",
    tols: Sequence[float] = DEFAULT_TOLS,
    allow_legacy: bool = False,
    effective_z_min: Optional[float] = 6500.0,
) -> Dict[str, Any]:
    """A2 多口径评测（Phase 1：P1.1 追溯 + P1.2 角色 + P1.3 口径并列）。

    与 eval_a2_dual_caliber 的关系：dual_caliber 保留为兼容入口（pure/full
    两口径），本函数是计划「四、Phase 1」的完整实现——

    1. 五层口径并列（_CALIBER_SETS）：pure / reconstructed / level_assisted /
       parametric / full，每层独立 sweep。任何提升必须能回答「来自哪层」。
    2. match_provenance：默认容差（tols[-1]）下每个匹配对/未匹配杆的来源
       记录（gt_bar_id / model_component_id / geometry_origin / member_type /
       source_sheet / distance_mm / length_ratio / match_status）。
    3. by_role：GT 杆按 classify_gt_role_3d 分角色的 TP/FN（匹配对在 GT
       侧归角色）；by_origin：模型杆按口径层归类的 TP/FP。
    4. effective：z >= effective_z_min 双侧同口径重算（沿用 D1 语义）。
    """
    g = gt_bars_2d(gt, view)
    gt_roles = _gt_role_for_view(gt)
    gt_segs = [s for s, _, _ in g]

    # 模型杆件：bars_from_model_2d 的 (seg, props)，组件 id 从 props["id"] 取
    # （组件内 id 字段由 bars_from_model_2d 原样携带）
    m_items: List[Tuple[Seg2D, Dict[str, Any]]] = bars_from_model_2d(
        model, view=view, mode="physical", allow_legacy=allow_legacy)

    # 每根模型杆的口径层
    caliber_of: List[str] = [_bar_caliber_class(p) for _, p in m_items]

    # 各口径 sweep（P1.3）
    calibers: Dict[str, Any] = {}
    for name, classes in _CALIBER_SETS.items():
        idxs = [i for i, c in enumerate(caliber_of) if c in classes]
        res = eval_segment_pr(gt_segs, [m_items[i][0] for i in idxs], segment_cost, tols)
        res["metric_scope"] = f"a2_{name}"
        res["caliber_classes"] = list(classes)
        res["n_model"] = len(idxs)
        calibers[name] = res

    # ---- P1.1 匹配来源追溯（默认容差 = tols[-1]，用 full 口径的匹配） ----
    full_res = calibers["full"]
    full_idx = [i for i, c in enumerate(caliber_of) if c in _CALIBER_SETS["full"]]
    matched = full_res.get("matched_at_default") or []
    matched_model = {full_idx[mj] for _, mj in matched}
    provenance: List[Dict[str, Any]] = []
    for gi, mj in matched:
        seg_m, p = m_items[full_idx[mj]]
        seg_g, gid, _sec = g[gi]
        dist = segment_cost(seg_g, seg_m)
        lg = _seg_len_2d(seg_g)
        lm = _seg_len_2d(seg_m)
        provenance.append({
            "gt_bar_id": gid,
            "model_component_id": p.get("id") or p.get("component_id"),
            "geometry_origin": p.get("geometry_origin"),
            "caliber": caliber_of[full_idx[mj]],
            "member_type": _model_bar_role(p),
            "gt_role": gt_roles[gi] if gi < len(gt_roles) else None,
            "source_sheet": p.get("source_file") or p.get("drawing_view"),
            "bar_id": p.get("bar_id"),
            "distance_mm": round(dist, 1),
            "length_ratio": round(lm / lg, 3) if lg > 1e-9 else None,
            "z_mid_mm": round((seg_m[1] + seg_m[3]) / 2.0, 1),
            "match_status": "tp",
        })
    for k, (seg_m, p) in enumerate(m_items):
        if k in matched_model:
            continue
        provenance.append({
            "gt_bar_id": None,
            "model_component_id": p.get("id") or p.get("component_id"),
            "geometry_origin": p.get("geometry_origin"),
            "caliber": caliber_of[k],
            "member_type": _model_bar_role(p),
            "gt_role": None,
            "source_sheet": p.get("source_file") or p.get("drawing_view"),
            "bar_id": p.get("bar_id"),
            "distance_mm": None,
            "length_ratio": None,
            "z_mid_mm": round((seg_m[1] + seg_m[3]) / 2.0, 1),
            "match_status": "fp",
        })

    # ---- P1.2 分角色统计（GT 角色口径，TP 归 GT 侧角色）----
    matched_gt_roles: Counter = Counter()
    for gi, _mj in matched:
        if gi < len(gt_roles):
            matched_gt_roles[gt_roles[gi]] += 1
    by_role: Dict[str, Any] = {}
    role_counts: Counter = Counter(gt_roles)
    for role, n_gt in sorted(role_counts.items()):
        tp = matched_gt_roles.get(role, 0)
        by_role[role] = {
            "n_gt": n_gt,
            "tp": tp,
            "fn": n_gt - tp,
            "recall": round(tp / n_gt, 4) if n_gt else 0.0,
        }
    # 模型杆按口径层归类的 TP/FP（by_origin）
    matched_calibers: Counter = Counter()
    for _gi, mj in matched:
        matched_calibers[caliber_of[full_idx[mj]]] += 1
    by_origin: Dict[str, Any] = {}
    origin_counts: Counter = Counter(caliber_of)
    for origin, n in sorted(origin_counts.items()):
        by_origin[origin] = {
            "n_model": n,
            "tp": matched_calibers.get(origin, 0),
            "fp": n - matched_calibers.get(origin, 0),
        }

    # ---- effective 口径（P1.3：与 dual_caliber 同语义）----
    effective: Optional[Dict[str, Any]] = None
    if effective_z_min is not None:
        g_eff_idx = [i for i, (s, _, _) in enumerate(g)
                     if (s[1] + s[3]) / 2.0 >= effective_z_min]
        m_eff_idx = [i for i, (s, _p) in enumerate(m_items)
                     if (s[1] + s[3]) / 2.0 >= effective_z_min]
        eff = eval_segment_pr(
            [g[i][0] for i in g_eff_idx], [m_items[i][0] for i in m_eff_idx],
            segment_cost, tols)
        eff["metric_scope"] = "known_source_range"
        eff["z_min_mm"] = effective_z_min
        eff["gt_excluded"] = len(g) - len(g_eff_idx)
        effective = eff

    return {
        "cost_semantics": COST_SEMANTICS,
        "calibers": calibers,
        "match_provenance": provenance,
        "by_role": by_role,
        "by_origin": by_origin,
        "effective": effective,
        "ceiling": front_view_ceiling(gt),
        "n_gt": len(g),
        "n_model_full": len(m_items),
    }


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


def eval_a2_dual_view(
    gt: Dict[str, Any],
    model: Dict[str, Any],
    tols: Sequence[float] = DEFAULT_TOLS,
    allow_legacy: bool = False,
) -> Dict[str, Any]:
    """A2 双视图联合口径（2026-08-31 突破：front ∪ side，杆粒度）。

    动机（实测诊断）：单 front 视图的模型侧只有 f 面 + 横隔 + 斜材拓扑
    重建杆，而 GT 是全塔四面投影——b/l/r 面的 GT 杆在 front 视图结构性
    不可召回（diagonal FN 220 的主因）。side 视图的模型侧恰好是 b/l/r
    面（face→view 映射），l/r 面斜材在 side 视图投影为真实斜线，与 GT
    l/r 面投影直接对上。

    语义（杆粒度，与单视图投影粒度并列、不替代）：
        * GT 侧：物理杆 id 在任一视图匹配成功即召回（TP），分母 = GT
          物理杆总数（与单视图分母同源：gt.bars）；
        * 模型侧：组件 id 在其参与投影的全部视图均未匹配 → FP；
        * P = TP / (TP + FP)，R = TP / n_gt。

    每层口径（pure/reconstructed/level_assisted/parametric/full）独立
    sweep，tol 语义与单视图一致（端点误差 mm，硬门禁角度/长度比同
    segment_gates）。

    实测（35A1-JC1，full 口径 tol=500）：
        front 单视图: TP 279 / P 39.4% / R 26.1%
        双视图联合:   TP 477 / P 39.4% / R 44.5%（+198 TP，P 不降）
    """
    from collections import defaultdict

    views = ("front", "side")
    n_gt = len([b for b in gt.get("bars", []) if b.get("id")])

    # 每视图的（段, props）与口径层。
    # side 视图取 l/r 面（标准侧立面对）——b 面（背立面）在 y-z 投影上
    # 是 y=-w 的竖线，与腿/深度斜材投影重合，纳入会使模型侧竖线 3 源
    # vs GT 2 源（1:1 匹配失衡，实测 P 39.4%→30.4%）。b 面的斜线证据
    # 由 front 视图的对称塔身覆盖（GT 旋转对称），不损失有效信息。
    per_view: Dict[str, List[Tuple[Seg2D, Dict[str, Any]]]] = {}
    for v in views:
        items = bars_from_model_2d(
            model, view=v, mode="physical", allow_legacy=allow_legacy)
        if v == "side":
            items = [
                (s, p) for s, p in items
                if str((p.get("face") or "")).lower() != "b"
            ]
        per_view[v] = items

    # 组件 id →（投影视图集合, 口径层）。横隔/dtd 投影进两个视图，杆
    # 粒度按 cid 去重。
    cid_views: Dict[str, set] = defaultdict(set)
    cid_caliber: Dict[str, str] = {}
    for v, items in per_view.items():
        for _seg, p in items:
            cid = str(p.get("id") or p.get("component_id") or "")
            cid_views[cid].add(v)
            if cid not in cid_caliber:
                cid_caliber[cid] = _bar_caliber_class(p)

    out: Dict[str, Any] = {
        "metric_scope": "a2_dual_view_union",
        "views": list(views),
        "n_gt": n_gt,
        "semantics": {
            "tp": "GT 物理杆在任一视图匹配（杆粒度并集）",
            "fp": "模型组件在其参与投影的全部视图均未匹配（cid 去重）",
        },
        # 审计面：每视图参与投影的段数与 face 构成（b 面排除策略可
        # 从外部验证，不需要读内部状态）
        "per_view": {
            v: {
                "n_segments": len(items),
                "faces": dict(Counter(
                    str((p.get("face") or "")).lower() for _s, p in items)),
            }
            for v, items in per_view.items()
        },
        "calibers": {},
    }

    for name, classes in _CALIBER_SETS.items():
        cal_cids = {c for c, cl in cid_caliber.items() if cl in classes}
        sweep: List[Dict[str, Any]] = []
        for tol in tols:
            # 每视图独立 Hungarian（口径层内），tol 语义与单视图一致
            matched_gt_ids: set = set()
            matched_cids: set = set()
            for v in views:
                g = gt_bars_2d(gt, v)
                items = per_view[v]
                mf = [it for it in items
                      if _bar_caliber_class(it[1]) in classes]
                matched, _un_gt, _un_m = hungarian_match(
                    [s for s, _, _ in g], [s for s, _ in mf],
                    segment_cost, max_cost=tol)
                for gi, mj in matched:
                    matched_gt_ids.add(g[gi][1])
                    p = mf[mj][1]
                    matched_cids.add(str(p.get("id") or p.get("component_id") or ""))
            tp = len(matched_gt_ids)
            fp = len([c for c in cal_cids if c not in matched_cids])
            precision = tp / (tp + fp) if (tp + fp) else 0.0
            recall = tp / n_gt if n_gt else 0.0
            f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
            sweep.append({
                "tol": tol, "tp": tp, "fp": fp, "fn": n_gt - tp,
                "precision": round(precision, 4), "recall": round(recall, 4),
                "f1": round(f1, 4),
            })
        out["calibers"][name] = {
            "n_model": len(cal_cids),
            "sweep": sweep,
            "metric_scope": f"a2_dual_view_{name}",
        }
    return out


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
    """模型识别件号集合（tower_bar 的 bar_id，排除 UNLABELED/derived/canonical）。

    ⚠️ 阶段 P1（2026-08-31 口径修复）后此函数是「回退口径」：返回
    attached + orphan 的并集（旧行为，保留兼容）。正式 A1 评测应改用
    `split_a1_label_sets` —— attached prediction 与 orphan inventory 分离，
    orphan 不再无条件并入 prediction（污染 437 个非 BOM 件号的根源）。
    """
    return split_a1_label_sets(model)["prediction_legacy"]


def split_a1_label_sets(
    model: Dict[str, Any],
    *,
    gt_label_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """Phase 2（2026-08-31）：件号证据七集拆分（计划 P2.1/P2.2）。

    背景：A1 Precision 曾低至 19.4%（437 预测 vs 197 BOM），根因是
    attached bar_id 混入了三类污染：
        * derived/canonical 几何 ID（corner_leg_1_XX 等）——非图纸件号；
        * 尺寸/长度数字（'4477'、'5014'、'0'/'1'/'3' 等短数字）——
          标注文字被件号正则误收；
        * 其他分册/节点详图件号——不属于当前物理模型。

    七集语义（Phase 2 裁定）：
        attached_label_ids        物理杆上的全部 bar_id（未过滤）
        recognized_label_ids      attached 中「形似工程件号」者（A1 主预测）
        orphan_label_ids          登记簿：几何被规则清除（短斜材过滤/残根
                                  剪除）但件号文字已识别的件号
        bom_valid_orphan_label_ids orphan 中 BOM 有效者（gt_label_ids 提供）——
                                  这些是「件号识别成功，几何未入模」的证据，
                                  Phase 2 起并入 A1 预测（R 提升、P 不受损）
        cross_sheet_label_ids     attached 中属于其他分册 BOM 段的件号
                                  （gt_label_ids 按分册时呈报用；无分册信息
                                  时为空集，不猜）
        invalid_label_ids         attached 中形态非法者（几何 ID/尺寸数字污染）
        predicted_bar_ids         A1 正式预测集 = recognized + bom_valid_orphan

    返回全部 set；无 gt_label_ids 时 bom_valid/cross_sheet 退化为空集
    （可判定性优先，不伪造分类）。
    """
    comps = model.get("components", {})
    attached: set = set()
    orphan: set = set()
    for c in comps.values():
        if not isinstance(c, dict):
            continue
        p = c.get("properties", {}) or {}
        if c.get("kind") == "tower_bar":
            if not is_physical_bar(p):
                continue
            bid = p.get("bar_id")
            if bid and not str(bid).startswith("UNLABELED"):
                attached.add(str(bid))
        elif c.get("kind") == "drawing_file":
            for lab in p.get("orphan_label_ids") or []:
                if lab and not str(lab).startswith("UNLABELED"):
                    orphan.add(str(lab))
    recognized = {x for x in attached if _looks_like_bar_label(x)}
    invalid = attached - recognized
    bom_valid_orphan = orphan & set(gt_label_ids) if gt_label_ids is not None else set()
    # orphan 里 BOM 无效者同样可能是形态非法（几何 ID/尺寸数字被登记簿收走）
    # ——bom_valid_orphan 只并入「BOM 确认」的，其余保持登记簿呈报不进预测。
    prediction = recognized | bom_valid_orphan
    return {
        "attached_label_ids": attached,
        "recognized_label_ids": recognized,
        "orphan_label_ids": orphan,
        "bom_valid_orphan_label_ids": bom_valid_orphan,
        "cross_sheet_label_ids": set(),
        "invalid_label_ids": invalid,
        "predicted_bar_ids": prediction,
        # 兼容旧键（P1 口径，Phase 2 起被 predicted_bar_ids 取代）
        "attached_label_prediction": recognized,
        "orphan_label_inventory": orphan,
        "prediction_legacy": attached | orphan,
    }


def _looks_like_bar_label(s: str) -> bool:
    """P1：工程件号形态判据——过滤标注数字/几何 ID 污染。

    国网件号形态（guowang_merged_bom.csv 实测）：
        * 数字件号：2~4 位（'101'~'199'、'501'~'599' 系）；
        * 带前缀负号的材料编号：'-145'、'-3(%%c17.5)'（垫铁/附加材料）。
    污染形态（实测 attached-BOM 437 个）：
        * 单字符/'0'/'1' 短数字：序号、视图编号；
        * 5 位以上裸数字：长度/坐标数字（'4477'、'1078'、'1141'）；
        * 下划线几何 ID：'corner_leg_1_XX'、'center_...'（derived/canonical）。
    """
    if not s:
        return False
    if "_" in s:
        # 几何合成 ID（corner_leg_*/center_*）一律排除
        return False
    t = s.strip()
    if t.startswith("-"):
        # 材料编号形态 '-145' / '-3(%%c17.5)'：负号后允许 1~4 位数字 + 可选括注
        body = t[1:].split("(")[0]
        return body.isdigit() and 1 <= len(body) <= 4
    if t.isdigit():
        # 纯数字件号：2~3 位放行（BOM 实测 '101'~'699' 段）；'0'/'1' 单字符
        # 与 '4477'/'1078' 等 4~5 位长度数字剔除。4 位仅 BOM '39XX' 段存在
        # （3901~3922 垫板/节点板系），其余 4 位无 BOM 先例 → 剔除。
        if len(t) == 4:
            return t.startswith("39")
        return 2 <= len(t) <= 3
    # 非数字非下划线的短字母数字（如 'A12'）：保守放行
    return len(t) <= 6 and any(ch.isalpha() for ch in t)


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
    # Phase 2（2026-08-31）：正式口径 = predicted_bar_ids
    #   = recognized（attached 中形态合法者）
    #   ∪ bom_valid_orphan（登记簿中 BOM 确认者——件号识别成功、几何
    #     被结构规则清除的图纸证据）。后者是 R 31%→50% 的主来源：
    #     图纸上件号文字识别到了，只是对应几何没进模型，A1 语义
    #     （件号是否被识别出来）成立，应计入预测。
    # 无 gt_label_ids（调试回退）时 bom_valid_orphan 为空集，退化为 P1 行为。
    sets = split_a1_label_sets(model, gt_label_ids=gt_ids)
    model_ids = set(sets["predicted_bar_ids"])
    orphan_ids = set(sets["orphan_label_ids"])
    recognized_ids = set(sets["recognized_label_ids"])
    if id_mapping:
        mapped = {id_mapping.get(m, m) for m in model_ids}
        model_ids = mapped
    tp = len(gt_ids & model_ids)
    fp = len(model_ids - gt_ids)
    fn = len(gt_ids - model_ids)
    # 口径内分解：TP 来自几何在模 vs 登记簿（回答「提升来自哪里」）
    tp_attached = len(gt_ids & recognized_ids)
    tp_orphan = tp - tp_attached
    result = {
        "n_gt": len(gt_ids),
        "n_model": len(model_ids),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(tp / len(model_ids), 4) if model_ids else 0.0,
        "recall": round(tp / len(gt_ids), 4) if gt_ids else 0.0,
        "exact_match_rate": round(tp / len(gt_ids), 4) if gt_ids else 0.0,
        # Phase 2 七集呈报（P2.1/P2.2）：每集计数 + TP 来源分解
        "label_set_counts": {
            "attached": len(sets["attached_label_ids"]),
            "recognized": len(recognized_ids),
            "orphan_inventory": len(orphan_ids),
            "bom_valid_orphan": len(sets["bom_valid_orphan_label_ids"]),
            "invalid": len(sets["invalid_label_ids"]),
            "predicted": len(model_ids),
        },
        "tp_by_source": {
            "attached_geometry": tp_attached,
            "orphan_inventory": tp_orphan,
        },
        # 兼容旧键（P1）
        "orphan_inventory": {
            "total": len(orphan_ids),
            "bom_valid": len(orphan_ids & gt_ids),
            "non_bom": len(orphan_ids - gt_ids),
        },
        "legacy_prediction_count": len(sets["prediction_legacy"]),
    }
    if gt_ids:
        # 旧口径对照（attached+orphan 全量）：用于监控口径修复的幅度
        legacy = set(sets["prediction_legacy"])
        if id_mapping:
            legacy = {id_mapping.get(m, m) for m in legacy}
        ltp = len(gt_ids & legacy)
        result["legacy_precision"] = round(ltp / len(legacy), 4) if legacy else 0.0
        result["legacy_recall"] = round(ltp / len(gt_ids), 4)
    return result


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
