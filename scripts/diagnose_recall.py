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

# 失败类别
FN_MISSING = "missing"          # GT 杆件无任何模型杆件靠近
FN_OVERLAP = "overlap"          # 可能被重叠去重吞掉
FN_SHORT = "short"              # 短杆被过滤
FN_GEOM = "geom"                # 几何偏差过大（端点/角度/长度）
FP_EXTRA = "extra"              # 模型多余杆件（无 GT 对应）
FP_DUP = "duplicate"            # 疑似重复杆件


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


def _classify_fn_failure(seg, model_segs, cost_fn, tol: float) -> str:
    """给一根未匹配的 GT 杆件分类失败原因（找最近模型杆件的代价特征）。"""
    if not model_segs:
        return FN_MISSING
    best = min(cost_fn(seg, m) for m in model_segs)
    if best >= tol * 3:
        return FN_MISSING
    if best < tol * 0.5:
        return FN_OVERLAP  # 有很近的杆件却没匹配，疑似重叠去重吞掉
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
        for key in (("type", typ), ("z", zb), ("len", lb)):
            fn_buckets[key] += 1
        if len(fn_samples) < max_fn:
            fn_samples.append({
                "bar_id": bar_id, "view": view, "length_mm": round(length, 1),
                "type": typ, "z_bucket": zb, "len_bucket": lb,
                "failure": failure, "section": section,
            })

    for j in un_m:
        seg = model_segs[j]
        length = _len_2d(seg)
        z_mid = (seg[1] + seg[3]) / 2.0
        typ = _classify_3d(((seg[0], 0.0, seg[1]), (seg[2], 0.0, seg[3])))
        zb = _z_bucket(z_mid)
        lb = _len_bucket(length)
        for key in (("type", typ), ("z", zb), ("len", lb)):
            fp_buckets[key] += 1
        if len(fp_samples) < max_fp:
            fp_samples.append({
                "length_mm": round(length, 1), "type": typ,
                "z_bucket": zb, "len_bucket": lb, "failure": FP_EXTRA,
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
        for key in (("type", typ), ("z", zb), ("len", lb)):
            fn_buckets[key] += 1
        if len(fn_samples) < max_fn:
            fn_samples.append({
                "bar_id": bar_id, "length_mm": round(length, 1),
                "type": typ, "z_bucket": zb, "len_bucket": lb,
                "failure": failure, "section": section,
            })

    for j in un_m:
        seg = model_segs[j]
        length = _len_3d(seg)
        p, q = seg
        z_mid = (p[2] + q[2]) / 2.0
        typ = _classify_3d(seg)
        zb = _z_bucket(z_mid)
        lb = _len_bucket(length)
        for key in (("type", typ), ("z", zb), ("len", lb)):
            fp_buckets[key] += 1
        if len(fp_samples) < max_fp:
            fp_samples.append({
                "length_mm": round(length, 1), "type": typ,
                "z_bucket": zb, "len_bucket": lb, "failure": FP_EXTRA,
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
        print(f"    {key[0]}={key[1]:16s}  {cnt:5d}")


def _print_samples(title, samples):
    print(f"\n{title}（前 {len(samples)} 条）:")
    if not samples:
        print("  无")
        return
    for s in samples:
        sid = s.get("bar_id", "-")
        print(f"  bar={sid:14s} view={s.get('view','-'):6s} len={s['length_mm']:9.1f} "
              f"type={s['type']:10s} z={s['z_bucket']:10s} lenB={s['len_bucket']:12s} "
              f"failure={s['failure']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt", help="GT json 路径")
    ap.add_argument("model", help="管线输出 model.json")
    ap.add_argument("--view", choices=["front", "side"], default="front")
    ap.add_argument("--tol-2d", type=float, default=200.0, help="2D 匹配容差 mm")
    ap.add_argument("--tol-3d", type=float, default=800.0, help="3D 匹配容差 mm")
    ap.add_argument("--max-fn", type=int, default=20)
    ap.add_argument("--max-fp", type=int, default=20)
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    main()
