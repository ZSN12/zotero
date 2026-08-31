#!/usr/bin/env python3
"""06 段斜材 P1.1 A/B 对照：baseline vs p11 vs relaxed。

用法：
    python3 scripts/ab_diagonal_p11.py              # HANDOFF 11 候选回放
    python3 scripts/ab_diagonal_p11.py --model PATH # 从 model.json 06 段重跑

输出 Markdown 表格到 stdout；可选 --json 落盘。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.solve.diagonal_topology import (  # noqa: E402
    select_interpretations,
)

# HANDOFF §1.1：生产 11 fan 候选 + GT 判定（每对 8 杆）
_HANDOFF_FANS: List[Dict[str, Any]] = [
    {"z_lo": 16488.6, "z_hi": 19000, "score": 801.8, "gt_tp": 0, "gt_fp": 8},
    {"z_lo": 14349.4, "z_hi": 16000, "score": 818.6, "gt_tp": 4, "gt_fp": 4},
    {"z_lo": 13797.4, "z_hi": 16000, "score": 1009.1, "gt_tp": 8, "gt_fp": 0},
    {"z_lo": 12143.0, "z_hi": 14000, "score": 1015.2, "gt_tp": 8, "gt_fp": 0},
    {"z_lo": 13229.5, "z_hi": 16000, "score": 1687.7, "gt_tp": 8, "gt_fp": 0},
    {"z_lo": 15957.9, "z_hi": 19000, "score": 1863.3, "gt_tp": 8, "gt_fp": 0},
    {"z_lo": 12683.4, "z_hi": 16000, "score": 1948.5, "gt_tp": 4, "gt_fp": 4},
    {"z_lo": 12143.0, "z_hi": 16000, "score": 2745.5, "gt_tp": 8, "gt_fp": 0},
    {"z_lo": 15417.8, "z_hi": 19000, "score": 2943.6, "gt_tp": 4, "gt_fp": 4},
    {"z_lo": 14898.0, "z_hi": 19000, "score": 3220.2, "gt_tp": 8, "gt_fp": 0},
    {"z_lo": 14349.4, "z_hi": 19000, "score": 3873.1, "gt_tp": 0, "gt_fp": 8},
]

JC1_PANELS = [11000, 12000, 13000, 14000, 16000, 17000, 19000]
BARS_PER_PAIR = 8


def _fan_interp(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "fan",
        "z_lo": rec["z_lo"],
        "z_hi": rec["z_hi"],
        "score": rec["score"],
        "evidence": ["handoff"],
        "n": 1,
    }


def _mode_select(
    interps: List[Dict[str, Any]],
    panel_levels: Sequence[float],
    mode: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if mode == "none":
        return list(interps), {"kept": len(interps), "rejected": [], "mode": "none"}
    kwargs: Dict[str, Any] = {}
    if mode == "relaxed":
        kwargs["beat_tol_mm"] = 650.0
    kept, audit = select_interpretations(interps, panel_levels, **kwargs)
    audit["mode"] = mode
    return kept, audit


def _gt_score(kept: List[Dict[str, Any]], handoff: List[Dict[str, Any]]) -> Dict[str, int]:
    """按 (z_lo,z_hi) 四舍五入匹配 HANDOFF 行的 GT TP/FP。"""
    lookup = {
        (round(r["z_lo"], 1), round(r["z_hi"], 1)): r for r in handoff
    }
    tp = fp = bars = 0
    for r in kept:
        if r["kind"] != "fan":
            continue
        key = (round(r["z_lo"], 1), round(r["z_hi"], 1))
        src = lookup.get(key)
        if not src:
            continue
        bars += BARS_PER_PAIR
        tp += src["gt_tp"]
        fp += src["gt_fp"]
    return {"fan_pairs": len([r for r in kept if r["kind"] == "fan"]),
            "bars": bars, "tp": tp, "fp": fp}


def run_handoff_ab(panel_levels: Sequence[float]) -> List[Dict[str, Any]]:
    interps = [_fan_interp(r) for r in _HANDOFF_FANS]
    rows: List[Dict[str, Any]] = []
    for mode in ("none", "p11", "relaxed"):
        kept, audit = _mode_select(interps, panel_levels, mode)
        gs = _gt_score(kept, _HANDOFF_FANS)
        rows.append({
            "mode": mode,
            "fan_pairs": gs["fan_pairs"],
            "bars": gs["bars"],
            "tp": gs["tp"],
            "fp": gs["fp"],
            "beat_unit": audit.get("beat_unit"),
            "rejected": len(audit.get("rejected") or []),
            "reject_reasons": sorted({x["reason"] for x in (audit.get("rejected") or [])}),
        })
    return rows


def run_model_ab(model_path: Path, panel_levels: Sequence[float]) -> List[Dict[str, Any]]:
    from traceability.io import load_model
    from traceability.solve.tower_solver import _iter_bars, _iter_nodes
    from traceability.solve.tower_geometry import gt_tower_half_width
    from traceability.solve.diagonal_topology import reconstruct_diagonal_topology

    overlay_path = REPO / "examples/external/guowang_35A1/layer_overlay.json"
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    hw_fn = gt_tower_half_width

    model = load_model(str(model_path))
    nodes: Dict[str, Tuple[float, float, float]] = {}
    for cid, comp in _iter_nodes(model):
        p = comp.properties
        if all(p.get(a) is not None for a in "xyz"):
            nodes[cid] = (float(p["x"]), float(p["y"]), float(p["z"]))
    bars: List[dict] = []
    for cid, comp in _iter_bars(model):
        p = comp.properties
        bars.append({
            "id": cid,
            "from": p.get("from_node"),
            "to": p.get("to_node"),
            "face": p.get("face", "f"),
            "role": p.get("role"),
            "source_file": p.get("source_file"),
            "geometry_origin": p.get("geometry_origin"),
            "geometry_class": p.get("geometry_class"),
            "bar_id": p.get("bar_id"),
            "layer": p.get("layer"),
        })

    z_window = tuple(overlay.get("diagonal_topology_z_window") or (11000.0, 17500.0))
    sheets = overlay.get("diagonal_topology_sheets") or ["35A1-JC1-06"]

    rows: List[Dict[str, Any]] = []
    for mode in ("none", "p11", "relaxed"):
        _, new_bars, rep = reconstruct_diagonal_topology(
            nodes, bars, hw_fn,
            sheets=list(sheets),
            panel_levels=panel_levels,
            z_window=(float(z_window[0]), float(z_window[1])),
            level_source_label="gt_canonical",
            selection_mode=mode,
        )
        gen = [b for b in new_bars if b.get("diagonal_topology")]
        sel = rep.get("selection") or {}
        rows.append({
            "mode": mode,
            "fan_pairs": rep.get("fan_pairs", 0),
            "twist_pairs": rep.get("twist_pairs", 0),
            "bars": len(gen),
            "beat_unit": sel.get("beat_unit"),
            "rejected": len(sel.get("rejected") or []),
            "reject_reasons": sorted({x["reason"] for x in (sel.get("rejected") or [])}),
        })
    return rows


def _print_table(rows: List[Dict[str, Any]], title: str) -> None:
    print(f"\n## {title}\n")
    if "tp" in rows[0]:
        hdr = "| mode | fan_pairs | bars | TP | FP | beat_unit | rejected | reasons |"
        sep = "|------|-----------|------|----|----|-----------|----------|---------|"
    else:
        hdr = "| mode | fan_pairs | twist | bars | beat_unit | rejected | reasons |"
        sep = "|------|-----------|-------|------|-----------|----------|---------|"
    print(hdr)
    print(sep)
    for r in rows:
        reasons = ",".join(r.get("reject_reasons") or []) or "—"
        bu = r.get("beat_unit")
        bu_s = f"{bu:.0f}" if isinstance(bu, (int, float)) else "skip"
        if "tp" in r:
            print(f"| {r['mode']} | {r['fan_pairs']} | {r['bars']} | {r['tp']} | "
                  f"{r['fp']} | {bu_s} | {r['rejected']} | {reasons} |")
        else:
            print(f"| {r['mode']} | {r['fan_pairs']} | {r.get('twist_pairs', 0)} | "
                  f"{r['bars']} | {bu_s} | {r['rejected']} | {reasons} |")


def main() -> int:
    ap = argparse.ArgumentParser(description="06 段斜材 P1.1 A/B 对照")
    ap.add_argument("--model", type=Path, default=None,
                    help="deliver 产物 model.json（可选，重跑三种 selection_mode）")
    ap.add_argument("--json", type=Path, default=None, help="结果 JSON 落盘路径")
    args = ap.parse_args()

    result: Dict[str, Any] = {"panel_levels": JC1_PANELS}
    handoff_rows = run_handoff_ab(JC1_PANELS)
    result["handoff_replay"] = handoff_rows
    _print_table(handoff_rows, "HANDOFF 11 fan 候选回放（GT TP/FP 来自 §1.1 离线归因）")

    if args.model and args.model.exists():
        model_rows = run_model_ab(args.model, JC1_PANELS)
        result["model_rerun"] = model_rows
        _print_table(model_rows, f"model 重跑 ({args.model})")
    elif args.model:
        print(f"\n⚠ model 不存在，跳过: {args.model}", file=sys.stderr)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {args.json}")

    # Phase 0 验收：p11 应优于 baseline FP，且 TP≥55（HANDOFF 回放）
    p11 = next(r for r in handoff_rows if r["mode"] == "p11")
    base = next(r for r in handoff_rows if r["mode"] == "none")
    ok = p11["tp"] >= 55 and p11["fp"] <= 15 and p11["fp"] < base["fp"]
    print(f"\nPhase 0 gate (HANDOFF replay): {'PASS' if ok else 'FAIL'} "
          f"(p11 TP={p11['tp']} FP={p11['fp']} vs baseline FP={base['fp']})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
