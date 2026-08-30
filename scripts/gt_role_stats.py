#!/usr/bin/env python3
"""GT 角色口径权威统计（P0-3：钉死分母）。

矛盾来源：UNIMPLEMENTED_PLAN.md 一处写 1071 = 横隔 295 + 斜材 520 + 主腿 252，
另一处写斜材 728（占 68%）。本脚本给出两套口径的权威数字：

1. 3D 全量：GT bars 按几何角色分类（无投影去重）
2. front 2D 投影：与 eval/metrics.py A2 相同的投影去重口径

角色分类规则（与 metrics.py 评测谓词一致）：
    horizontal: |dz| < 50mm（水平杆）
    leg:        近竖直（|dx|/|dz| 小）且较长
    diagonal:   其余斜向杆

用法：
    python3 scripts/gt_role_stats.py [gt.json]
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

DEFAULT_GT = "examples/gt/35A1-JC1_ground_truth.json"


def classify(p1, p2) -> str:
    dx = abs(p1[0] - p2[0])
    dy = abs(p1[1] - p2[1])
    dz = abs(p1[2] - p2[2])
    if dz < 50.0 and dx > 1e-6:
        return "horizontal"
    if dz < 50.0 and dx <= 1e-6 and dy > 50.0:
        return "y_member"  # Y 方向杆（front 投影退化为点，A2 无法匹配）
    if dz >= 50.0 and dx / max(dz, 1e-9) < 0.10:
        return "leg"
    return "diagonal"


def main() -> int:
    gt_path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GT)
    gt = json.load(open(gt_path))
    nodes = gt["nodes"]  # {id: [x, y, z]}

    # ---- 口径 1：3D 全量 ----
    c3d = Counter()
    len_by_role = {"horizontal": [], "leg": [], "diagonal": [], "y_member": []}
    for b in gt["bars"]:
        p1, p2 = nodes[b["from"]], nodes[b["to"]]
        role = classify(p1, p2)
        c3d[role] += 1
        len_by_role[role].append(
            math.dist(p1, p2)
        )
    total3d = sum(c3d.values())

    # ---- 口径 2：front 投影去重（A2 评测口径）----
    # 复刻 metrics.py 的投影：front (x, z) 平面，端点对排序去重
    seen = set()
    c2d = Counter()
    len2d_by_role = {"horizontal": [], "leg": [], "diagonal": [], "y_member": []}
    for b in gt["bars"]:
        p1, p2 = nodes[b["from"]], nodes[b["to"]]
        a = (p1[0], p1[2])
        b2 = (p2[0], p2[2])
        key = tuple(sorted((a, b2)))
        if key in seen:
            continue
        seen.add(key)
        role = classify(p1, p2)
        c2d[role] += 1
        len2d_by_role[role].append(math.hypot(a[0] - b2[0], a[1] - b2[1]))
    total2d = sum(c2d.values())

    print(f"GT 权威口径（{gt_path.name}）")
    print(f"\n== 口径1：3D 全量（{total3d} 根）==")
    for role in ("leg", "diagonal", "horizontal", "y_member"):
        ls = sorted(len_by_role[role])
        if ls:
            print(
                f"  {role:11} {c3d[role]:5} 根  "
                f"长度 min/med/max = {ls[0]:.0f}/{ls[len(ls)//2]:.0f}/{ls[-1]:.0f} mm"
            )
    print(f"\n== 口径2：front 投影去重（{total2d} 根，A2 分母）==")
    for role in ("leg", "diagonal", "horizontal", "y_member"):
        ls = sorted(len2d_by_role[role])
        if ls:
            print(
                f"  {role:11} {c2d[role]:5} 根  "
                f"长度 min/med/max = {ls[0]:.0f}/{ls[len(ls)//2]:.0f}/{ls[-1]:.0f} mm"
            )
    print(f"\n== 斜材细分（front 投影，按长度桶）==")
    diag = sorted(len2d_by_role["diagonal"])
    buckets = [("<200", 0, 200), ("200-500", 200, 500), ("500-1500", 500, 1500),
               ("1500-3000", 1500, 3000), ("3000-6000", 3000, 6000), (">=6000", 6000, 1e9)]
    for name, lo, hi in buckets:
        n = sum(1 for L in diag if lo <= L < hi)
        print(f"  {name:10} {n:4} 根")

    # GT 横隔层（水平杆 z 聚类）
    hz = []
    for b in gt["bars"]:
        p1, p2 = nodes[b["from"]], nodes[b["to"]]
        if abs(p1[2] - p2[2]) < 50:
            hz.append((p1[2] + p2[2]) / 2)
    buckets_z = {}
    for z in sorted(hz):
        placed = False
        for bz in buckets_z:
            if abs(bz - z) <= 500:
                buckets_z[bz].append(z)
                placed = True
                break
        if not placed:
            buckets_z[z] = [z]
    layers = sorted(sum(bs) / len(bs) for bs in buckets_z.values())
    print(f"\n== GT 横隔层（水平杆 z 聚类 500mm）==")
    print(f"  层数 = {len(layers)}")
    print(f"  z = {[f'{z:.0f}' for z in layers]}")

    # 输出 JSON 供其它脚本引用
    out = {
        "gt_file": str(gt_path),
        "total_3d": total3d,
        "roles_3d": dict(c3d),
        "total_front_dedup": total2d,
        "roles_front": dict(c2d),
        "diaphragm_layers": [round(z, 1) for z in layers],
    }
    out_path = Path("out/gt_role_stats.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(out_path, "w"), ensure_ascii=False, indent=2)
    print(f"\n已写出 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
