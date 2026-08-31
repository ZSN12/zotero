#!/usr/bin/env python3
"""诊断：拼接生产化后 TP 208→188 的损失归因。

对比 /tmp/model_baseline.json（无拼接）与 out/35A1-JC1-full-deliver/model.json
（生产拼接），找出「基线 TP@500 → 拼接后 FN」的 GT 杆，并定位它们在两个模型里
对应的模型杆，判断损失机制。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import (  # noqa: E402
    bars_from_model_2d,
    gt_bars_2d,
    hungarian_match,
    segment_cost,
)

GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"
BASELINE = Path("/tmp/model_baseline.json")
STITCHED = REPO / "out/35A1-JC1-full-deliver/model.json"
TOL = 500.0


def match_at(model_path: Path):
    gt = json.load(open(GT_PATH))
    model = json.load(open(model_path))
    g = gt_bars_2d(gt, "front")
    m = bars_from_model_2d(model, view="front", mode="physical")
    matched, un_gt, un_m = hungarian_match(
        [s for s, _, _ in g], [s for s, _ in m], segment_cost, max_cost=TOL)
    return g, m, matched

g, mb, matched_b = match_at(BASELINE)
_, ms, matched_s = match_at(STITCHED)

gt_hit_b = {gi for gi, _ in matched_b}
gt_hit_s = {gi for gi, _ in matched_s}

lost = sorted(gt_hit_b - gt_hit_s)
gained = sorted(gt_hit_s - gt_hit_b)
print(f"baseline TP@500 = {len(gt_hit_b)}, stitched TP@500 = {len(gt_hit_s)}")
print(f"lost {len(lost)} GT bars, gained {len(gained)}")
print()

mb_by_gi = {}
for gi, mj in matched_b:
    mb_by_gi[gi] = mj

print("=== LOST（基线 TP → 拼接 FN）===")
for gi in lost:
    seg, gid, role = g[gi]
    (x1, y1, x2, y2) = seg
    mj = mb_by_gi.get(gi)
    if mj is not None:
        ms_seg, ms_props = mb[mj]
        ms_id = ms_props.get("id") or ms_props.get("bar_id") or "?"
        p1 = (round(x1), round(y1)); p2 = (round(x2), round(y2))
        print(f"GT#{gi} role={role} len={round(((x2-x1)**2+(y2-y1)**2)**0.5)} "
              f"({p1})->({p2})")
        print(f"   基线模型杆: id={ms_id} origin={ms_props.get('geometry_origin')} "
              f"class={ms_props.get('geometry_class')} face={ms_props.get('face')} "
              f"len={round(((ms_seg[2]-ms_seg[0])**2+(ms_seg[3]-ms_seg[1])**2)**0.5)}")
    else:
        print(f"GT#{gi} role={role} (no baseline match??)")

print()
print("=== GAINED ===")
ms_by_gi = {gi: mj for gi, mj in matched_s}
for gi in gained:
    seg, gid, role = g[gi]
    mj = ms_by_gi[gi]
    ms_seg, ms_props = ms[mj]
    ms_id = ms_props.get("id") or ms_props.get("bar_id") or "?"
    print(f"GT#{gi} role={role} <- 模型杆 id={ms_id} "
          f"origin={ms_props.get('geometry_origin')} class={ms_props.get('geometry_class')}")
