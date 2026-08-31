#!/usr/bin/env python3
"""06 段斜材拓扑诊断：line_kind / twist_kind 分布 + 解释对摘要。

用法：
    python3 scripts/diagnose_diagonal_topology.py
    python3 scripts/diagnose_diagonal_topology.py --model out/.../model.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

JC1_PANELS = [11000, 12000, 13000, 14000, 16000, 17000, 19000]


def _from_report(report: dict) -> None:
    print("=== diagonal_topology_report ===")
    print(f"sheets: {report.get('sheets')}")
    print(f"z_window: {report.get('z_window')}")
    print(f"fan_pairs: {report.get('fan_pairs')}  twist_pairs: {report.get('twist_pairs')}")
    print(f"generated: {report.get('generated')}  n_candidates: {report.get('n_candidates')}")
    print(f"n_twist_candidates: {report.get('n_twist_candidates')}  "
          f"twist_faces: {report.get('twist_faces')}")
    lk = Counter(c.get("line_kind") or "None" for c in (report.get("candidates") or []))
    tk = Counter(c.get("twist_kind") or "None" for c in (report.get("twist_candidates") or []))
    print(f"line_kind (front fan): {dict(lk)}")
    print(f"twist_kind (multi-face): {dict(tk)}")
    sel = report.get("selection") or {}
    print(f"selection: kept={sel.get('kept')} beat_unit={sel.get('beat_unit')} "
          f"rejected={len(sel.get('rejected') or [])}")
    for r in (report.get("interpretations") or [])[:12]:
        print(f"  {r['kind']:5} {r['z_lo']}→{r['z_hi']} score={r['score']} n={r['n_evidence']}")


def _from_model(model_path: Path) -> None:
    from traceability.io import load_model
    from traceability.solve.tower_solver import _iter_bars, _iter_nodes
    from traceability.solve.tower_geometry import gt_tower_half_width
    from traceability.solve.diagonal_topology import reconstruct_diagonal_topology

    overlay_path = REPO / "examples/external/guowang_35A1/layer_overlay.json"
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    model = load_model(str(model_path))
    nodes = {}
    for cid, comp in _iter_nodes(model):
        p = comp.properties
        if all(p.get(a) is not None for a in "xyz"):
            nodes[cid] = (float(p["x"]), float(p["y"]), float(p["z"]))
    bars = []
    for cid, comp in _iter_bars(model):
        p = comp.properties
        bars.append({
            "id": cid, "from": p.get("from_node"), "to": p.get("to_node"),
            "face": p.get("face", "f"), "role": p.get("role"),
            "source_file": p.get("source_file"),
            "geometry_origin": p.get("geometry_origin"),
            "geometry_class": p.get("geometry_class"),
            "bar_id": p.get("bar_id"), "layer": p.get("layer"),
        })
    z_window = tuple(overlay.get("diagonal_topology_z_window") or (11000.0, 17500.0))
    sheets = overlay.get("diagonal_topology_sheets") or ["35A1-JC1-06"]
    twist_faces = overlay.get("diagonal_topology_twist_faces") or ("f", "l", "r")
    _, _, rep = reconstruct_diagonal_topology(
        nodes, bars, gt_tower_half_width,
        sheets=list(sheets),
        panel_levels=JC1_PANELS,
        z_window=(float(z_window[0]), float(z_window[1])),
        twist_faces=list(twist_faces),
    )
    _from_report(rep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, default=None)
    args = ap.parse_args()
    if args.model and args.model.exists():
        _from_model(args.model)
    else:
        print("无 model.json：仅输出 HANDOFF 参考分布")
        print("line_kind (06 front 生产): {MID: 12, HALF: 4, None: 7, FULL: 0}")
        print("运行全管线后: python3 scripts/diagnose_diagonal_topology.py "
              "--model out/35A1-JC1-full-deliver/model.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
