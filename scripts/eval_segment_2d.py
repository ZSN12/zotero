#!/usr/bin/env python3
"""阶段2.6：单段（如 06 段 11-16m）可见杆 2D 对照 GT 评测。

与 gt_segment_recall.py 的区别：本脚本按「段 Z 范围」直接过滤 GT 前投影杆件
与模型 2D 杆件（而非按 z-band 粗扫），输出该段的 recognition 口径 P/R（A2）。

段 Z 范围映射（35A1-JC1 六段塔身）：
    seg   Z 范围(mm)    对应 DXF 分册
    40    0~5500         35A1-JC1-40
    07    5500~11000     35A1-JC1-07
    06    11000~16000    35A1-JC1-06
    05    16000~23000    35A1-JC1-05
    04    23000~30000    35A1-JC1-04
    02    30000~36600    35A1-JC1-02

用法：
    python3 scripts/eval_segment_2d.py <gt.json> <model.json> --seg 06 [--view front]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from traceability.eval.metrics import (
    DEFAULT_TOLS,
    bars_from_model_2d,
    eval_segment_pr,
    gt_bars_2d,
    hungarian_match,
    model_has_gt_alignment,
    segment_cost,
)

SEGMENT_Z = {
    "40": (0.0, 5500.0),
    "07": (5500.0, 11000.0),
    "06": (11000.0, 16000.0),
    "05": (16000.0, 23000.0),
    "04": (23000.0, 30000.0),
    "02": (30000.0, 36600.0),
}


def _zmid(seg):
    return (seg[1] + seg[3]) / 2.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt", help="GT json")
    ap.add_argument("model", help="model.json")
    ap.add_argument("--seg", required=True, choices=sorted(SEGMENT_Z),
                    help="段号（40/07/06/05/04/02）")
    ap.add_argument("--view", choices=["front", "side"], default="front")
    ap.add_argument("--tol", type=float, default=200.0,
                    help="主匹配容差 mm（默认 200）")
    args = ap.parse_args()

    z0, z1 = SEGMENT_Z[args.seg]
    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

    if model_has_gt_alignment(model):
        print("✗ GT 泄漏：模型含 gt_aligned/canonical 杆件，正式评测拒绝。")
        sys.exit(3)

    g_all = gt_bars_2d(gt, args.view)
    m_all = bars_from_model_2d(model, view=args.view, mode="recognition")

    # 按段 Z 范围过滤（中点落在 [z0, z1)）
    g_seg = [t for t in g_all if z0 <= _zmid(t[0]) < z1]
    m_seg = [t for t in m_all if z0 <= _zmid(t[0]) < z1]

    gt_segs = [s for s, _, _ in g_seg]
    model_segs = [s for s, _ in m_seg]

    print(f"=== 阶段2.6 段 {args.seg}（Z {z0:.0f}~{z1:.0f}mm）{args.view} 投影对照 GT ===")
    print(f"GT 段内前投影杆件: {len(gt_segs)} | 模型 recognition 杆件: {len(model_segs)}")
    print()

    if not gt_segs:
        print("⚠ 该段 GT 无前投影杆件")
        return 1

    pr = eval_segment_pr(gt_segs, model_segs, segment_cost, DEFAULT_TOLS)
    print("tolerance sweep：")
    print(f"{'tol(mm)':>8} {'TP':>5} {'FP':>5} {'FN':>5} {'Precision':>10} {'Recall':>10} {'F1':>8}")
    for s in pr["sweep"]:
        print(f"{s['tol']:>8.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>10.1%} {s['recall']:>10.1%} {s['f1']:>8.1%}")
    print()

    # 主容差下的通过判定
    matched, un_gt, un_m = hungarian_match(gt_segs, model_segs, segment_cost, max_cost=args.tol)
    tp = len(matched)
    fp = len(un_m)
    fn = len(un_gt)
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    print(f"主容差 {args.tol:.0f}mm：TP={tp} FP={fp} FN={fn} "
          f"P={p:.1%} R={r:.1%}（目标 P≥85% 且 R≥85%）")
    ok = p >= 0.85 and r >= 0.85
    print(f"达标：{'✓' if ok else '✗（未达标）'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
