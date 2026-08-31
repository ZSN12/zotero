#!/usr/bin/env python3
"""Phase 2.1：悬空节点（Degree=1）分类器——52 个 genuine dangling 的逐节点归因。

背景（2026-08-31 review）：交付门禁 genuine_dangling=52（要求 <=4）。
不能直接放宽 max_degree1，也不能把所有 Degree=1 都标成横担端头——
必须先逐节点分类，只修「真实可解释的断裂」。

分类体系（用户裁定 Phase 2）：
    crossarm_tip          横担悬臂端头（合法自由端，role=CROSS 或径向>1.4×半宽）
    short_noise_stub      短线残根（杆长 < 600mm 的孤立碎段，图纸噪声/螺栓细节）
    diagonal_endpoint_gap 斜材端点偏离主腿（唯一杆是斜材且另一端靠近腿工作线）
    horizontal_endpoint_gap 水平材端点无连接（唯一杆近水平且端点悬空）
    leg_break             主腿节间断裂（唯一杆是腿且两端 z 跨段界）
    module_boundary_gap   跨段接口断裂（节点 z 落在段边界 ±300mm 内）
    unknown               未知（进 review_queue，不允许自动修复）

用法：
    python3 scripts/classify_dangling_nodes.py [model.json]
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def classify_dangling_nodes(model: dict) -> list[dict]:
    """逐节点分类 Degree=1 悬空节点。返回按 z 排序的记录列表。"""
    comps = model.get("components", {})
    nodes = {}
    for cid, c in comps.items():
        if not isinstance(c, dict) or c.get("kind") != "tower_node":
            continue
        p = c.get("properties", {}) or {}
        if all(p.get(a) is not None for a in ("x", "y", "z")):
            nodes[cid] = (float(p["x"]), float(p["y"]), float(p["z"]))
    bars = []
    for cid, c in comps.items():
        if not isinstance(c, dict) or c.get("kind") != "tower_bar":
            continue
        p = c.get("properties", {}) or {}
        if p.get("from_node") in nodes and p.get("to_node") in nodes:
            bars.append((cid, p))

    # 度数统计（物理杆）
    degree = defaultdict(int)
    bar_at = {}
    for cid, p in bars:
        f, t = p.get("from_node"), p.get("to_node")
        degree[f] += 1
        degree[t] += 1
        bar_at.setdefault(f, (cid, p))
        bar_at.setdefault(t, (cid, p))

    # 腿工作线（front 投影 |x| 随 z 的包络，取每 1000mm 桶的 max |x|）
    leg_env = defaultdict(float)
    for cid, p in bars:
        role = str(p.get("role") or "").upper()
        if role != "LEG":
            continue
        f, t = nodes[p["from_node"]], nodes[p["to_node"]]
        z0, z1 = sorted((f[2], t[2]))
        x_env = max(abs(f[0]), abs(t[0]))
        for zb in range(int(z0 // 1000), int(z1 // 1000) + 1):
            leg_env[zb] = max(leg_env[zb], x_env)

    # 段边界（z_offset 配置）
    seg_bounds = {6643, 12143, 17143, 24143, 30943, 30000, 36600, 6500}

    records = []
    for nid, d in sorted(degree.items()):
        if d != 1 or nid not in nodes:
            continue
        p = nodes[nid]
        cid, bp = bar_at[nid]
        f, t = nodes[bp["from_node"]], nodes[bp["to_node"]]
        length = math.dist(f, t)
        role = str(bp.get("role") or "").upper()
        radial = math.hypot(p[0], p[1])
        z = p[2]
        other = t if bp["from_node"] == nid else f

        # ---- 分类规则（优先级从高到低）----
        hw = leg_env.get(int(z // 1000), 0.0)
        if role == "CROSS" or (hw > 0 and radial > hw * 1.4):
            cls = "crossarm_tip"
        elif length < 600.0:
            cls = "short_noise_stub"
        else:
            dz = abs(f[2] - t[2])
            near_boundary = any(abs(z - b) < 300.0 for b in seg_bounds)
            other_hw = leg_env.get(int(other[2] // 1000), 0.0)
            dist_to_leg = abs(abs(other[0]) - other_hw) if other_hw else 9999.0
            if role == "LEG":
                cls = "leg_break"
            elif dz < 200.0:
                cls = "horizontal_endpoint_gap"
            elif near_boundary:
                cls = "module_boundary_gap"
            elif dist_to_leg < 400.0:
                cls = "diagonal_endpoint_gap"
            else:
                cls = "unknown"

        records.append({
            "node_id": nid,
            "z": round(z, 1),
            "role": role,
            "bar_id": bp.get("bar_id") or "",
            "component_id": cid,
            "length_mm": round(length, 1),
            "distance_to_leg_mm": round(
                abs(abs(other[0]) - leg_env.get(int(other[2] // 1000), 0.0))
                if leg_env.get(int(other[2] // 1000)) else -1, 1),
            "source_sheet": str(bp.get("source_file") or "")[:18],
            "is_crossarm_tip": cls == "crossarm_tip",
            "classification": cls,
        })
    records.sort(key=lambda r: r["z"])
    return records


def main() -> int:
    model_path = sys.argv[1] if len(sys.argv) > 1 else \
        "out/35A1-JC1-full-deliver/model.json"
    model = json.loads(Path(model_path).read_text(encoding="utf-8"))
    records = classify_dangling_nodes(model)

    hist = Counter(r["classification"] for r in records)
    print(f"模型: {model_path}")
    print(f"Degree=1 节点总数: {len(records)}")
    print("\n分类统计:")
    for cls, n in hist.most_common():
        print(f"  {cls:>26}: {n}")
    print(f"\n可修复类（diagonal/horizontal/leg/module gap）: "
          f"{sum(hist[c] for c in ('diagonal_endpoint_gap', 'horizontal_endpoint_gap', 'leg_break', 'module_boundary_gap'))}")
    print(f"合法横担端头: {hist.get('crossarm_tip', 0)}")
    print(f"短噪声残根: {hist.get('short_noise_stub', 0)}")
    print(f"unknown（进 review_queue）: {hist.get('unknown', 0)}")

    print("\n逐节点明细（非 crossarm_tip）:")
    for r in records:
        if r["classification"] == "crossarm_tip":
            continue
        print(f"  z={r['z']:>8.1f}  {r['classification']:>26}  {r['role']:>5} "
              f"L={r['length_mm']:>7.1f}mm d_leg={r['distance_to_leg_mm']:>7.1f} "
              f"src={r['source_sheet']}")

    out = Path(model_path).parent / "dangling_node_classification.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n明细已写: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
