"""图册级杆件件号索引（M7 / Gap 1）。

当分册模型无 bom_row 时，从各 sheet 的 tower_bar 汇总件号出现次数，
供 Project Harness 与交付 manifest 使用。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from ..model import EngineeringModel


def aggregate_bar_inventory(
    models: List[EngineeringModel],
    *,
    model_sources: Optional[List[str]] = None,
    bom_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """按 bar_id 汇总各 sheet 杆件出现次数与截面信息。

    bom_rows 提供时（P0-3，2026-09-09）：非 master BOM 件号的跨册出现
    从 cross_sheet 计数排除（图面噪声件号与 r_project_cross_sheet_bar_id
    的核对语义无关——与 cross_file_bar_id_report 的白名单闸同纪律）。
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    qty_by_source: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # 证据链真实统计（阶段 2）：逐根杆件统计溯源覆盖情况，而非假设 100%。
    bars_with_sheet_evidence = 0
    bars_with_view_evidence = 0
    bars_with_multiple_projections = 0
    bars_total = 0

    sources = model_sources or [m.name for m in models]
    for i, model in enumerate(models):
        src = sources[i] if i < len(sources) else model.name
        for comp in model.components.values():
            if comp.kind != "tower_bar":
                continue
            bars_total += 1
            props = comp.properties
            # sheet 证据：source_file / drawing_view 存在且非空占位
            sheet = props.get("source_file") or props.get("drawing_view")
            if sheet not in (None, "", "None"):
                bars_with_sheet_evidence += 1
            # view 证据：view_type / face / generated_face 任一存在
            view = props.get("view_type") or props.get("face") or props.get("generated_face")
            if view not in (None, "", "None"):
                bars_with_view_evidence += 1
            # 多投影：projection_refs 数量 > 1
            prs = props.get("projection_refs") or []
            if len(prs) > 1:
                bars_with_multiple_projections += 1

            bid = str(props.get("bar_id") or "")
            if not bid or bid.startswith("UNLABELED"):
                continue
            qty_by_source[bid][src] += 1
            if bid not in by_id:
                by_id[bid] = {
                    "bar_id": bid,
                    "sources": [],
                    "count": 0,
                    "sections": [],
                }
            node = by_id[bid]
            if src not in node["sources"]:
                node["sources"].append(src)
            sec = props.get("section")
            if sec and sec not in node["sections"]:
                node["sections"].append(str(sec))

    entries = []
    cross_sheet = []
    # P0-3：BOM 白名单（member 行）——非杆件件号的跨册出现是图面噪声
    member_ids: set = set()
    if bom_rows:
        from ..intake.tower_bom import classify_bom_row
        for row in bom_rows:
            if classify_bom_row(row.get("bar_id", ""),
                               row.get("section", "")) == "member":
                member_ids.add(str(row.get("bar_id", "")))
    non_bom_cross = []
    for bid in sorted(by_id):
        node = by_id[bid]
        node["count"] = sum(qty_by_source[bid].values())
        node["qty_by_source"] = dict(qty_by_source[bid])
        entries.append(node)
        if len(node["sources"]) > 1:
            entry = {
                "bar_id": bid,
                "sources": list(node["sources"]),
                "count": node["count"],
            }
            if bom_rows and bid not in member_ids:
                non_bom_cross.append(entry)
                continue
            cross_sheet.append(entry)

    return {
        "entries": entries,
        "total_unique_bar_ids": len(entries),
        "cross_sheet_groups": cross_sheet,
        "cross_sheet_count": len(cross_sheet),
        # P0-3：非 BOM 件号的跨册出现（图面噪声，披露不核对）
        "non_bom_cross_sheet_groups": non_bom_cross,
        "non_bom_cross_sheet_count": len(non_bom_cross),
        # 证据链真实统计（阶段 2）
        "evidence_chain": {
            "bars_total": bars_total,
            "bars_with_sheet_evidence": bars_with_sheet_evidence,
            "bars_with_view_evidence": bars_with_view_evidence,
            "bars_with_multiple_projections": bars_with_multiple_projections,
            "sheet_evidence_rate": round(bars_with_sheet_evidence / bars_total, 4) if bars_total else 0.0,
            "view_evidence_rate": round(bars_with_view_evidence / bars_total, 4) if bars_total else 0.0,
        },
    }
