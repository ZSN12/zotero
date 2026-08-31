#!/usr/bin/env python3
"""生成 review_queue.json：残留悬空节点人工复核清单（Phase 3 验收项）。

从 model.json 读取 genuine_dangling_nodes 明细 + 节点坐标，
计算断口距离（到最近的有效连接节点），输出建议动作。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def dist(a, b):
    return math.dist((a["x"], a["y"], a["z"]), (b["x"], b["y"], b["z"]))


def face_of(bar_id: str) -> str:
    for suffix in ("_F", "_B", "_L", "_R"):
        if bar_id.endswith(suffix):
            return suffix[1]
    return "?"


def stem_of(bar_id: str) -> str:
    """去掉面后缀得到物理杆标识（用于分组镜像实例）。"""
    for suffix in ("_F", "_B", "_L", "_R"):
        if bar_id.endswith(suffix):
            return bar_id[: -len(suffix)]
    return bar_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="out/35A1-JC1-full-deliver/model.json")
    ap.add_argument("--out", default="out/35A1-JC1-full-deliver/review_queue.json")
    args = ap.parse_args()

    model = json.loads(Path(args.model).read_text(encoding="utf-8"))
    comps = model["components"]
    df_props = comps["drawing_file"]["properties"]

    nodes = {}
    for cid, c in comps.items():
        if c.get("kind") == "tower_node":
            p = c["properties"]
            nodes[cid] = {"x": p["x"], "y": p["y"], "z": p["z"], "id": c["name"]}

    bars = {}
    for cid, c in comps.items():
        if c.get("kind") == "tower_bar":
            p = c["properties"]
            bars[c["name"]] = p

    details = df_props.get("genuine_dangling_nodes", [])
    degree = {}  # 节点度数
    for p in bars.values():
        for key in ("from_node", "to_node"):
            nid = p.get(key)
            if nid:
                degree[nid] = degree.get(nid, 0) + 1

    # 候选焊接目标：度数 >= 2 的节点（真连接点）
    anchors = {nid: n for nid, n in nodes.items() if degree.get(nid, 0) >= 2}

    entries = []
    for d in details:
        nid = d["id"]
        # genuine_dangling_nodes 的 id 是面模型节点名（如 N00097），
        # 交付模型组件键为 4f_N00097
        n = nodes.get(f"4f_{nid}") or nodes.get(nid)
        if n is None:
            continue
        # 断口距离：到最近锚点（排除同杆另一端已通过 bar 约束）
        best = None
        for aid, a in anchors.items():
            dd = dist(n, a)
            if best is None or dd < best[1]:
                best = (aid, dd)
        gap = round(best[1], 1) if best else None
        nearest = best[0] if best else None

        z = n["z"]
        role = d.get("role", "?")
        if gap is None or gap > 600:
            action = "manual_review_source_drawing"
            reason = "600mm 内无可焊节点，疑源图缺线（模块边界/图纸接缝）"
        elif gap > 350:
            action = "manual_review_source_drawing"
            reason = f"断口 {gap}mm 超出自动焊接半径 350mm，需人工确认"
        else:
            action = f"weld_to_{nearest}"
            reason = f"断口 {gap}mm 在可焊范围，建议人工确认后焊接"

        entries.append(
            {
                "node_id": nid,
                "xyz_mm": [round(n["x"], 1), round(n["y"], 1), round(n["z"], 1)],
                "face": face_of(d.get("bar_id", "")),
                "role": role,
                "bar_id": d.get("bar_id"),
                "gap_to_nearest_connected_node_mm": gap,
                "nearest_node_id": nearest,
                "suggested_action": action,
                "reason": reason,
                "z_mm": round(z, 1),
                "radial_mm": round(d.get("radial", 0), 1),
                "body_half_width_mm": round(d.get("body_half_width", 0), 1),
            }
        )

    # 按物理杆分组（镜像实例合并）
    groups = {}
    for e in entries:
        groups.setdefault(stem_of(e["bar_id"] or ""), []).append(e)

    queue = {
        "description": "悬空断裂节点人工复核清单（自动修复后残留）",
        "policy": "这些节点不计入 FP，标记为人工复核项（HANDOFF_PLAN Phase 3 约定）",
        "gate": {
            "genuine_dangling_instances": len(entries),
            "genuine_dangling_physical": len(groups),
            "gate_threshold": 4,
        },
        "repair_summary": df_props.get("dangling_repair_report", {}),
        "groups": [
            {
                "physical_bar_stem": stem,
                "instances": len(items),
                "faces": [it["face"] for it in items],
                "z_mm": items[0]["z_mm"],
                "entries": items,
            }
            for stem, items in sorted(groups.items(), key=lambda kv: kv[1][0]["z_mm"])
        ],
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"review_queue.json -> {out_path}")
    print(f"  实例: {len(entries)}，物理分组: {len(groups)}")
    for stem, items in groups.items():
        print(f"  - {stem}: {len(items)} 面, z={items[0]['z_mm']}, gap={items[0]['gap_to_nearest_connected_node_mm']}mm")


if __name__ == "__main__":
    main()
