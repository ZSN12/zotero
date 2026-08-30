#!/usr/bin/env python3
"""口径矩阵实验：{单面, 4面} × {不拼接, 贪心拼接, 并查集拼接} —— 用真实 JC1 数据选路。

背景（2026-08-31 诊断，两处口径/根因修正）
------------------------------------------
1. **评测口径不对称 4 倍**：GT front 投影 1071 根是**全塔 4 个面**的杆件投影到
   (x,z)；而 `bars_from_model_2d(view="front")` 通过 `_face_to_view` 只放行
   `face=f`（b/l/r → side 被过滤），模型等于只投了 **1 个面**。
   两侧不对称导致召回被结构性压低。本脚本给出 4 面全投影的对称口径。

2. **碎片化假设只对了一半**：已匹配的 188 对中，长度比中位 **1.06**、端点 Z 偏差
   中位 **0mm**、X 偏差中位 25mm——能匹配上的杆**端点位置是准的**。真正的问题是
   杆件**数量不足**且**缺少长杆**（模型 >3m 仅 10 根，GT 336 根）。这也是
   「端点吸附 / 共线拼接」在单面口径下均无正收益的原因：移动端点反而破坏已有的
   精确定位。

本脚本**不修改生产口径**，只给实验数据——口径是否切换由人决策。

用法
----
    python3 scripts/experiment_caliber_matrix.py <model.json> <gt.json> [--gap 80] [--ang 5]

输出
----
    每个组合的 n / TP@500 / Recall / Precision，以及相对基线的最优增益组合。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import (  # noqa: E402
    DEFAULT_TOLS, gt_bars_2d, hungarian_match, segment_cost,
    is_physical_bar, is_recognized_bar,
)
from scripts.experiment_collinear_stitch import stitch_model  # noqa: E402


def collect_projected(
    model: Dict[str, Any],
    filter_fn: Callable[[dict], bool],
    faces: Optional[Sequence[str]],
) -> List[Tuple[tuple, dict]]:
    """按 face 集合收集模型杆件并投影到 front (x, z)。

    faces=None 表示不按 face 过滤（4 面全投影）；横隔始终纳入（与 GT 口径一致）。
    """
    comps = model.get("components", {})
    nodes = {k: v for k, v in comps.items() if v.get("kind") == "tower_node"}
    out: List[Tuple[tuple, dict]] = []
    for _cid, c in comps.items():
        if c.get("kind") != "tower_bar":
            continue
        p = c.get("properties", {})
        if not filter_fn(p):
            continue
        face = str(p.get("face") or "")
        is_dia = bool(p.get("diaphragm")) or face.lower() == "diaphragm"
        if faces is not None and not is_dia and face not in faces:
            continue
        nf = nodes.get(p.get("from_node"))
        nt = nodes.get(p.get("to_node"))
        if not nf or not nt:
            continue
        pf, pt = nf.get("properties", {}), nt.get("properties", {})
        try:
            s = (float(pf["x"]), float(pf["z"]), float(pt["x"]), float(pt["z"]))
        except (KeyError, TypeError, ValueError):
            continue
        if (s[0], s[1]) > (s[2], s[3]):
            s = (s[2], s[3], s[0], s[1])
        out.append((s, p))
    return out


def evaluate(gt_segs: List[tuple], model_segs: List[tuple],
             tols: Sequence[float]) -> Dict[float, int]:
    return {tol: len(hungarian_match(gt_segs, model_segs, segment_cost,
                                     float(tol))[0]) for tol in tols}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("gt")
    ap.add_argument("--gap", type=float, default=80.0)
    ap.add_argument("--ang", type=float, default=5.0)
    ap.add_argument("--max-merged-len", type=float, default=4500.0)
    ap.add_argument("--max-segments", type=int, default=3)
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

    g = gt_bars_2d(gt, "front")
    gt_segs = [s for s, _, _ in g]
    n_gt = len(gt_segs)
    print(f"GT front 投影杆件: {n_gt}（全塔 4 面）")
    print(f"拼接判据：gap<={args.gap}mm 夹角<={args.ang}° "
          f"合成长度<={args.max_merged_len:.0f}mm 最多{args.max_segments}段\n")

    # 预生成拼接模型（3D 后处理，各口径共用）
    variants: Dict[str, Dict[str, Any]] = {"不拼接": model}
    for mode, label in (("greedy", "贪心拼接"), ("union_find", "并查集拼接")):
        sm, st = stitch_model(
            model, gap_mm=args.gap, ang_deg=args.ang, mode=mode,
            max_merged_len=args.max_merged_len, max_segments=args.max_segments)
        variants[label] = sm
        print(f"[{label}] 杆件 {st['n_bars_in']} → {st['n_bars_out']}"
              f"（合并组 {st['merged_groups']}，碎片中位 {st['len_before_median']}mm"
              f" → 合成 {st['len_after_median']}mm）")
    print()

    scopes = [
        ("单面 face=f（现行口径）", ["f"]),
        ("4 面全投影（对称口径）", None),
    ]
    modes = [("physical", is_physical_bar), ("recognition", is_recognized_bar)]

    print(f"{'拼接':<10} {'口径':<22} {'模式':<12} {'n':>6} {'TP@500':>7} "
          f"{'Recall':>8} {'Prec':>7}")
    print("-" * 78)
    best = None
    for vname, mdl in variants.items():
        for sname, faces in scopes:
            for modename, fn in modes:
                bars = collect_projected(mdl, fn, faces)
                if not bars:
                    continue
                segs = [s for s, _ in bars]
                res = evaluate(gt_segs, segs, DEFAULT_TOLS)
                tp = res[500.0]
                r, p = tp / n_gt, tp / len(segs)
                print(f"{vname:<10} {sname:<22} {modename:<12} {len(segs):>6} "
                      f"{tp:>7} {r:>8.1%} {p:>7.1%}")
                if best is None or tp > best[0]:
                    best = (tp, vname, sname, modename, len(segs), r, p)
    print("-" * 78)
    if best:
        tp, v, s, mo, n, r, p = best
        print(f"最优 TP 组合：{v} × {s} × {mo} → TP={tp}（R {r:.1%} / P {p:.1%}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
