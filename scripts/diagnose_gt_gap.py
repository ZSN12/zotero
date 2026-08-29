"""GT 对比差距深度诊断脚本：定位漏检（FN）与误报（FP）的具体结构分类与空间分布。"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.evaluate_ground_truth import (
    gt_bars_2d,
    load_gt,
    load_model,
    match_bars,
    model_bars_2d,
)

REPO = Path(__file__).resolve().parent.parent
GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"
MODEL_PATH = REPO / "out/35A1-JC1-full-deliver/model.json"


def main():
    gt = load_gt(GT_PATH)
    model = load_model(MODEL_PATH)

    gb = gt_bars_2d(gt, "front")
    mb = model_bars_2d(model, "front")
    matched, un_gt, un_m = match_bars(gb, mb, 500.0)

    print(f"=== GT 诊断总览 ===")
    print(f"GT 总数: {len(gb)}, Model 总数: {len(mb)}")
    print(f"TP 匹配: {len(matched)}, FN 漏检: {len(un_gt)}, FP 误报: {len(un_m)}")
    print(f"Precision: {len(matched)/len(mb):.1%}, Recall: {len(matched)/len(gb):.1%}\n")

    # 1. 漏检（FN）分析
    fn_by_z = defaultdict(list)
    fn_by_sec = defaultdict(int)
    for gi in un_gt:
        bar = gb[gi]
        x1, z1, x2, z2, bid, sec = bar
        z_mid = (z1 + z2) / 2.0
        z_bin = int(z_mid // 5000) * 5000
        fn_by_z[z_bin].append(bar)
        fn_by_sec[sec or "未知"] += 1

    print("--- 漏检（FN）按高度区间分布 ---")
    for z in sorted(fn_by_z):
        bars_in_z = fn_by_z[z]
        print(f"  Z [{z:5d}, {z+5000:5d} mm]: {len(bars_in_z):3d} 根漏检")
        # 显示几种典型杆件
        sample = bars_in_z[:3]
        for s in sample:
            dx, dz = abs(s[2] - s[0]), abs(s[3] - s[1])
            role = "水平/横隔" if dz < 50 else "主立柱" if dx < 50 else "斜腹杆"
            print(f"    - {role:6s} {s[5]:8s} (x={s[0]:.0f}->{s[2]:.0f}, z={s[1]:.0f}->{s[3]:.0f})")

    print("\n--- 漏检（FN）按截面规格分布 ---")
    for sec, count in sorted(fn_by_sec.items(), key=lambda x: -x[1])[:8]:
        print(f"  {sec:12s}: {count:3d} 根")

    # 2. 误报（FP）分析
    fp_by_z = defaultdict(list)
    for mi in un_m:
        bar = mb[mi]
        x1, z1, x2, z2, bid, sec = bar
        z_mid = (z1 + z2) / 2.0
        z_bin = int(z_mid // 5000) * 5000
        fp_by_z[z_bin].append(bar)

    print("\n--- 误报（FP）按高度区间分布 ---")
    for z in sorted(fp_by_z):
        bars_in_z = fp_by_z[z]
        print(f"  Z [{z:5d}, {z+5000:5d} mm]: {len(bars_in_z):3d} 根误报")


if __name__ == "__main__":
    main()
