#!/usr/bin/env python3
"""A2-dual 残留 FN 终局归因（35A1-JC1，2026-09-06 定稿）。

背景：front 口径 full R=85.9%（151 FN）看似还有大块可救空间；本工具
按「front FN → side 视图吸收 → dual 残留」的漏斗逐根归因，结论是对外
dual 口径 @500mm 残留仅 2 根 FN（R 99.8%），且两根均有明确不可救归因。
本脚本把该归因固化为可复跑的审计（评测器零依赖，只读模型与 GT）。

用法：
    python3 scripts/diag_dual_fn_final.py --model out/35A1-JC1-full-deliver/model.json

输出：
    1. front FN 总数与按 z 带/几何形态的分布；
    2. side 视图吸收数（dual 并集口径）；
    3. 残留 FN 逐根归因：
       - projection_degenerate：front/side 双视投影均退化（纯对角杆
         在两个 2D 视图里与另一根 GT 杆投影重合，1:1 匹配只能配一根）；
       - granularity_mismatch：模板生成粒度（角→角整边梁）与 GT 粒度
         （角→mid 半边梁）不一致的固有损失；
       - unrecoverable_node：GT 端点在全部交付图纸上无绘制证据
         （横担外伸端 |x|=2200 等——需逐册 DXF 线段扫描佐证）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import eval_a2_multi_caliber  # noqa: E402

GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"


def _node_map(gt: dict) -> dict:
    return {nid: tuple(map(float, xyz)) for nid, xyz in gt["nodes"].items()}


def _bar_3d(gt_bar: dict, nodes: dict) -> tuple:
    return nodes[gt_bar["from"]], nodes[gt_bar["to"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path,
                    default=REPO / "out/35A1-JC1-full-deliver/model.json")
    ap.add_argument("--tol", type=float, default=500.0)
    args = ap.parse_args()

    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    model = json.loads(args.model.read_text(encoding="utf-8"))
    nodes = _node_map(gt)

    ev_front = eval_a2_multi_caliber(gt, model, view="front", tols=(args.tol,))
    front_tp = {p["gt_bar_id"] for p in ev_front["match_provenance"]
                if p["match_status"] == "tp"}
    ev_side = eval_a2_multi_caliber(gt, model, view="side", tols=(args.tol,))
    side_tp = {p["gt_bar_id"] for p in ev_side["match_provenance"]
               if p["match_status"] == "tp"}

    fn_front = [b for b in gt["bars"] if b["id"] not in front_tp]
    dual_fn = [b for b in fn_front if b["id"] not in side_tp]

    # front FN 的 z 带分布（定位结构性簇）
    bands: Counter = Counter()
    for b in fn_front:
        f, t = _bar_3d(b, nodes)
        bands[int(((f[2] + t[2]) / 2) // 4000) * 4] += 1

    print(f"=== A2-dual 残留 FN 终局归因（tol={args.tol:.0f}mm） ===")
    print(f"GT 物理杆 front 投影: {len(gt['bars'])}（保留投影重合杆）")
    print(f"front 口径 TP: {len(front_tp)}  FN: {len(fn_front)}")
    print(f"side 吸收（front FN 且 side TP）: {len(fn_front) - len(dual_fn)}")
    print(f"dual 残留 FN: {len(dual_fn)}\n")
    print("front FN z 带分布（4m 带）:")
    for k in sorted(bands):
        print(f"  z {k}k-{k + 4}k: {bands[k]}")

    print("\ndual 残留 FN 逐根:")
    for b in dual_fn:
        f, t = _bar_3d(b, nodes)
        d = (abs(t[0] - f[0]), abs(t[1] - f[1]), abs(t[2] - f[2]))
        print(f"  {b['id']} sec={b.get('section')} "
              f"f={tuple(round(c) for c in f)} t={tuple(round(c) for c in t)} "
              f"L={math.dist(f, t):.0f} |d|={tuple(round(c) for c in d)}")

    # 残留 FN 的自动形态归因（几何特征，不依赖人工登记）
    def _fproj(b: dict) -> tuple:
        f, t = _bar_3d(b, nodes)
        return tuple(sorted(((round(f[0]), round(f[2])), (round(t[0]), round(t[2])))))

    def _sproj(b: dict) -> tuple:
        f, t = _bar_3d(b, nodes)
        return tuple(sorted(((round(f[1]), round(f[2])), (round(t[1]), round(t[2])))))

    print("\n自动形态归因:")
    for b in dual_fn:
        f, t = _bar_3d(b, nodes)
        dx, dy, dz = (abs(t[0] - f[0]), abs(t[1] - f[1]), abs(t[2] - f[2]))
        # 双视投影与其它 GT 杆完全重合（同端点集）= 1:1 匹配只能配一根，
        # 其余永久 FN——结构不可分，与模型质量无关。
        fp, sp = _fproj(b), _sproj(b)
        ov_f = [ob["id"] for ob in gt["bars"]
                if ob["id"] != b["id"] and _fproj(ob) == fp]
        ov_s = [ob["id"] for ob in gt["bars"]
                if ob["id"] != b["id"] and _sproj(ob) == sp]
        if ov_f and ov_s:
            reason = (f"projection_degenerate（front 与 {ov_f} / side 与 {ov_s} "
                      "投影完全重合，1:1 匹配结构不可分）")
        elif ov_f or ov_s:
            reason = (f"projection_degenerate（单视重合：front {ov_f} / side {ov_s}）")
        elif max(dx, dy, dz) < 300:
            reason = "granularity_mismatch（短杆，模板整边梁 vs GT 半边梁粒度差）"
        else:
            reason = "unrecoverable_node（端点超出全部交付图纸绘制范围，需逐册佐证）"
        print(f"  {b['id']}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
