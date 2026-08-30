#!/usr/bin/env python3
"""强化 GT 比对：在 evaluate_ground_truth 基础上增加按 Z 分段的召回诊断。

诊断几何检测的召回短板：把 GT 和模型杆件按 Z 分段（默认每 5m），
逐段统计漏检数与 Recall，定位「哪段塔身的杆件漏检最严重」
（如塔头 30-35m 段的系统性漏检）。

阶段 1 评测重写后本脚本迁移到 traceability/eval/metrics.py 公共内核：
    * Hungarian 一对一最优匹配（非贪心）
    * recognition 口径（只算直接识别杆件，排除 mirrored/derived/canonical）
    * tolerance sweep（50/100/200/500mm）+ 分段表按 --tol 输出
    * GT 泄漏检测（gt_aligned=True 时拒绝评测，exit 3）

用法：
    python3 scripts/gt_segment_recall.py <gt.json> <model.json> [--view front] [--tol 500] [--z-band 5000]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from traceability.eval.metrics import (  # noqa: E402
    DEFAULT_TOLS,
    bars_from_model_2d,
    eval_segment_pr,
    gt_bars_2d,
    hungarian_match,
    model_has_gt_alignment,
    segment_cost,
)


def _zmid(seg) -> float:
    """杆件中点 Z（投影 2D 中第 2 个坐标 = Z）。"""
    return (seg[1] + seg[3]) / 2.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt", help="GT json 路径")
    ap.add_argument("model", help="管线输出 model.json 路径")
    ap.add_argument("--view", choices=["front", "side"], default="front")
    ap.add_argument("--tol", type=float, default=500.0,
                    help="分段表使用的匹配容差 mm（仅诊断用，正式指标看 sweep）")
    ap.add_argument("--z-band", type=float, default=5000.0, help="Z 分段步长 mm")
    ap.add_argument("--allow-legacy-semantics", action="store_true",
                    help="兼容旧 evidence_status 语义（默认 fail-closed）")
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
    if not gt_segs:
        print("⚠ GT 无可用投影杆件")
        sys.exit(1)

    print(f"=== GT 分段召回诊断（{args.view} 投影，recognition 口径，Hungarian 一对一）===")
    print(f"GT 投影杆件: {len(gt_segs)} | 模型杆件（排除 derived/mirrored）: {len(model_segs)}")
    print()

    # tolerance sweep（含 F1）
    pr = eval_segment_pr(gt_segs, model_segs, segment_cost, DEFAULT_TOLS)
    print("tolerance sweep：")
    print(f"{'tol(mm)':>8} {'TP':>5} {'FP':>5} {'FN':>5} {'Precision':>10} {'Recall':>10} {'F1':>8}")
    for s in pr["sweep"]:
        print(f"{s['tol']:>8.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>10.1%} {s['recall']:>10.1%} {s['f1']:>8.1%}")
    print()

    # 分段表：按 --tol 匹配
    matched, un_gt, un_m = hungarian_match(gt_segs, model_segs, segment_cost, max_cost=args.tol)
    matched_gt_idx = {gi for gi, _ in matched}

    zmax = max(_zmid(b) for b in gt_segs)
    zmin = min(_zmid(b) for b in gt_segs)
    print(f"GT Z 范围: {zmin:.0f} ~ {zmax:.0f} mm（分段容差 {args.tol:.0f}mm）")
    print()
    print(f"{'Z 段(mm)':<16} {'GT':>5} {'模型':>5} {'匹配':>5} {'漏检':>5} {'Recall':>8}")
    print("-" * 55)

    band = max(args.z_band, 1.0)
    z0 = math.floor(zmin / band) * band
    total_gt = total_model = total_tp = total_fn = 0
    while z0 < zmax:
        z1 = z0 + band
        g_in = {i for i, b in enumerate(gt_segs) if z0 <= _zmid(b) < z1}
        m_in = [j for j, b in enumerate(model_segs) if z0 <= _zmid(b) < z1]
        t_in = len(g_in & matched_gt_idx)
        f_in = len(g_in) - t_in
        rec = t_in / len(g_in) if g_in else 0.0
        total_gt += len(g_in)
        total_model += len(m_in)
        total_tp += t_in
        total_fn += f_in
        print(f"{z0:>7.0f}-{z1:<7.0f} {len(g_in):>5} {len(m_in):>5} {t_in:>5} {f_in:>5} {rec:>8.1%}")
        z0 = z1

    print("-" * 55)
    overall = total_tp / total_gt if total_gt else 0.0
    print(f"{'合计':<16} {total_gt:>5} {total_model:>5} {total_tp:>5} {total_fn:>5} {overall:>8.1%}")

    # 件号 Exact Match（匹配对中）
    exact = 0
    for gi, mj in matched:
        gid = g[gi][1]
        mid = m[mj][1].get("bar_id", "")
        if mid and not str(mid).startswith("UNLABELED") and str(gid) == str(mid):
            exact += 1
    print()
    tp = len(matched)
    print(f"件号 Exact Match: {exact}/{tp} = {exact / tp:.1%}" if tp else "件号 Exact Match: 0/0")


if __name__ == "__main__":
    main()
