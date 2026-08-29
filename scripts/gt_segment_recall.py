#!/usr/bin/env python3
"""强化 GT 比对：在 evaluate_ground_truth 基础上增加按 Z 分段的召回诊断。

诊断 MLLM/hybrid 几何检测的召回短板：把 GT 和模型杆件按 Z 分段（每 5m），
逐段统计 Precision/Recall，定位「哪段塔身的杆件漏检最严重」。

用法：
    python3 scripts/gt_segment_recall.py <gt.json> <model.json> [--z-band 5000]
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

# 复用 evaluate_ground_truth 的投影/匹配逻辑
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.evaluate_ground_truth import (  # noqa: E402
    gt_bars_2d, model_bars_2d, match_bars, _seg_mid_dist, _seg_angle_diff,
)


def _bar_zmid(bar):
    """杆件中点 Z（第 1 个坐标 = 投影 X，第 2 个 = 投影 Z）。"""
    return (bar[1] + bar[3]) / 2.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt")
    ap.add_argument("model")
    ap.add_argument("--view", choices=["front", "side"], default="front")
    ap.add_argument("--tol", type=float, default=500.0)
    ap.add_argument("--z-band", type=float, default=5000.0, help="Z 分段步长 mm")
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

    gt_bars = gt_bars_2d(gt, args.view)
    model_bars = model_bars_2d(model, view=args.view)

    if not model_bars:
        print("⚠ 模型无可用杆件坐标")
        return

    matched, un_gt, un_m = match_bars(gt_bars, model_bars, args.tol)

    n_gt = len(gt_bars)
    n_model = len(model_bars)
    tp = len(matched)
    fp = len(un_m)
    fn = len(un_gt)

    print(f"=== GT 分段召回诊断（{args.view} 投影，容差 {args.tol:.0f}mm）===")
    print(f"GT 投影杆件: {n_gt} | 模型杆件: {n_model}")
    print(f"匹配 TP={tp}  FP={fp}  FN={fn}")
    print(f"整体 Precision={tp/n_model:.1%}  Recall={tp/n_gt:.1%}")
    print()

    # 按 Z 分段统计
    matched_gt_idx = {gi for gi, _ in matched}
    zmax = max(_bar_zmid(b) for b in gt_bars)
    zmin = min(_bar_zmid(b) for b in gt_bars)
    print(f"GT Z 范围: {zmin:.0f} ~ {zmax:.0f} mm")
    print()
    print(f"{'Z 段(mm)':<16} {'GT':>5} {'模型':>5} {'匹配':>5} {'漏检':>5} {'Recall':>8}")
    print("-" * 55)

    band = args.z_band
    z0 = math.floor(zmin / band) * band
    total_gt = total_model = total_tp = total_fn = 0
    while z0 < zmax:
        z1 = z0 + band
        g_in = [i for i, b in enumerate(gt_bars) if z0 <= _bar_zmid(b) < z1]
        m_in = [j for j, b in enumerate(model_bars) if z0 <= _bar_zmid(b) < z1]
        # 匹配对的 GT 索引落入该段
        t_in = sum(1 for gi in matched_gt_idx if gi in set(g_in))
        f_in = len(g_in) - t_in
        rec = t_in / len(g_in) if g_in else 0.0
        total_gt += len(g_in)
        total_model += len(m_in)
        total_tp += t_in
        total_fn += f_in
        print(f"{z0:>7}-{z1:<7} {len(g_in):>5} {len(m_in):>5} {t_in:>5} {f_in:>5} {rec:>8.1%}")
        z0 = z1

    print("-" * 55)
    print(f"{'合计':<16} {total_gt:>5} {total_model:>5} {total_tp:>5} {total_fn:>5} {total_tp/total_gt if total_gt else 0:>8.1%}")

    # 件号 Exact Match
    exact = 0
    for gi, mj in matched:
        gid = gt_bars[gi][4]
        mid = model_bars[mj][4]
        if mid and not str(mid).startswith("UNLABELED") and gid == mid:
            exact += 1
    print()
    print(f"件号 Exact Match: {exact}/{tp} = {exact/tp:.1%}" if tp else "件号 Exact Match: 0/0")


if __name__ == "__main__":
    main()
