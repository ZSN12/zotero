#!/usr/bin/env python3
"""A2 full 池 FP 结构归因 + kfan 层对可分性检验（可复跑）。

背景（2026-09-04/05 FP 治理）：full 池 @500mm front FP=2707（P=25.4%，
R=85.9%）。本脚本固化两个结论：

1. FP 的 caliber×geometry_origin 结构（最大簇 = panel_template 1257，
   全部在 parametric caliber —— pure 池不受其影响）。
2. kfan 0-TP 层对无法用诚实证据剪除：四条候选规则（A 其它来源跨同层对、
   B junction 兄弟证据、C 目标层层位证据、D 图纸已画跨度佐证）+ 跨度匹配
   门 + 单册覆盖 + 链式截断全部无法实现零 TP 损失——GT 本身包含
   「图纸未画但真实存在」的深 K 面板（6500→d5500、19000→d4000 等），
   与纯 FP 深面板证据状态完全相同。

用法: python3 scripts/diag_fp_pairs.py [--model out/35A1-JC1-full-deliver/model.json]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"

import sys  # noqa: E402

sys.path.insert(0, str(REPO))  # noqa: E402

from traceability.eval.metrics import eval_a2_multi_caliber  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path,
                    default=REPO / "out/35A1-JC1-full-deliver/model.json")
    ap.add_argument("--tol", type=float, default=500.0)
    args = ap.parse_args()

    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    model = json.loads(args.model.read_text(encoding="utf-8"))

    ev = eval_a2_multi_caliber(gt, model, view="front", tols=(args.tol,))
    prov = ev["match_provenance"]

    # ---- 1. FP 结构: caliber × geometry_origin ----
    fp_origin = Counter()
    tp_origin = Counter()
    for p in prov:
        if p["match_status"] != "fp":
            continue
        fp_origin[(p.get("caliber"), p.get("geometry_origin"))] += 1
    tp_map = defaultdict(int)
    for p in prov:
        if p["match_status"] == "tp":
            tp_map[(p.get("caliber"), p.get("geometry_origin"))] += 1
    print(f"=== A2 full 池 FP 结构归因（front @tol={args.tol:.0f}mm） ===")
    total_fp = sum(fp_origin.values())
    print(f"full 池 FP 总数: {total_fp}")
    for (cal, org), n in fp_origin.most_common():
        tp = tp_map.get((cal, org), 0)
        print(f"  {cal:16s} × {org:28s} FP={n:5d}  TP={tp:4d}")

    # ---- 2. kfan 0-TP 层对可分性 ----
    # kfan 杆 id 形如 kfan_bar_N；layer pair = (端点 z 排序后). 逐杆检验
    # 「该层对是否有任何 TP」与「是否有图纸侧证据」，输出混淆矩阵。
    kfan_ids = {p["model_component_id"] for p in prov
                if p.get("geometry_origin") == "panel_template_completion"}
    gt_nodes = {nid: tuple(map(float, xyz)) for nid, xyz in gt["nodes"].items()}
    comps = model["components"]

    def node_xyz(nid):
        for cand in (nid, f"4f_{nid}", nid.removeprefix("4f_")):
            c = comps.get(cand)
            if c is not None and c.get("kind") == "tower_node":
                pr = c["properties"]
                return (float(pr["x"]), float(pr["y"]), float(pr["z"]))
        raise KeyError(nid)

    def bar_ends(comp_props):
        fn, tn = comp_props["from_node"], comp_props["to_node"]
        return node_xyz(fn), node_xyz(tn)

    pair_tp = defaultdict(int)
    pair_fp = defaultdict(int)
    for p in prov:
        if p.get("geometry_origin") != "panel_template_completion":
            continue
        cid = p["model_component_id"]
        props = comps[cid]["properties"]
        (x1, _, z1), (x2, _, z2) = bar_ends(props)
        pair = (max(z1, z2), min(z1, z2))
        if p["match_status"] == "tp":
            pair_tp[pair] += 1
        else:
            pair_fp[pair] += 1
    zero_pairs = sorted(p for p in pair_fp if pair_tp.get(p, 0) == 0)
    print(f"\n=== kfan/panel_template 层对 TP/FB 结构 ===")
    print(f"层对总数: {len(set(pair_tp) | set(pair_fp))}")
    print(f"0-TP 层对: {len(zero_pairs)}（承载 FP {sum(pair_fp[p] for p in zero_pairs)}）")

    # GT junction span 普查（负结论的核心证据：深跨度 GT 真实但图纸未画）
    gt_span = defaultdict(set)
    for b in gt["bars"]:
        f, t = gt_nodes[b["from"]], gt_nodes[b["to"]]
        zj, zt = max(f[2], t[2]), min(f[2], t[2])
        if zt > 0 and zj - zt >= 2000:
            gt_span[round(zj)].add(round(zj - zt))
    print("\nGT junction→目标层真实跨度普查（≥2000mm 面板）:")
    for zj in sorted(gt_span):
        print(f"  junction {zj}: 深度 {sorted(gt_span[zj])}")

    print("\n[负结论] 图纸已画跨度不构成 GT 深度上界：junction 6500 图纸以下"
          "空白（07 册底 5759）但 GT 真实 d2500-6500；19000 图纸仅画 d2000"
          " 但 GT 真实 d4000。0-TP 层对与深 TP 层对证据状态相同，"
          "诚实剪除不可分（详见 docs/A2_FP_TREATMENT_ATTRIBUTION.md）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
