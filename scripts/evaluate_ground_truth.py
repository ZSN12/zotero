#!/usr/bin/env python3
"""P0：Ground Truth 评测 —— 把「自验证」换成「真实 Precision/Recall」。

读权威 GT（examples/gt/35A1-JC1_ground_truth.json，来自国网 GIM .mod）
和管线输出模型（model.json），计算：

    * 杆件 Precision / Recall（按几何位置匹配，容差 tol）
    * 件号 Exact Match（GT bar_id vs 模型 bar_id）
    * BOM 覆盖率（模型识别件号 ∩ GT 件号 / GT 件号）

关键：GT 是 3D（x/y/z mm），管线输出是 2D 立面重建（front: x→X, view_y→Z）。
评测只对比「同一投影」——front 立面用 (x, z)，side 用 (y, z)。

用法：
    python3 scripts/evaluate_ground_truth.py <gt.json> <model.json> [--tol 500]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def load_gt(gt_path: Path) -> dict:
    return json.loads(Path(gt_path).read_text(encoding="utf-8"))


def load_model(model_path: Path) -> dict:
    return json.loads(Path(model_path).read_text(encoding="utf-8"))


def gt_bars_2d(gt: dict, view: str) -> list:
    """把 GT 的 3D 杆件投影到 2D（front: (x,z)，side: (y,z)），并去重。

    返回 [(x1,z1,x2,z2,bar_id,section), ...]，坐标 mm。
    GT 是 3D 物理杆件，对称杆件投影到同一 2D 线段；这里按「端点排序后
    四舍五入」去重，避免同一条线被算成多根 GT 杆件，歪曲 Precision/Recall。
    """
    nodes = gt["nodes"]
    seen: set = set()
    bars = []
    for b in gt["bars"]:
        f = nodes.get(b["from"])
        t = nodes.get(b["to"])
        if f is None or t is None:
            continue
        if view == "front":
            x1, z1 = f[0], f[2]
            x2, z2 = t[0], t[2]
        elif view == "side":
            x1, z1 = f[1], f[2]
            x2, z2 = t[1], t[2]
        else:
            raise ValueError(f"未知视图 {view}")
        # 端点排序 + 取整去重（对称杆件投影重叠）
        if (x1, z1) > (x2, z2):
            x1, z1, x2, z2 = x2, z2, x1, z1
        key = (round(x1), round(z1), round(x2), round(z2))
        if key in seen:
            continue
        seen.add(key)
        bars.append((x1, z1, x2, z2, b["id"], b.get("section", "")))
    return bars


def model_bars_2d(model: dict, view: Optional[str] = None) -> list:
    """管线输出的杆件 2D 坐标（view_x/view_y 已标定 mm，否则回退 x/y）。

    view 非 None 时只取该 view_type 的杆件（front 评测不混入 side 杆件）。
    """
    comps = model.get("components", {})
    nodes = {cid: c for cid, c in comps.items() if c.get("kind") == "tower_node"}
    bars = []
    dedup: set = set()
    for cid, c in comps.items():
        if c.get("kind") != "tower_bar":
            continue
        p = c.get("properties", {})
        # view 过滤只对「未展开、带 view_type」的 2D 杆件生效；4-face 展开后的
        # 3D 杆件没有 view_type，直接参与评测（它们的 x/y/z 已是 3D 坐标）。
        vt = p.get("view_type")
        if view is not None and vt is not None and vt not in (view, None):
            continue
        f = p.get("from_node")
        t = p.get("to_node")
        nf = nodes.get(f) if f else None
        nt = nodes.get(t) if t else None
        if nf is None or nt is None:
            continue
        pf, pt = nf.get("properties", {}), nt.get("properties", {})
        # 4-face 展开后的 3D 模型没有 view_x/view_y（已替换为 3D x/y/z）。
        # front 投影取 (x, z)：x 是横向投影，z 是标高（y 是深度，投影时忽略）。
        # 仅当节点有 view_x/view_y（未展开的 2D 阶段）时用 view_x/view_y。
        if pf.get("view_x") is not None and pt.get("view_x") is not None:
            x1 = pf["view_x"]; y1 = pf.get("view_y", pf.get("y"))
            x2 = pt["view_x"]; y2 = pt.get("view_y", pt.get("y"))
        elif pf.get("z") is not None and pt.get("z") is not None:
            # 3D 模型：front 投影 (x, z)
            x1 = pf.get("x"); y1 = pf.get("z")
            x2 = pt.get("x"); y2 = pt.get("z")
        else:
            x1 = pf.get("x"); y1 = pf.get("y")
            x2 = pt.get("x"); y2 = pt.get("y")
        if None in (x1, y1, x2, y2):
            continue
        # 与 GT 侧对称地去重：4-face 展开后对称杆件投影到同一 2D 线段，
        # 端点排序 + 取整去重，避免 4 面对称杆被算成 4 根误报（FP 虚高）。
        if (x1, y1) > (x2, y2):
            x1, y1, x2, y2 = x2, y2, x1, y1
        key = (round(x1), round(y1), round(x2), round(y2))
        if key in dedup:
            continue
        dedup.add(key)
        bars.append((x1, y1, x2, y2, p.get("bar_id", ""), p.get("section", "")))
    return bars


def _seg_mid_dist(a, b) -> float:
    """两线段中点距离（mm）。"""
    ax = (a[0] + a[2]) / 2
    ay = (a[1] + a[3]) / 2
    bx = (b[0] + b[2]) / 2
    by = (b[1] + b[3]) / 2
    return math.hypot(ax - bx, ay - by)


def _seg_angle_diff(a, b) -> float:
    da = math.atan2(a[3] - a[1], a[2] - a[0])
    db = math.atan2(b[3] - b[1], b[2] - b[0])
    d = abs(da - db)
    return min(d, math.pi - d)


def match_bars(gt_bars, model_bars, tol: float):
    """贪心匹配：每根 GT 杆件找最近的模型杆件（中点距 + 角度差）。

    返回 (matched_pairs, unmatched_gt, unmatched_model)。
    """
    matched = []
    used_m = set()
    for gi, g in enumerate(gt_bars):
        best_j, best_d = None, tol
        for mj, m in enumerate(model_bars):
            if mj in used_m:
                continue
            d = _seg_mid_dist(g, m)
            ad = _seg_angle_diff(g, m)
            if d < best_d and ad < math.radians(30):
                best_d, best_j = d, mj
        if best_j is not None:
            matched.append((gi, best_j))
            used_m.add(best_j)
    unmatched_gt = [i for i in range(len(gt_bars)) if i not in {p[0] for p in matched}]
    unmatched_m = [i for i in range(len(model_bars)) if i not in used_m]
    return matched, unmatched_gt, unmatched_m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt", help="GT json 路径")
    ap.add_argument("model", help="管线输出 model.json")
    ap.add_argument("--view", choices=["front", "side"], default="front")
    ap.add_argument("--tol", type=float, default=500.0, help="中点匹配容差 mm")
    args = ap.parse_args()

    gt = load_gt(args.gt)
    model = load_model(args.model)

    gt_bars = gt_bars_2d(gt, args.view)
    model_bars = model_bars_2d(model, view=args.view)

    if not model_bars:
        print("⚠ 管线输出无可用杆件坐标（view_x/view_y 缺失）")
        return

    matched, un_gt, un_m = match_bars(gt_bars, model_bars, args.tol)

    n_gt = len(gt_bars)
    n_model = len(model_bars)
    tp = len(matched)
    fp = len(un_m)          # 模型多出的杆件（误报）
    fn = len(un_gt)         # 漏检的真实杆件

    precision = tp / n_model if n_model else 0.0
    recall = tp / n_gt if n_gt else 0.0

    print(f"=== Ground Truth 评测（{args.view} 投影）===")
    print(f"GT 物理杆件: {n_gt}")
    print(f"模型输出杆件: {n_model}")
    print(f"匹配 (TP): {tp}")
    print(f"误报 (FP): {fp} | 漏检 (FN): {fn}")
    print(f"Precision: {precision:.1%}")
    print(f"Recall:    {recall:.1%}")

    # 件号 Exact Match（在匹配对里，GT bar_id vs 模型 bar_id）
    exact = 0
    for gi, mj in matched:
        gid = gt_bars[gi][4]
        mid = model_bars[mj][4]
        if mid and not str(mid).startswith("UNLABELED") and gid == mid:
            exact += 1
    print(f"件号 Exact Match（匹配对中）: {exact}/{tp} = {exact/tp:.1%}" if tp else "件号 Exact Match: 0/0")


if __name__ == "__main__":
    main()
