#!/usr/bin/env python3
"""拼接参数网格搜索：在 4 面对称口径 + recognition 模式下，找最优拼接参数。

为什么认准 recognition 模式
--------------------------
矩阵实验（scripts/experiment_caliber_matrix.py）显示：
    * physical 口径下拼接反而降 TP（281→260）——因为拼接删掉的短碎片里，
      有一部分是能「碰巧」匹配 GT 短杆的；
    * 但 recognition 口径下贪心拼接让 TP 从 56 → 120（R 5.2% → 11.2%）。
recognition 才是「从图纸真实识别出多少」的口径，也是对外应汇报的口径，
因此参数寻优以它为准，同时监控 physical 口径不出现大幅回退。

用法
----
    python3 scripts/experiment_stitch_sweep.py <model.json> <gt.json>

输出
----
    按 recognition@500 TP 降序的参数表，并写出最优参数下的拼接模型。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import (  # noqa: E402
    DEFAULT_TOLS, gt_bars_2d, hungarian_match, segment_cost,
    is_physical_bar, is_recognized_bar,
)
from scripts.experiment_caliber_matrix import collect_projected  # noqa: E402
from scripts.experiment_collinear_stitch import stitch_model  # noqa: E402

# (gap_mm, ang_deg, max_merged_len, max_segments)
# 2026-08-31 第一轮：宽松方向单调增益，最优值落在网格边界（400/15°），
# 故第二轮继续外扩，直到增益不再增长或 physical 口径出现回退。
GRID = [
    (400.0, 15.0, 8000.0, 8),
    (600.0, 15.0, 8000.0, 8),
    (400.0, 20.0, 8000.0, 8),
    (600.0, 20.0, 10000.0, 10),
    (800.0, 20.0, 10000.0, 10),
    (800.0, 25.0, 12000.0, 12),
    (1000.0, 25.0, 12000.0, 12),
    (1200.0, 30.0, 15000.0, 15),
    (1600.0, 35.0, 18000.0, 20),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("gt")
    ap.add_argument("--out", default=None, help="写出最优参数下的拼接模型")
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))
    gt_segs = [s for s, _, _ in gt_bars_2d(gt, "front")]
    n_gt = len(gt_segs)

    def tp_of(mdl, fn) -> int:
        segs = [s for s, _ in collect_projected(mdl, fn, None)]
        return len(hungarian_match(gt_segs, segs, segment_cost, 500.0)[0]), len(segs)

    base_rec, n_rec = tp_of(model, is_recognized_bar)
    base_phys, n_phys = tp_of(model, is_physical_bar)
    print(f"基线（不拼接，4 面口径）：recognition TP={base_rec}(n={n_rec})  "
          f"physical TP={base_phys}(n={n_phys})\n")
    print(f"{'gap':>6} {'ang':>5} {'maxLen':>8} {'seg':>4} | "
          f"{'n_rec':>6} {'TP_rec':>7} {'Δ':>5} {'R':>7} | "
          f"{'n_phys':>7} {'TP_phys':>8} {'Δ':>5}")
    print("-" * 82)

    results = []
    for gap, ang, mml, mseg in GRID:
        sm, st = stitch_model(model, gap_mm=gap, ang_deg=ang, mode="greedy",
                              max_merged_len=mml, max_segments=mseg)
        tpr, nr = tp_of(sm, is_recognized_bar)
        tpp, nph = tp_of(sm, is_physical_bar)
        results.append((tpr, gap, ang, mml, mseg, nr, tpp, nph, sm, st))
        print(f"{gap:>6.0f} {ang:>5.1f} {mml:>8.0f} {mseg:>4d} | "
              f"{nr:>6} {tpr:>7} {tpr-base_rec:>+5d} {tpr/n_gt:>7.1%} | "
              f"{nph:>7} {tpp:>8} {tpp-base_phys:>+5d}")

    results.sort(key=lambda r: (-r[0], r[5]))
    best = results[0]
    tpr, gap, ang, mml, mseg, nr, tpp, nph, sm, st = best
    print("-" * 82)
    print(f"最优：gap={gap:.0f} ang={ang:.1f} maxLen={mml:.0f} seg<={mseg}")
    print(f"  recognition TP {base_rec} → {tpr} ({tpr-base_rec:+d}, R {tpr/n_gt:.1%})")
    print(f"  physical     TP {base_phys} → {tpp} ({tpp-base_phys:+d})")
    print(f"  杆件 {st['n_bars_in']} → {st['n_bars_out']}，"
          f"碎片中位 {st['len_before_median']}mm → 合成 {st['len_after_median']}mm")

    if args.out:
        Path(args.out).write_text(json.dumps(sm, ensure_ascii=False), encoding="utf-8")
        print(f"  最优模型已写出: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
