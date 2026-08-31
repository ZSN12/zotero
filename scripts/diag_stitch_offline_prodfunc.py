#!/usr/bin/env python3
"""决定性实验：生产 stitch_collinear_bars 直接作用于基线模型（离线），
用生产评测器评估——隔离「函数行为」与「生产管线上下文」。

如果离线跑生产函数也得 188：函数本身（corner 跳过/细节差异）是根因。
如果离线跑生产函数得 ~209：生产接线位置/上下文是根因。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.solve.tower_geometry import stitch_collinear_bars  # noqa: E402
from traceability.eval.metrics import (  # noqa: E402
    bars_from_model_2d, gt_bars_2d, hungarian_match, segment_cost,
)

GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"
BASELINE = Path("/tmp/model_baseline.json")


def model_to_face(model):
    """从 model.json 还原 (face_nodes, face_bars) 供 stitch 使用。"""
    comps = model["components"]
    nodes = {cid[3:]: (c["properties"]["x"], c["properties"]["y"], c["properties"]["z"])
             for cid, c in comps.items() if c.get("kind") == "tower_node"}
    bars = []
    for cid, c in comps.items():
        if c.get("kind") != "tower_bar":
            continue
        p = dict(c["properties"])
        p["id"] = cid[3:]
        p["from"] = p.pop("from_node")[3:]
        p["to"] = p.pop("to_node")[3:]
        bars.append(p)
    return nodes, bars


def eval_model(model, tag):
    gt = json.load(open(GT_PATH))
    g = gt_bars_2d(gt, "front")
    m = bars_from_model_2d(model, view="front", mode="physical")
    row = {}
    for tol in (200, 500):
        matched, _, _ = hungarian_match(
            [s for s, _, _ in g], [s for s, _ in m], segment_cost, max_cost=float(tol))
        row[tol] = (len(matched), len(m))
    print(f"{tag}: bars={len(m)} TP@200={row[200][0]} TP@500={row[500][0]}")
    return {gi for gi, _ in
            hungarian_match([s for s, _, _ in g], [s for s, _ in m],
                            segment_cost, max_cost=500.0)[0]}


model = json.load(open(BASELINE))
nodes, bars = model_to_face(model)
print(f"restored: nodes={len(nodes)} bars={len(bars)}")

new_bars, new_nodes, rep = stitch_collinear_bars(
    nodes, bars, gap_mm=300.0, ang_deg=10.0,
    min_merged_len_mm=600.0, max_merged_len_mm=4500.0,
    skip_corner_leg=("--no-skip-corner" in sys.argv))
print("stitch report:", json.dumps(rep, ensure_ascii=False))

# 把结果写回 model 结构
comps = model["components"]
consumed = {str(b.get("id")) for b in bars if str(b.get("id")) not in
            {str(nb.get("id")) for nb in new_bars}}
for cid in list(comps):
    if cid.startswith("4f_") and cid[3:] in consumed and comps[cid].get("kind") == "tower_bar":
        del comps[cid]
maxn = 0
for nid, pos in new_nodes.items():
    comps[f"4f_{nid}"] = {
        "id": f"4f_{nid}", "name": nid, "kind": "tower_node", "source": None,
        "properties": {"x": pos[0], "y": pos[1], "z": pos[2],
                        "solve_status": "solved", "generated_4face": True,
                        "original_node_id": nid, "geometry_origin": "derived_4face"},
    }
for nb in new_bars:
    if str(nb.get("geometry_origin")) != "collinear_stitch":
        continue
    cid = f"4f_{nb['id']}"
    src = comps.get(f"4f_{nb['stitched_from'][0]}", {}).get("properties", {})
    comps[cid] = {
        "id": cid, "name": nb["id"], "kind": "tower_bar",
        "source": comps.get(f"4f_{nb['stitched_from'][0]}", {}).get("source"),
        "properties": {
            "bar_id": nb.get("bar_id"), "from_node": f"4f_{nb['from']}",
            "to_node": f"4f_{nb['to']}", "face": nb.get("face"),
            "generated_face": str(nb.get("face") or "").upper(),
            "role": nb.get("role"), "corner_leg": False, "diaphragm": False,
            "panel_subdivision": False, "generated_4face": True,
            "solve_status": "solved", "geometry_origin": "collinear_stitch",
            "geometry_class": nb.get("geometry_class"),
            "evidence_status": "recognized" if nb.get("geometry_class") == "recognized" else "reconstructed",
            "length_mm_3d": None,
        },
    }

eval_model(model, "offline-prodfunc")
