#!/usr/bin/env python3
"""阶段 2 召回诊断：分桶定位 2D/3D 召回瓶颈（只诊断，不调容差）。

官网验收要求「先诊断、不调容差强绿」。本脚本对 GT vs 模型做 Hungarian 一对一
匹配后，把未匹配的 GT 杆件（FN）与未匹配的模型杆件（FP）按多维度分桶，
输出每个桶的召回缺口与 FN/FP 样例清单，供阶段 2 修复（crop 覆盖、重叠去重、
短杆/斜材）定位。

分桶维度：
    * 杆件类型（leg / diagonal / horizontal / degenerate）
    * Z 标高段（0-10m / 10-20m / 20-30m / 30m+ 塔头）
    * 长度区间（<300 / 300-1000 / 1000-3000 / >3000 mm）
    * 2D 视图（front / side）

用法：
    python3 scripts/diagnose_recall.py <gt.json> <model.json> [--view front] [--max-fn 20] [--max-fp 20]

输出：
    * 每桶 FN 数量（GT 未召回）与 FP 数量（模型多余）
    * FN/FP 样例：bar_id / sheet / view / 长度 / 类型 / 失败类别
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from traceability.eval.metrics import (
    hungarian_match,
    segment_cost,
    segment_cost_3d,
    gt_bars_2d,
    gt_bars_3d,
    bars_from_model_2d,
    bars_from_model_3d,
    _classify_3d,
)

# 失败类别（阶段 7：删除伪分类 FN_OVERLAP/FN_SHORT——那些是「猜测」的失败原因，
# 未经几何验证，会误导召回诊断。只保留可验证的两类：
#   * missing —— GT 杆件附近无任何模型杆件（真缺失）
#   * geom    —— 存在模型杆件但几何偏差超过匹配容差（端点/角度/长度不符）
# 其余（重叠去重、短杆过滤等）一律归入 geom，由几何代价本身说话，不臆测管线原因。）
FN_MISSING = "missing"          # GT 杆件无任何模型杆件靠近
FN_GEOM = "geom"                # 几何偏差过大（端点/角度/长度）
FP_EXTRA = "extra"              # 模型多余杆件（无 GT 对应）


def _len_2d(seg) -> float:
    x1, y1, x2, y2 = seg
    return math.hypot(x2 - x1, y2 - y1)


def _len_3d(seg) -> float:
    p, q = seg
    return math.sqrt(sum((q[i] - p[i]) ** 2 for i in range(3)))


def _z_bucket(z: float) -> str:
    if z < 10000.0:
        return "0-10m"
    if z < 20000.0:
        return "10-20m"
    if z < 30000.0:
        return "20-30m"
    return "30m+塔头"


def _len_bucket(length: float) -> str:
    if length < 300.0:
        return "<300mm"
    if length < 1000.0:
        return "300-1000mm"
    if length < 3000.0:
        return "1000-3000mm"
    return ">3000mm"


# --------------------------------------------------------------------------- #
# sheet / view_type / has_label 提取（阶段 1.4 新增维度）
# --------------------------------------------------------------------------- #

SHEET_NONE = "<none>"
VIEW_NONE = "<none>"


def _bar_sheet(props: dict) -> str:
    """杆件来源图纸 sheet：drawing_view / source_file，缺失则占位。"""
    for key in ("drawing_view", "source_file"):
        v = props.get(key)
        if v not in (None, "", "None"):
            return str(v)
    return SHEET_NONE


def _bar_view_type(props: dict, fallback: str = "") -> str:
    """视图类型：优先 view_type / face（物理面），回退 2D 投影视图。"""
    vt = props.get("view_type")
    if vt not in (None, "", "None"):
        return str(vt)
    face = props.get("face")
    if face not in (None, "", "None"):
        return str(face)
    if fallback:
        return fallback
    return VIEW_NONE


def _bar_has_label(props: dict) -> bool:
    bid = props.get("bar_id")
    return bool(bid) and not str(bid).startswith("UNLABELED")


def _nearest_model_ctx(seg, model_items, cost_fn):
    """FN 匹配上下文：找几何最接近的模型杆件，返回 (sheet, view_type, has_label)。"""
    if not model_items:
        return SHEET_NONE, VIEW_NONE, False
    best_i, best_c = None, float("inf")
    for i, (mseg, _) in enumerate(model_items):
        c = cost_fn(seg, mseg)
        # 跳过退化杆件（cost 为 inf/NaN），避免 best_i 保持 None 导致索引崩溃
        if c is None or math.isinf(c) or math.isnan(c):
            continue
        if c < best_c:
            best_c = c
            best_i = i
    if best_i is None:
        return SHEET_NONE, VIEW_NONE, False
    props = model_items[best_i][1]
    return _bar_sheet(props), _bar_view_type(props), _bar_has_label(props)


def _classify_fn_failure(seg, model_segs, cost_fn, tol: float) -> str:
    """给一根未匹配的 GT 杆件分类失败原因（仅可验证的两类）。

    阶段 7：删除 FN_OVERLAP 伪分类——「有很近的杆件却没匹配」只是几何代价
    偏低的一种表现，无法证明是「重叠去重吞掉」；臆测管线原因会误导诊断。
    统一归为：
        * missing —— 无模型杆件在 tol*3 内（真缺失）
        * geom    —— 存在模型杆件但超出 tol 匹配容差（几何偏差）
    """
    if not model_segs:
        return FN_MISSING
    # 跳过退化杆件（cost 为 inf/NaN），避免 min() 返回 inf 掩盖真实几何代价
    costs = [cost_fn(seg, m) for m in model_segs]
    finite = [c for c in costs if c is not None and not math.isinf(c) and not math.isnan(c)]
    if not finite:
        return FN_MISSING
    best = min(finite)
    if best >= tol * 3:
        return FN_MISSING
    return FN_GEOM


def diagnose_2d(gt, model, view, tol, max_fn, max_fp):
    """2D 召回诊断（A2 几何检测口径）。"""
    g = gt_bars_2d(gt, view)
    m = bars_from_model_2d(model, view=view, mode="recognition")
    gt_segs = [s for s, _, _ in g]
    model_segs = [s for s, _ in m]
    matched, un_gt, un_m = hungarian_match(gt_segs, model_segs, segment_cost, tol)

    fn_buckets = defaultdict(int)
    fp_buckets = defaultdict(int)
    fn_samples = []
    fp_samples = []

    for i in un_gt:
        seg = gt_segs[i]
        bar_id, section = g[i][1], g[i][2]
        length = _len_2d(seg)
        z_mid = (seg[1] + seg[3]) / 2.0
        typ = _classify_3d(((seg[0], 0.0, seg[1]), (seg[2], 0.0, seg[3])))
        zb = _z_bucket(z_mid)
        lb = _len_bucket(length)
        failure = _classify_fn_failure(seg, model_segs, segment_cost, tol)
        sheet, vtype, _ = _nearest_model_ctx(seg, m, segment_cost)
        has_label = bool(bar_id) and not str(bar_id).startswith("UNLABELED")
        for key in (
            ("type", typ), ("z", zb), ("len", lb),
            ("sheet", sheet), ("view_type", vtype), ("has_label", has_label),
        ):
            fn_buckets[key] += 1
        if len(fn_samples) < max_fn:
            fn_samples.append({
                "bar_id": bar_id, "view": view, "length_mm": round(length, 1),
                "type": typ, "z_bucket": zb, "len_bucket": lb,
                "sheet": sheet, "view_type": vtype, "has_label": has_label,
                "failure": failure, "section": section,
            })

    for j in un_m:
        seg = model_segs[j]
        props = m[j][1] if j < len(m) else {}
        length = _len_2d(seg)
        z_mid = (seg[1] + seg[3]) / 2.0
        typ = _classify_3d(((seg[0], 0.0, seg[1]), (seg[2], 0.0, seg[3])))
        zb = _z_bucket(z_mid)
        lb = _len_bucket(length)
        sheet = _bar_sheet(props)
        vtype = _bar_view_type(props, view)
        has_label = _bar_has_label(props)

        for key in (
            ("type", typ), ("z", zb), ("len", lb),
            ("sheet", sheet), ("view_type", vtype), ("has_label", has_label),
        ):
            fp_buckets[key] += 1
        if len(fp_samples) < max_fp:
            fp_samples.append({
                "length_mm": round(length, 1), "type": typ,
                "z_bucket": zb, "len_bucket": lb, "failure": FP_EXTRA,
                "sheet": sheet, "view_type": vtype, "has_label": has_label,
            })

    return {
        "n_gt": len(gt_segs), "n_model": len(model_segs),
        "matched": len(matched), "fn": len(un_gt), "fp": len(un_m),
        "fn_by": dict(fn_buckets), "fp_by": dict(fp_buckets),
        "fn_samples": fn_samples, "fp_samples": fp_samples,
    }


def diagnose_3d(gt, model, tol, max_fn, max_fp):
    """3D 召回诊断（M3 物理口径）。"""
    g = gt_bars_3d(gt)
    m = bars_from_model_3d(model, mode="physical")
    gt_segs = [s for s, _, _ in g]
    model_segs = [s for s, _ in m]
    matched, un_gt, un_m = hungarian_match(gt_segs, model_segs, segment_cost_3d, tol)

    fn_buckets = defaultdict(int)
    fp_buckets = defaultdict(int)
    fn_samples = []
    fp_samples = []

    for i in un_gt:
        seg = gt_segs[i]
        bar_id, section = g[i][1], g[i][2]
        length = _len_3d(seg)
        p, q = seg
        z_mid = (p[2] + q[2]) / 2.0
        typ = _classify_3d(seg)
        zb = _z_bucket(z_mid)
        lb = _len_bucket(length)
        failure = _classify_fn_failure(seg, model_segs, segment_cost_3d, tol)
        sheet, vtype, _ = _nearest_model_ctx(seg, m, segment_cost_3d)
        has_label = bool(bar_id) and not str(bar_id).startswith("UNLABELED")
        for key in (
            ("type", typ), ("z", zb), ("len", lb),
            ("sheet", sheet), ("view_type", vtype), ("has_label", has_label),
        ):
            fn_buckets[key] += 1
        if len(fn_samples) < max_fn:
            fn_samples.append({
                "bar_id": bar_id, "length_mm": round(length, 1),
                "type": typ, "z_bucket": zb, "len_bucket": lb,
                "sheet": sheet, "view_type": vtype, "has_label": has_label,
                "failure": failure, "section": section,
            })

    for j in un_m:
        seg = model_segs[j]
        props = m[j][1] if j < len(m) else {}
        length = _len_3d(seg)
        p, q = seg
        z_mid = (p[2] + q[2]) / 2.0
        typ = _classify_3d(seg)
        zb = _z_bucket(z_mid)
        lb = _len_bucket(length)
        sheet = _bar_sheet(props)
        vtype = _bar_view_type(props)
        has_label = _bar_has_label(props)

        for key in (
            ("type", typ), ("z", zb), ("len", lb),
            ("sheet", sheet), ("view_type", vtype), ("has_label", has_label),
        ):
            fp_buckets[key] += 1
        if len(fp_samples) < max_fp:
            fp_samples.append({
                "length_mm": round(length, 1), "type": typ,
                "z_bucket": zb, "len_bucket": lb, "failure": FP_EXTRA,
                "sheet": sheet, "view_type": vtype, "has_label": has_label,
            })

    return {
        "n_gt": len(gt_segs), "n_model": len(model_segs),
        "matched": len(matched), "fn": len(un_gt), "fp": len(un_m),
        "fn_by": dict(fn_buckets), "fp_by": dict(fp_buckets),
        "fn_samples": fn_samples, "fp_samples": fp_samples,
    }


def _print_buckets(title, buckets):
    if not buckets:
        print(f"  {title}: 无")
        return
    print(f"  {title}:")
    for key, cnt in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"    {key[0]}={str(key[1]):16s}  {cnt:5d}")


def _print_samples(title, samples):
    print(f"\n{title}（前 {len(samples)} 条）:")
    if not samples:
        print("  无")
        return
    for s in samples:
        sid = s.get("bar_id", "-")
        sheet = s.get("sheet", "-")
        vtype = s.get("view_type", "-")
        has_label = s.get("has_label")
        print(f"  bar={sid:14s} sheet={sheet:14s} view={s.get('view','-'):6s} "
              f"vt={vtype:10s} label={has_label!s:5s} len={s['length_mm']:9.1f} "
              f"type={s['type']:10s} z={s['z_bucket']:10s} lenB={s['len_bucket']:12s} "
              f"failure={s['failure']}")


def _stringify_buckets(buckets):
    """把 tuple key 的分桶表转成 JSON 可序列化的 dict（"维度=值" -> count）。"""
    return {f"{k[0]}={k[1]}": v for k, v in buckets.items()}


def main():
    ap = argparse.ArgumentParser()
    # 兼容命名参数（--gt/--model，阶段 1.4 验收命令）与旧位置参数两种形式
    ap.add_argument("gt", nargs="?", help="GT json 路径（位置参数形式）")
    ap.add_argument("model", nargs="?", help="管线输出 model.json（位置参数形式）")
    ap.add_argument("--gt", dest="gt_named", help="GT json 路径")
    ap.add_argument("--model", dest="model_named", help="管线输出 model.json")
    ap.add_argument("--view", choices=["front", "side"], default="front")
    ap.add_argument("--tol-2d", type=float, default=200.0, help="2D 匹配容差 mm")
    ap.add_argument("--tol-3d", type=float, default=800.0, help="3D 匹配容差 mm")
    ap.add_argument("--max-fn", type=int, default=20)
    ap.add_argument("--max-fp", type=int, default=20)
    ap.add_argument("--save", help="把 FN/FP 样例导出为 hard-case JSON（错误回放数据集）")
    ap.add_argument("--miss-report", dest="miss_report", default=None,
                    help="追加生成阶段 3.1 FN/FP 漏检报告 JSON 到指定路径"
                         "（可几何验证口径，复用 --view/--tol-2d）")
    args = ap.parse_args()

    gt_path = args.gt_named or args.gt
    model_path = args.model_named or args.model
    if not gt_path or not model_path:
        ap.error("需要提供 GT 与 model 路径（--gt/--model 或位置参数）")

    gt = json.loads(Path(gt_path).read_text(encoding="utf-8"))
    model = json.loads(Path(model_path).read_text(encoding="utf-8"))

    print("=" * 70)
    print(f"召回诊断（只诊断，不调容差）: tol_2d={args.tol_2d}mm tol_3d={args.tol_3d}mm")
    print("=" * 70)

    d2 = diagnose_2d(gt, model, args.view, args.tol_2d, args.max_fn, args.max_fp)
    print(f"\n[A2 2D 几何检测（{args.view} 投影）]")
    print(f"  GT={d2['n_gt']} 模型={d2['n_model']} 匹配={d2['matched']} "
          f"FN={d2['fn']} FP={d2['fp']} 召回={d2['matched']/d2['n_gt']:.1%}" if d2['n_gt'] else "  GT=0")
    _print_buckets("FN 分桶（GT 未召回）", d2["fn_by"])
    _print_buckets("FP 分桶（模型多余）", d2["fp_by"])
    _print_samples("FN 样例（未召回 GT）", d2["fn_samples"])
    _print_samples("FP 样例（模型多余）", d2["fp_samples"])

    d3 = diagnose_3d(gt, model, args.tol_3d, args.max_fn, args.max_fp)
    print(f"\n[M3 3D 物理（排除 derived）]")
    print(f"  GT={d3['n_gt']} 模型={d3['n_model']} 匹配={d3['matched']} "
          f"FN={d3['fn']} FP={d3['fp']} 召回={d3['matched']/d3['n_gt']:.1%}" if d3['n_gt'] else "  GT=0")
    _print_buckets("FN 分桶（GT 未召回）", d3["fn_by"])
    _print_buckets("FP 分桶（模型多余）", d3["fp_by"])
    _print_samples("FN 样例（未召回 GT）", d3["fn_samples"])
    _print_samples("FP 样例（模型多余）", d3["fp_samples"])

    if args.save:
        hard_cases = {
            "gt": str(Path(gt_path).resolve()),
            "model": str(Path(model_path).resolve()),
            "tol_2d": args.tol_2d, "tol_3d": args.tol_3d, "view": args.view,
            "a2_2d": {
                "fn_by": _stringify_buckets(d2["fn_by"]),
                "fp_by": _stringify_buckets(d2["fp_by"]),
                "fn": d2["fn_samples"], "fp": d2["fp_samples"],
            },
            "m3_3d": {
                "fn_by": _stringify_buckets(d3["fn_by"]),
                "fp_by": _stringify_buckets(d3["fp_by"]),
                "fn": d3["fn_samples"], "fp": d3["fp_samples"],
            },
        }
        save_path = Path(args.save)
        save_path.write_text(json.dumps(hard_cases, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✓ hard-case 回放数据集已保存 -> {save_path}")

    # 阶段 3.1：FN/FP 漏检报告（可几何验证口径）。只追加输出，不改上方诊断逻辑。
    if args.miss_report:
        from traceability.eval.miss_report import (
            build_miss_report,
            FN_FAILURE_TYPES,
            FP_FAILURE_TYPES,
        )

        report = build_miss_report(gt, model, view=args.view, tol=args.tol_2d)
        miss_path = Path(args.miss_report)
        miss_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[阶段 3.1 FN/FP 漏检报告（可几何验证口径）] view={args.view} tol={args.tol_2d}mm")
        print(f"  GT={report['n_gt']} 模型={report['n_model']} 匹配={report['matched']} "
              f"FN={len(report['fn'])} FP={len(report['fp'])}")
        print("  FN 失败分类:")
        for k in FN_FAILURE_TYPES:
            print(f"    {k:22s}{report['fn_summary'].get(k, 0):6d}")
        print("  FP 失败分类:")
        for k in FP_FAILURE_TYPES:
            print(f"    {k:22s}{report['fp_summary'].get(k, 0):6d}")
        print(f"  ✓ 漏检报告已保存 -> {miss_path}")


if __name__ == "__main__":
    main()
