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
) -> Dict[str, Any]:
    """按 bar_id 汇总各 sheet 杆件出现次数与截面信息。"""
    by_id: Dict[str, Dict[str, Any]] = {}
    qty_by_source: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    sources = model_sources or [m.name for m in models]
    for i, model in enumerate(models):
        src = sources[i] if i < len(sources) else model.name
        for comp in model.components.values():
            if comp.kind != "tower_bar":
                continue
            bid = str(comp.properties.get("bar_id") or "")
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
            sec = comp.properties.get("section")
            if sec and sec not in node["sections"]:
                node["sections"].append(str(sec))

    entries = []
    cross_sheet = []
    for bid in sorted(by_id):
        node = by_id[bid]
        node["count"] = sum(qty_by_source[bid].values())
        node["qty_by_source"] = dict(qty_by_source[bid])
        entries.append(node)
        if len(node["sources"]) > 1:
            cross_sheet.append({
                "bar_id": bid,
                "sources": list(node["sources"]),
                "count": node["count"],
            })

    return {
        "entries": entries,
        "total_unique_bar_ids": len(entries),
        "cross_sheet_groups": cross_sheet,
        "cross_sheet_count": len(cross_sheet),
    }
