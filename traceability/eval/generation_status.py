"""分册生成状态审计：从 deliver 产物 model.json 汇总候选→保留→拒绝链路。

供 production_chain / ab_full_pipeline / final_report 写入 generation_status.json。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _safe_get(props: dict, *keys: str, default=None):
    cur: Any = props
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def collect_generation_status(model: Dict[str, Any]) -> Dict[str, Any]:
    """从 model.json 抽取各分册/过滤阶段的生成审计。"""
    df = (model.get("components") or {}).get("drawing_file") or {}
    props = df.get("properties") or {}

    dt_rep = props.get("diagonal_topology_report") or {}
    per_sheet: List[Dict[str, Any]] = []
    if isinstance(dt_rep.get("per_sheet"), list):
        for sh in dt_rep["per_sheet"]:
            sel = sh.get("selection") or {}
            per_sheet.append({
                "sheet": sh.get("sheet"),
                "n_candidates": sh.get("n_candidates", 0),
                "n_twist_candidates": sh.get("n_twist_candidates", 0),
                "generated": sh.get("generated", 0),
                "fan_pairs": sh.get("fan_pairs", 0),
                "twist_pairs": sh.get("twist_pairs", 0),
                "removed_originals": len(sh.get("removed_originals") or []),
                "selection_mode": sel.get("mode"),
                "selection_kept": sel.get("kept"),
                "selection_rejected": len(sel.get("rejected") or []),
                "reject_reasons": sorted({
                    str(x.get("reason") or "?")
                    for x in (sel.get("rejected") or [])
                }),
                "beat_unit": sel.get("beat_unit"),
            })
    elif dt_rep:
        sel = dt_rep.get("selection") or {}
        per_sheet.append({
            "sheet": (dt_rep.get("sheets") or ["?"])[0],
            "n_candidates": dt_rep.get("n_candidates", 0),
            "generated": dt_rep.get("generated", 0),
            "fan_pairs": dt_rep.get("fan_pairs", 0),
            "twist_pairs": dt_rep.get("twist_pairs", 0),
            "selection_mode": sel.get("mode"),
            "selection_rejected": len(sel.get("rejected") or []),
            "reject_reasons": sorted({
                str(x.get("reason") or "?")
                for x in (sel.get("rejected") or [])
            }),
        })

    filters: Dict[str, Any] = {}
    for key in (
        "diaphragm_depth_filter",
        "collinear_stitch_role_specific",
        "crossarm_fp_prune_report",
        "centerline_geom_filter",
        "multiview_hypothesis_report",
        "candidate_lifecycle",
    ):
        val = props.get(key)
        if val is not None:
            filters[key] = val

    lifecycle = props.get("candidate_lifecycle") or {}
    totals = dt_rep.get("totals") or {}
    return {
        "diagonal_topology": {
            "sheets": dt_rep.get("sheets") or [],
            "totals": {
                "generated": totals.get("generated", dt_rep.get("generated", 0)),
                "fan_pairs": totals.get("fan_pairs", dt_rep.get("fan_pairs", 0)),
                "twist_pairs": totals.get("twist_pairs", dt_rep.get("twist_pairs", 0)),
                "removed_originals": totals.get(
                    "removed_originals", len(dt_rep.get("removed_originals") or [])),
            },
            "per_sheet": per_sheet,
        },
        "filters": filters,
        "lifecycle_totals": lifecycle.get("totals") if isinstance(lifecycle, dict) else None,
        "deliver_status": props.get("deliver_status"),
    }
