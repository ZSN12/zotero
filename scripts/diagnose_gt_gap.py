#!/usr/bin/env python3
"""GT 对比差距深度诊断脚本：定位漏检（FN）与误报（FP）的空间分布与结构特征。

阶段 1 评测重写后迁移到 traceability/eval/metrics.py 公共内核：
    * Hungarian 一对一最优匹配（非贪心、端点对齐口径）
    * recognition 口径（只算直接识别杆件，排除 mirrored/derived/canonical）
    * GT 泄漏检测（gt_aligned=True 时拒绝评测，exit 3）

用法：
    python3 scripts/diagnose_gt_gap.py [gt.json] [model.json] [--view front] [--tol 500]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import (  # noqa: E402
    bars_from_model_2d,
    gt_bars_2d,
    hungarian_match,
    model_has_gt_alignment,
    segment_cost,
)

DEFAULT_GT = REPO / "examples/gt/35A1-JC1_ground_truth.json"
DEFAULT_MODEL = REPO / "out/35A1-JC1-full-deliver/model.json"


def _len2d(seg) -> float:
    return math.hypot(seg[2] - seg[0], seg[3] - seg[1])


def _role_2d(seg) -> str:
    """2D 投影粗分类：水平材 / 立柱（主腿投影）/ 斜材。"""
    dx = abs(seg[2] - seg[0])
    dz = abs(seg[3] - seg[1])
    if dz < 50.0:
        return "水平材"
    if dx < 50.0:
        return "主立柱"
    return "斜腹杆"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt", nargs="?", default=str(DEFAULT_GT))
    ap.add_argument("model", nargs="?", default=str(DEFAULT_MODEL))
    ap.add_argument("--view", choices=["front", "side"], default="front")
    ap.add_argument("--tol", type=float, default=500.0)
    ap.add_argument("--allow-legacy-semantics", action="store_true")
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

    if model_has_gt_alignment(model):
        print("✗ GT 泄漏：模型含 gt_aligned/canonical 杆件，正式评测拒绝（阶段 0.2）。")
        sys.exit(3)

    g = gt_bars_2d(gt, args.view)
    m = bars_from_model_2d(model, view=args.view, mode="recognition",
                           allow_legacy=args.allow_legacy_semantics)
    gt_segs = [s for s, _, _ in g]
    model_segs = [s for s, _ in m]
    matched, un_gt, un_m = hungarian_match(gt_segs, model_segs, segment_cost, max_cost=args.tol)

    print("=== GT 诊断总览 ===")
    print(f"GT 总数: {len(gt_segs)}, Model 总数: {len(model_segs)}")
    print(f"TP 匹配: {len(matched)}, FN 漏检: {len(un_gt)}, FP 误报: {len(un_m)}")
    p = len(matched) / len(model_segs) if model_segs else 0.0
    r = len(matched) / len(gt_segs) if gt_segs else 0.0
    print(f"Precision: {p:.1%}, Recall: {r:.1%}\n")

    # 1. 漏检（FN）分析
    fn_by_z = defaultdict(list)
    fn_by_sec = defaultdict(int)
    for gi in un_gt:
        seg = gt_segs[gi]
        z_mid = (seg[1] + seg[3]) / 2.0
        z_bin = int(z_mid // 5000) * 5000
        fn_by_z[z_bin].append((gi, seg))
        fn_by_sec[g[gi][2] or "未知"] += 1

    print("--- 漏检（FN）按高度区间分布 ---")
    for z in sorted(fn_by_z):
        bars_in_z = fn_by_z[z]
        print(f"  Z [{z:5d}, {z + 5000:5d} mm]: {len(bars_in_z):3d} 根漏检")
        for gi, seg in bars_in_z[:3]:
            print(f"    - {_role_2d(seg):4s} {g[gi][2] or '':8s} "
                  f"(x={seg[0]:.0f}->{seg[2]:.0f}, z={seg[1]:.0f}->{seg[3]:.0f}, "
                  f"len={_len2d(seg):.0f})")

    print("\n--- 漏检（FN）按截面规格分布 ---")
    for sec, count in sorted(fn_by_sec.items(), key=lambda x: -x[1])[:8]:
        print(f"  {sec:12s}: {count:3d} 根")

    # 2. 误报（FP）分析
    fp_by_z = defaultdict(list)
    for mi in un_m:
        seg = model_segs[mi]
        z_mid = (seg[1] + seg[3]) / 2.0
        z_bin = int(z_mid // 5000) * 5000
        props = m[mi][1]
        fp_by_z[z_bin].append((mi, seg, props))

    print("\n--- 误报（FP）按高度区间分布 ---")
    for z in sorted(fp_by_z):
        bars_in_z = fp_by_z[z]
        print(f"  Z [{z:5d}, {z + 5000:5d} mm]: {len(bars_in_z):3d} 根误报")
        for mi, seg, props in bars_in_z[:3]:
            bid = props.get("bar_id", "")
            origin = props.get("geometry_origin", "?")
            print(f"    - {_role_2d(seg):4s} bar_id={bid or '∅':10s} origin={origin} "
                  f"(len={_len2d(seg):.0f})")


if __name__ == "__main__":
    main()
