#!/usr/bin/env python3
"""P0 补充：Ground Truth 3D 评测 —— 对比完整 3D 模型 vs 3D GT（1071 杆）。

现有 evaluate_ground_truth.py 只对比「front 投影」（GT 投影后 369 杆），
但目标是对齐 GT 完整 3D 结构（1071 杆 / 358 节点 / Z 0-36600mm）。
本脚本按 3D 线段中点距离 + 方向夹角做贪心匹配，输出真实 3D Precision/Recall，
并按杆件类型（主腿 / 腹杆 / 横隔面）细分召回缺口，方便定位优化方向。

用法：
    python3 scripts/evaluate_ground_truth_3d.py <gt.json> <model.json> [--tol 800]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple


Vec3 = Tuple[float, float, float]


def load_gt(gt_path: Path) -> dict:
    return json.loads(Path(gt_path).read_text(encoding="utf-8"))


def load_model(model_path: Path) -> dict:
    return json.loads(Path(model_path).read_text(encoding="utf-8"))


def gt_bars_3d(gt: dict) -> List[Tuple[Vec3, Vec3, str, str]]:
    """GT 3D 杆件：((x1,y1,z1),(x2,y2,z2),bar_id,section)。"""
    nodes = gt["nodes"]
    out = []
    for b in gt["bars"]:
        f = nodes.get(b["from"])
        t = nodes.get(b["to"])
        if f is None or t is None:
            continue
        out.append((tuple(f), tuple(t), b["id"], b.get("section", "")))
    return out


def model_bars_3d(model: dict) -> List[Tuple[Vec3, Vec3, str]]:
    """模型 3D 杆件：((x1,y1,z1),(x2,y2,z2),comp_id)。"""
    comps = model.get("components", {})
    nodes: Dict[str, Vec3] = {}
    for cid, c in comps.items():
        if c.get("kind") == "tower_node":
            p = c.get("properties", {})
            if all(p.get(a) is not None for a in ("x", "y", "z")):
                nodes[cid] = (p["x"], p["y"], p["z"])
    out = []
    for cid, c in comps.items():
        if c.get("kind") != "tower_bar":
            continue
        p = c.get("properties", {})
        f, t = p.get("from_node"), p.get("to_node")
        if f in nodes and t in nodes:
            out.append((nodes[f], nodes[t], cid))
    return out


def _seglen(b: Tuple[Vec3, Vec3]) -> float:
    return math.sqrt(sum((b[1][i] - b[0][i]) ** 2 for i in range(3)))


def _mid(b: Tuple[Vec3, Vec3]) -> Vec3:
    return tuple((b[0][i] + b[1][i]) / 2 for i in range(3))


def _unit_dir(b: Tuple[Vec3, Vec3]) -> Vec3:
    d = [b[1][i] - b[0][i] for i in range(3)]
    L = math.sqrt(sum(x * x for x in d))
    return tuple(x / L for x in d) if L > 1e-9 else (0.0, 0.0, 0.0)


def _angle_diff(a: Vec3, b: Vec3) -> float:
    dot = sum(a[i] * b[i] for i in range(3))
    return math.degrees(math.acos(max(-1.0, min(1.0, abs(dot)))))


def classify(gt_bar: Tuple[Vec3, Vec3, str, str]) -> str:
    """按方向分类 GT 杆件：leg(近垂直) / horizontal(近水平) / diagonal(斜)。"""
    b = (gt_bar[0], gt_bar[1])
    L = _seglen(b)
    if L < 1e-6:
        return "degenerate"
    dz = abs(b[1][2] - b[0][2]) / L
    if dz > 0.85:
        return "leg"
    if dz < 0.3:
        return "horizontal"
    return "diagonal"


def match_3d(gt_bars, model_bars, tol: float, angle_tol_deg: float = 30.0):
    matched = []
    used = set()
    for gi, g in enumerate(gt_bars):
        gm = _mid((g[0], g[1]))
        gd = _unit_dir((g[0], g[1]))
        best_j, best_d = None, tol
        for mj, m in enumerate(model_bars):
            if mj in used:
                continue
            mm = _mid((m[0], m[1]))
            d = math.sqrt(sum((gm[i] - mm[i]) ** 2 for i in range(3)))
            ad = _angle_diff(gd, _unit_dir((m[0], m[1])))
            if d < best_d and ad < angle_tol_deg:
                best_d, best_j = d, mj
        if best_j is not None:
            matched.append((gi, best_j))
            used.add(best_j)
    return matched, used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt", help="GT json 路径")
    ap.add_argument("model", help="管线输出 model.json")
    ap.add_argument("--tol", type=float, default=800.0, help="3D 中点匹配容差 mm")
    ap.add_argument("--angle-tol", type=float, default=30.0, help="方向夹角容差 °")
    args = ap.parse_args()

    gt_bars = gt_bars_3d(load_gt(args.gt))
    model_bars = model_bars_3d(load_model(args.model))

    if not model_bars:
        print("⚠ 模型无可用 3D 杆件坐标")
        return

    matched, used = match_3d(gt_bars, model_bars, args.tol, args.angle_tol)
    tp = len(matched)
    fp = len(model_bars) - len(used)
    fn = len(gt_bars) - tp
    n_gt, n_model = len(gt_bars), len(model_bars)
    precision = tp / n_model if n_model else 0.0
    recall = tp / n_gt if n_gt else 0.0

    print("=== Ground Truth 3D 评测（完整 3D 模型 vs 3D GT）===")
    print(f"GT 3D 杆件: {n_gt}")
    print(f"模型 3D 杆件: {n_model}")
    print(f"匹配 (TP): {tp}")
    print(f"误报 (FP): {fp} | 漏检 (FN): {fn}")
    print(f"Precision: {precision:.1%}")
    print(f"Recall:    {recall:.1%}")
    print()

    # 按杆件类型细分召回
    from collections import Counter
    matched_gt = {gi for gi, _ in matched}
    by_type = Counter()
    by_type_missed = Counter()
    for gi, g in enumerate(gt_bars):
        t = classify(g)
        by_type[t] += 1
        if gi not in matched_gt:
            by_type_missed[t] += 1
    print("按杆件类型的 GT 构成与召回缺口：")
    for t in ("leg", "diagonal", "horizontal", "degenerate"):
        total = by_type.get(t, 0)
        missed = by_type_missed.get(t, 0)
        if total:
            print(f"  {t:12s}: 共 {total:4d} 根，漏检 {missed:4d} 根（召回 {(total-missed)/total:.1%}）")

    # 杆长对比
    import statistics
    gl = [_seglen((g[0], g[1])) for g in gt_bars]
    ml = [_seglen((m[0], m[1])) for m in model_bars]
    print(f"\nGT 杆长: min {min(gl):.0f} / median {statistics.median(gl):.0f} / max {max(gl):.0f} mm")
    print(f"模型杆长: min {min(ml):.0f} / median {statistics.median(ml):.0f} / max {max(ml):.0f} mm")


if __name__ == "__main__":
    main()
