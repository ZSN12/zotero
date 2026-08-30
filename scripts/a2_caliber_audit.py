#!/usr/bin/env python3
"""A2 口径审计脚本：四口径 × 角色分解（风险2/3 复核，2026-08-31）。

背景：A2 TP@500 从 46 → 188 的提升链路混杂了多个机制（S2a 方向修复、
S2b 横隔层 Z 对齐、S6 主腿节间化、A2-effective 口径），用户 review 要求
拆分归因并区分「纯 DXF 口径」与「GT canonical 标高辅助口径」。

用法：
    python3 scripts/a2_caliber_audit.py <model.json> <gt.json>

输出：
    * A2-full / A2-effective（z>=6500）双口径 TP@{50..500} 总量；
    * 按 GT 角色分解（leg / depth_diag / diagonal / horiz_x / y_member）
      的 TP 与召回（Hungarian 一对一，与正式评测同核）；
    * 模型侧 level_source / panel_subdivision 构成（GT 辅助成分透明化）。
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traceability.eval.metrics import (
    bars_from_model_2d,
    gt_bars_2d,
    hungarian_match,
    segment_cost,
)


def classify_gt_role_3d(p1, p2) -> str:
    """GT 3D 杆件角色（与 gt_role_stats.py 同一判据）。"""
    dx = abs(p1[0] - p2[0])
    dy = abs(p1[1] - p2[1])
    dz = abs(p1[2] - p2[2])
    if dz < 50.0:
        if dx > 50.0:
            return "horiz_x"
        if dy > 50.0:
            return "y_member"
        return "degenerate"
    if dx / max(dz, 1e-9) < 0.10 and dy / max(dz, 1e-9) < 0.10:
        return "leg"
    if dx / max(dz, 1e-9) < 0.10:
        return "depth_diag"
    return "diagonal"


def role_tp_breakdown(g, m, tol: float) -> dict:
    """Hungarian 一对一匹配后按 GT 角色统计 TP。"""
    gt_segs = [s for s, _, _ in g]
    model_segs = [s for s, _ in m]
    # hungarian_match 的门禁在 segment_cost 内（长度比/角度），此处
    # max_cost=tol 直接给容差上界（SUM≤tol，与正式评测同核）。
    pairs, _un_g, _un_m = hungarian_match(
        gt_segs, model_segs, segment_cost, max_cost=tol)
    tp_by_role = Counter()
    for gi, mj in pairs:
        # 角色已附在 g[gi][1]['_role']（3D 原杆判据）
        tp_by_role[g[gi][1].get("_role", "?")] += 1
    n_by_role = Counter(x[1].get("_role", "?") for x in g)
    return {
        "tol": tol,
        "tp_total": sum(tp_by_role.values()),
        "by_role": {r: {"tp": tp_by_role.get(r, 0), "n": n_by_role.get(r, 0)}
                    for r in ("leg", "depth_diag", "diagonal", "horiz_x", "y_member")},
    }


def main() -> int:
    model_path = Path(sys.argv[1] if len(sys.argv) > 1 else
                      "out/35A1-JC1-full-deliver/model.json")
    gt_path = Path(sys.argv[2] if len(sys.argv) > 2 else
                   "examples/gt/35A1-JC1_ground_truth.json")
    model = json.loads(model_path.read_text(encoding="utf-8"))
    gt = json.loads(gt_path.read_text(encoding="utf-8"))

    # GT front 投影 + 3D 角色标注（bar properties 附 _role）
    g_raw = gt_bars_2d(gt, "front")
    gnodes = gt["nodes"]
    g = []
    for seg, bar_id, section in g_raw:
        # 回查 3D 原杆判角色
        src = None
        for b in gt["bars"]:
            if str(b["id"]) == str(bar_id):
                src = b
                break
        if src is None:
            role = "?"
        else:
            role = classify_gt_role_3d(gnodes[src["from"]], gnodes[src["to"]])
        props = {"_role": role, "bar_id": bar_id}
        g.append((seg, props, section))

    m = bars_from_model_2d(model, view="front", mode="physical")

    print(f"模型: {model_path}")
    print(f"GT front 投影杆件: {len(g)}  模型物理杆件: {len(m)}")

    # 模型侧 GT 辅助成分透明化（风险3）
    comp = Counter()
    for _, p in m:
        if p.get("panel_subdivision"):
            comp["panel_subdivision"] += 1
        if p.get("level_source") == "gt_canonical":
            comp["diaphragm@gt_levels"] += 1
        elif p.get("level_source") == "dxf_derived":
            comp["diaphragm@dxf_levels"] += 1
        elif p.get("diaphragm"):
            comp["diaphragm@legacy_buckets"] += 1
        if p.get("geometry_class") == "recognized":
            comp["recognized"] += 1
        if p.get("geometry_class") == "mirrored":
            comp["mirrored"] += 1
    print(f"模型构成: {dict(comp)}")

    for scope_name, z_min in (("A2-full", None), ("A2-effective", 6500.0)):
        gg = [x for x in g if z_min is None or (x[0][1] + x[0][3]) / 2.0 >= z_min]
        mm = [x for x in m if z_min is None or (x[0][1] + x[0][3]) / 2.0 >= z_min]
        print(f"\n== {scope_name} (n_gt={len(gg)}, n_model={len(mm)}) ==")
        for tol in (50.0, 100.0, 200.0, 500.0):
            r = role_tp_breakdown(gg, mm, tol)
            rec = r["tp_total"] / max(len(gg), 1) * 100
            parts = " ".join(
                f"{k}:{v['tp']}/{v['n']}" for k, v in r["by_role"].items())
            print(f"  tol={tol:5.0f}  TP={r['tp_total']:4d}  R={rec:5.1f}%   [{parts}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
