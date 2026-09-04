"""阶段 3.1 FN/FP 漏检报告（可几何验证口径）。

对 Hungarian 一对一匹配后的未匹配杆件做事后分桶，输出每根 FN（GT 未召回）/FP
（模型多余）的失败分类与几何证据，供「先诊断、不调容差」的修复定位使用。

设计原则（延续阶段 7 的约束，见 scripts/diagnose_recall.py:45-52 注释）：
    * 每个失败分类都必须由可直接计算的几何量判定（共线重叠、方向夹角、长度比、
      端点误差、中点距离、投影覆盖），绝不臆测「MLLM 漏检」「重叠去重吞掉」等
      管线内部原因——那类伪分类（FN_OVERLAP/FN_SHORT）已在阶段 7 删除；
    * 不放松任何匹配容差：匹配内核完全复用 metrics.hungarian_match +
      metrics.segment_cost（阶段1.3/1.4 语义），本模块只读匹配结果，不影响 P/R；
    * fail-closed：模型含 GT 对齐泄漏（阶段0.2）时直接 raise，不让 GT 参与生产结果。

FN 分类（对每根未匹配 GT 杆，按优先级取第一命中；几何量均来自
metrics.segment_gates(gt_seg, model_seg) 的公开输出——overlap_ratio 即
「模型杆端点投影到 GT 杆方向后的共线重叠长度 / min(len_gt, len_model)」）：
    fragmented          存在 ≥2 根模型杆，各自 overlap_ratio≥0.35 且夹角≤30°，
                        且对 GT 的投影区间并集覆盖 ≥0.8 —— GT 被碎片化检出但没拼回
    length_mismatch     存在 1 根模型杆 overlap_ratio≥0.6、夹角≤30°，但长度比>3
                        —— 被错误拼接/过度延伸成一根长杆
    near_miss_geom      存在中点距 GT 中点 ≤proximity 的模型杆，但匹配代价超容差
                        （端点误差≥tol，或未过角度/长度比硬门禁）—— 几何偏差过大
    one_to_one_conflict 存在中点距 GT 中点 ≤proximity 且端点误差<tol 的模型杆，
                        但它已被一对一匹配分配给其他 GT 杆 —— 纯匹配事实（可从
                        匹配结果直接验证），典型于 GT 投影重合多杆 vs 模型单杆
    missing             proximity 内无任何模型杆 —— 真缺失

  说明：near_miss_geom 的「超 tol」按匹配内核语义取「端点误差≥tol 或未过硬门禁」
  ——segment_cost<max_cost 才可能成为 TP，故端点误差恰等于 tol 的配对同样不可
  能匹配；one_to_one_conflict 是本模块唯一新增分类，其判定材料（proximity、
  端点误差、匹配归属）全部可从输入几何+匹配结果直接复算，非管线原因臆测。

FP 分类（对每根未匹配模型杆，按优先级取第一命中）：
    duplicate_fp        与其他 FP 模型杆 overlap_ratio≥0.6 且夹角≤15°（模型内部
                        重复检出）；组内保留「离 GT 最近」的一根为 representative
                        （到所有 GT 杆中点距离最小，平局取序号小者；GT 为空时取
                        组内第一根），其余标 duplicate_fp
    near_frame          两端点均落在「GT 投影 bbox 外扩 300mm 的边框带」内（即外扩
                        矩形与原 bbox 之间的环形带，含原 bbox 边界）—— 图框/贴边
                        线误检；GT 投影 bbox 为空或任一方向无展延时不判定此分类
    extra               其余

边界行为：
    * GT 为空：无 FN；FP 不可能 near_frame（无 bbox），只判 duplicate_fp/extra；
    * 模型为空：所有 GT 杆为 missing，evidence.nearest_model_mid_mm=null；
    * 所有浮点 round 2 位、非有限值置 null，输出可直接 json.dumps（无 NaN/Inf）。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from traceability.eval.metrics import (
    bars_from_model_2d,
    gt_bars_2d,
    hungarian_match,
    model_has_gt_alignment,
    segment_cost,
    segment_gates,
)

# --------------------------------------------------------------------------- #
# 分类常量（汇总表固定 schema：未出现的类别计 0，便于跨迭代对比）
# --------------------------------------------------------------------------- #

FN_FAILURE_TYPES = (
    "fragmented",
    "length_mismatch",
    "near_miss_geom",
    "one_to_one_conflict",
    "missing",
)
FP_FAILURE_TYPES = ("duplicate_fp", "near_frame", "extra")

# --- FN 判定阈值（诊断分桶口径；匹配容差仍只由调用方 tol 决定，此处不放松） ---
FRAGMENT_OVERLAP_MIN = 0.35       # 碎片对 GT 的共线重叠比例下限
FRAGMENT_MIN_BARS = 2             # 至少 2 根碎片才可能构成 fragmented
FRAGMENT_COVERAGE_MIN = 0.8       # 碎片对 GT 的投影并集覆盖下限
FRAGMENT_ANGLE_MAX_DEG = 30.0     # 碎片与 GT 的方向夹角上限
LENGTH_MISMATCH_OVERLAP_MIN = 0.6  # 过度延伸杆对 GT 的共线重叠比例下限
LENGTH_MISMATCH_RATIO = 3.0       # 长度比严格大于该值才判 length_mismatch
LENGTH_MISMATCH_ANGLE_MAX_DEG = 30.0
NEAR_MISS_ANGLE_GATE_DEG = 45.0   # 与 metrics.segment_gates 硬门禁一致（仅文档性）

# --- FP 判定阈值 ---
DUPLICATE_OVERLAP_MIN = 0.6       # FP 间共线重叠比例下限
DUPLICATE_ANGLE_MAX_DEG = 15.0    # FP 间方向夹角上限
FRAME_MARGIN_MM = 300.0           # GT 投影 bbox 外扩宽度（边框带）

_EPS = 1e-9


# --------------------------------------------------------------------------- #
# 本地几何辅助（metrics 公开 API 未单独暴露的量；语义与 metrics 保持一致）
# --------------------------------------------------------------------------- #

def _r(x: Any) -> Optional[float]:
    """浮点保留 2 位；None / 非有限值 → None，保证 JSON 可序列化（无 NaN/Inf）。"""
    if x is None:
        return None
    v = float(x)
    if not math.isfinite(v):
        return None
    return round(v, 2)


def _seg_len(seg) -> float:
    return math.hypot(seg[2] - seg[0], seg[3] - seg[1])


def _mid_dist(a, b) -> float:
    """两线段中点距离（与 metrics 私有 _midpoint_dist_2d 同义；公开 API 未单独
    暴露中点距离，按同一公式在本模块重实现）。"""
    am = ((a[0] + a[2]) / 2.0, (a[1] + a[3]) / 2.0)
    bm = ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)
    return math.hypot(am[0] - bm[0], am[1] - bm[1])


def _proj_interval(gt_seg, m_seg: Tuple[float, float, float, float],
                   gt_len: float) -> Tuple[float, float]:
    """模型杆两端点在 GT 杆方向轴上的投影区间 [lo, hi]（GT 起点为原点，mm）。

    与 metrics._overlap_ratio 的投影方式一致（先投影到 GT 杆方向再求共线重叠）；
    公开 API 只暴露最终 overlap_ratio 标量，投影覆盖需区间并集，故在此重实现。
    """
    if gt_len <= _EPS:
        return (0.0, 0.0)
    ux = (gt_seg[2] - gt_seg[0]) / gt_len
    uy = (gt_seg[3] - gt_seg[1]) / gt_len
    t0 = (m_seg[0] - gt_seg[0]) * ux + (m_seg[1] - gt_seg[1]) * uy
    t1 = (m_seg[2] - gt_seg[0]) * ux + (m_seg[3] - gt_seg[1]) * uy
    return (min(t0, t1), max(t0, t1))


def _union_coverage(gt_len: float, intervals: Sequence[Tuple[float, float]]) -> float:
    """若干投影区间与 [0, gt_len] 的并集长度 / gt_len。

    用区间并集而非简单求和：重复/部分重叠的碎片不重复计入覆盖，
    保证「覆盖合计」是可几何验证的量（0≤coverage≤1）。
    """
    if gt_len <= _EPS:
        return 0.0
    clipped = []
    for lo, hi in intervals:
        lo2, hi2 = max(0.0, lo), min(gt_len, hi)
        if hi2 > lo2:
            clipped.append((lo2, hi2))
    if not clipped:
        return 0.0
    clipped.sort()
    union_len = 0.0
    cur_lo, cur_hi = clipped[0]
    for lo, hi in clipped[1:]:
        if lo > cur_hi:
            union_len += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    union_len += cur_hi - cur_lo
    return union_len / gt_len


# --------------------------------------------------------------------------- #
# FN 分类（每根未匹配 GT 杆 → (failure_type, evidence)）
# --------------------------------------------------------------------------- #

def _classify_fn(
    gt_seg,
    model_segs: Sequence[Tuple[float, float, float, float]],
    *,
    tol: float,
    proximity_mm: float,
    matched_model_idx: set,
    gt_id_of_model: Dict[int, str],
) -> Tuple[str, Dict[str, Any]]:
    """按优先级 fragmented → length_mismatch → near_miss_geom →
    one_to_one_conflict → missing 取第一命中（判定依据见模块 docstring）。"""
    gt_len = _seg_len(gt_seg)

    rows = []
    for j, mseg in enumerate(model_segs):
        gates = segment_gates(gt_seg, mseg)
        rows.append({
            "j": j,
            "mid": _mid_dist(gt_seg, mseg),
            "ov": gates["overlap_ratio"],
            "ang": gates["angle_error_deg"],
            "lr": gates["length_ratio"],
            "ee": gates["endpoint_error_mm"],
            "pass": bool(gates["pass"]),
            "iv": _proj_interval(gt_seg, mseg, gt_len),
        })
    nearest_mid = min((r["mid"] for r in rows), default=None)

    # 1) fragmented：≥2 根碎片各自共线重叠达阈，且投影并集覆盖 GT 主体
    frag = [r for r in rows
            if r["ov"] >= FRAGMENT_OVERLAP_MIN and r["ang"] <= FRAGMENT_ANGLE_MAX_DEG]
    if len(frag) >= FRAGMENT_MIN_BARS:
        coverage = _union_coverage(gt_len, [r["iv"] for r in frag])
        if coverage >= FRAGMENT_COVERAGE_MIN:
            return "fragmented", {
                "overlap_bars": [r["j"] for r in frag],
                "fragment_count": len(frag),
                "coverage_ratio": _r(coverage),
                "nearest_model_mid_mm": _r(nearest_mid),
            }

    # 2) length_mismatch：单根高重叠、同向，但长度比超 3（过度延伸/错误拼接）
    lm = [r for r in rows
          if r["ov"] >= LENGTH_MISMATCH_OVERLAP_MIN
          and r["ang"] <= LENGTH_MISMATCH_ANGLE_MAX_DEG
          and r["lr"] > LENGTH_MISMATCH_RATIO]
    if lm:
        best = max(lm, key=lambda r: r["ov"])
        return "length_mismatch", {
            "overlap_bars": [best["j"]],
            "overlap_ratio": _r(best["ov"]),
            "length_ratio": _r(best["lr"]),
            "nearest_model_mid_mm": _r(nearest_mid),
        }

    # 3) near_miss_geom：附近有模型杆但匹配代价超容差（端点误差≥tol，或未过
    #    角度/长度比硬门禁——两种情况 segment_cost 均 ≥ max_cost，不可能成 TP）
    near = [r for r in rows
            if r["mid"] <= proximity_mm and (r["ee"] >= tol or not r["pass"])]
    if near:
        best = min(near, key=lambda r: r["mid"])
        return "near_miss_geom", {
            "overlap_bars": [],
            "model_bar_index": best["j"],
            "endpoint_error_mm": _r(best["ee"]),
            "gate_pass": best["pass"],
            "angle_error_deg": _r(best["ang"]),
            "nearest_model_mid_mm": _r(nearest_mid),
        }

    # 4) one_to_one_conflict：附近存在几何上可匹配（过门禁且端点误差<tol）的模型
    #    杆，但已被一对一分配给其他 GT 杆（可从匹配结果直接复验的事实）
    conf = [r for r in rows
            if r["mid"] <= proximity_mm and r["pass"] and r["ee"] < tol
            and r["j"] in matched_model_idx]
    if conf:
        best = min(conf, key=lambda r: r["mid"])
        return "one_to_one_conflict", {
            "overlap_bars": [],
            "model_bar_index": best["j"],
            "endpoint_error_mm": _r(best["ee"]),
            "occupied_by_gt_bar_id": gt_id_of_model.get(best["j"]),
            "nearest_model_mid_mm": _r(nearest_mid),
        }

    # 5) missing：proximity 内无任何模型杆（真缺失）。
    #    （前三类已覆盖「附近有但不可匹配/被占用」的全部情形，此处必然无近邻。）
    return "missing", {
        "overlap_bars": [],
        "nearest_model_mid_mm": _r(nearest_mid),
    }


# --------------------------------------------------------------------------- #
# FP 分类（未匹配模型杆 → duplicate_fp / near_frame / extra）
# --------------------------------------------------------------------------- #

def _duplicate_groups(
    model_segs: Sequence[Tuple[float, float, float, float]],
    un_m: Sequence[int],
    gt_segs: Sequence[Tuple[float, float, float, float]],
) -> Tuple[Dict[int, int], Dict[int, Dict[str, Any]]]:
    """FP 重复分组：与其他 FP 共线重叠≥0.6 且夹角≤15° 视为同一杆的重复检出。

    组内保留「离 GT 最近」（到所有 GT 杆中点距离最小；平局取序号小者；GT 为空
    时取组内第一根）的一根为 representative，其余标 duplicate_fp。
    返回 ({j: representative_j}, {j: evidence})。
    """
    parent = {j: j for j in un_m}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(un_m)):
        for b in range(a + 1, len(un_m)):
            ja, jb = un_m[a], un_m[b]
            gates = segment_gates(model_segs[ja], model_segs[jb])
            if (gates["overlap_ratio"] >= DUPLICATE_OVERLAP_MIN
                    and gates["angle_error_deg"] <= DUPLICATE_ANGLE_MAX_DEG):
                ra, rb = find(ja), find(jb)
                if ra != rb:
                    parent[rb] = ra

    groups: Dict[int, List[int]] = defaultdict(list)
    for j in un_m:
        groups[find(j)].append(j)

    dup_of: Dict[int, int] = {}
    evidence_by_j: Dict[int, Dict[str, Any]] = {}
    for members in groups.values():
        if len(members) < 2:
            continue

        def gt_dist(j: int) -> float:
            if not gt_segs:
                return float("inf")
            return min(_mid_dist(model_segs[j], gs) for gs in gt_segs)

        rep = min(members, key=lambda j: (gt_dist(j), j))
        for j in members:
            if j == rep:
                continue
            dup_of[j] = rep
            gates = segment_gates(model_segs[rep], model_segs[j])
            evidence_by_j[j] = {
                "duplicate_of": rep,
                "overlap_ratio": _r(gates["overlap_ratio"]),
                "angle_error_deg": _r(gates["angle_error_deg"]),
            }
    return dup_of, evidence_by_j


def _gt_bbox(gt_segs: Sequence[Tuple[float, float, float, float]]):
    """GT 投影 bbox=(xmin, ymin, xmax, ymax)；空集或任一方向无展延 → None
    （退化为线/点的 bbox 上「边框带」无几何意义，不判定 near_frame）。"""
    if not gt_segs:
        return None
    xs = [s[0] for s in gt_segs] + [s[2] for s in gt_segs]
    ys = [s[1] for s in gt_segs] + [s[3] for s in gt_segs]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    if (xmax - xmin) <= _EPS or (ymax - ymin) <= _EPS:
        return None
    return (xmin, ymin, xmax, ymax)


def _pt_in_frame_band(px: float, py: float, bbox) -> bool:
    """端点是否落在「bbox 外扩 FRAME_MARGIN_MM 的边框带」内。

    边框带 = 外扩矩形与原 bbox 之间的环形区域（含原 bbox 边界线）：
    端点须在外扩矩形内，且至少一维落在原 bbox 边界上或外侧（贴边）。
    """
    xmin, ymin, xmax, ymax = bbox
    m = FRAME_MARGIN_MM
    if not (xmin - m <= px <= xmax + m and ymin - m <= py <= ymax + m):
        return False
    return px <= xmin or px >= xmax or py <= ymin or py >= ymax


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def build_miss_report(
    gt: dict,
    model: dict,
    *,
    view: str = "front",
    tol: float = 500.0,
    proximity_mm: float = 1500.0,
) -> dict:
    """构建 FN/FP 漏检报告（可几何验证口径，只诊断，不影响 P/R）。

    匹配完全复用 metrics 公开内核：gt_bars_2d + bars_from_model_2d(mode=
    "recognition") + hungarian_match(segment_cost, max_cost=tol)。
    模型含 GT 对齐泄漏（阶段0.2）时 raise ValueError。

    返回 dict（结构见模块 docstring；浮点 round 2 位，可直接 json.dumps）。
    """
    if model_has_gt_alignment(model):
        raise ValueError(
            "模型含 GT 对齐泄漏（gt_aligned / geometry_class=canonical / "
            "geometry_origin=gim），违反阶段0.2 评测红线：GT 不得参与生产结果。"
            "请先清除模型中的 GT 注入，再生成漏检报告"
        )

    g = gt_bars_2d(gt, view)
    m = bars_from_model_2d(model, view=view, mode="recognition")
    gt_segs = [seg for seg, _, _ in g]
    model_segs = [seg for seg, _ in m]
    matched, un_gt, un_m = hungarian_match(gt_segs, model_segs, segment_cost, max_cost=tol)

    matched_model_idx = {j for _, j in matched}
    gt_id_of_model = {j: g[i][1] for i, j in matched}

    # GT 杆件 3D 中点标高（z_mid）：优先取 GT 节点 3D 坐标，缺失回退投影均值
    gt_nodes = gt.get("nodes") or {}
    bar_records: Dict[Any, dict] = {}
    for b in (gt.get("bars") or []):
        if isinstance(b, dict) and b.get("id") is not None:
            bar_records.setdefault(b["id"], b)

    def _z_mid(bar_id, seg) -> Optional[float]:
        rec = bar_records.get(bar_id) or {}
        nf = gt_nodes.get(rec.get("from"))
        nt = gt_nodes.get(rec.get("to"))
        try:
            if nf is not None and nt is not None:
                return _r((float(nf[2]) + float(nt[2])) / 2.0)
        except (TypeError, ValueError, IndexError):
            pass
        return _r((seg[1] + seg[3]) / 2.0)

    # ---- FN ----
    fn_entries: List[Dict[str, Any]] = []
    fn_counter: Counter = Counter()
    for i in un_gt:
        seg = gt_segs[i]
        bar_id, section = g[i][1], g[i][2]
        failure_type, evidence = _classify_fn(
            seg, model_segs,
            tol=tol, proximity_mm=proximity_mm,
            matched_model_idx=matched_model_idx, gt_id_of_model=gt_id_of_model,
        )
        fn_counter[failure_type] += 1
        fn_entries.append({
            "gt_bar_id": bar_id,
            "section": section,
            "x1": _r(seg[0]), "y1": _r(seg[1]),
            "x2": _r(seg[2]), "y2": _r(seg[3]),
            "length_mm": _r(_seg_len(seg)),
            "z_mid": _z_mid(bar_id, seg),
            "failure_type": failure_type,
            "evidence": evidence,
        })

    # ---- FP ----
    dup_of, dup_evidence = _duplicate_groups(model_segs, un_m, gt_segs)
    bbox = _gt_bbox(gt_segs)
    bbox_r = None if bbox is None else [_r(v) for v in bbox]

    fp_entries: List[Dict[str, Any]] = []
    fp_counter: Counter = Counter()
    for j in un_m:
        seg = model_segs[j]
        props = m[j][1] if j < len(m) else {}
        if j in dup_of:
            failure_type = "duplicate_fp"
            evidence: Dict[str, Any] = dup_evidence[j]
        else:
            in_band = (
                bbox is not None
                and _pt_in_frame_band(seg[0], seg[1], bbox)
                and _pt_in_frame_band(seg[2], seg[3], bbox)
            )
            if in_band:
                failure_type = "near_frame"
                evidence = {"frame_bbox_mm": bbox_r, "margin_mm": FRAME_MARGIN_MM}
            else:
                failure_type = "extra"
                nearest_gt = min((_mid_dist(seg, gs) for gs in gt_segs), default=None)
                evidence = {"nearest_gt_mid_mm": _r(nearest_gt)}
        fp_counter[failure_type] += 1
        fp_entries.append({
            "model_bar_index": j,
            "bar_id": props.get("bar_id"),
            "geometry_origin": props.get("geometry_origin"),
            "x1": _r(seg[0]), "y1": _r(seg[1]),
            "x2": _r(seg[2]), "y2": _r(seg[3]),
            "length_mm": _r(_seg_len(seg)),
            "failure_type": failure_type,
            "evidence": evidence,
        })

    n_gt, n_model = len(gt_segs), len(model_segs)
    return {
        "view": view,
        "tol": _r(tol),
        "n_gt": n_gt,
        "n_model": n_model,
        "matched": len(matched),
        "precision": _r(len(matched) / n_model) if n_model else 0.0,
        "recall": _r(len(matched) / n_gt) if n_gt else 0.0,
        "fn": fn_entries,
        "fp": fp_entries,
        "fn_summary": {k: fn_counter.get(k, 0) for k in FN_FAILURE_TYPES},
        "fp_summary": {k: fp_counter.get(k, 0) for k in FP_FAILURE_TYPES},
    }
