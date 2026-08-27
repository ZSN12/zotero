"""跨页 BOM 树状汇总与去重（Gap 1）。

处理分册局部物料表与总材料表的层级映射与数量核对。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..model import EngineeringModel
from ..intake.tower_bom import parse_bom_csv


@dataclass
class BomTreeNode:
    bar_id: str
    section: str = ""
    length_mm: float = 0.0
    qty: int = 0
    sources: List[str] = field(default_factory=list)
    children: List["BomTreeNode"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bar_id": self.bar_id,
            "section": self.section,
            "length_mm": self.length_mm,
            "qty": self.qty,
            "sources": self.sources,
            "children": [c.to_dict() for c in self.children],
        }


def _bom_rows_from_model(model: EngineeringModel, source: str) -> List[Dict]:
    rows = []
    for comp in model.components.values():
        if comp.kind != "bom_row":
            continue
        row = dict(comp.properties)
        row["_source"] = source
        rows.append(row)
    return rows


def aggregate_bom_tree(
    models: List[EngineeringModel],
    *,
    master_bom_path: Optional[str] = None,
    model_sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """汇总多模型 BOM 行，按 bar_id 去重并核对数量。

    返回 {
        tree: [BomTreeNode...],
        conflicts: [{bar_id, qty_by_source, master_qty}],
        total_unique_bar_ids: int,
    }
    """
    by_id: Dict[str, BomTreeNode] = {}
    qty_by_source: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    sources = model_sources or [m.name for m in models]
    for i, model in enumerate(models):
        src = sources[i] if i < len(sources) else model.name
        for row in _bom_rows_from_model(model, src):
            bid = row.get("bar_id", "")
            if not bid:
                continue
            qty_by_source[bid][src] += int(row.get("qty", 1) or 1)
            if bid not in by_id:
                by_id[bid] = BomTreeNode(
                    bar_id=bid,
                    section=str(row.get("section", "")),
                    length_mm=float(row.get("length_mm", 0) or 0),
                    qty=0,
                    sources=[],
                )
            node = by_id[bid]
            if src not in node.sources:
                node.sources.append(src)

    master: Dict[str, Dict] = {}
    if master_bom_path:
        for row in parse_bom_csv(master_bom_path):
            master[row["bar_id"]] = row

    conflicts: List[Dict[str, Any]] = []
    tree: List[BomTreeNode] = []
    for bid, node in sorted(by_id.items()):
        node.qty = sum(qty_by_source[bid].values())
        if bid in master:
            mqty = int(master[bid].get("qty", 1))
            if mqty != node.qty:
                conflicts.append({
                    "bar_id": bid,
                    "aggregated_qty": node.qty,
                    "master_qty": mqty,
                    "qty_by_source": dict(qty_by_source[bid]),
                })
            node.children.append(BomTreeNode(
                bar_id=f"master:{bid}",
                section=master[bid].get("section", ""),
                length_mm=float(master[bid].get("length_mm", 0)),
                qty=mqty,
                sources=["master_bom"],
            ))
        tree.append(node)

    return {
        "tree": [n.to_dict() for n in tree],
        "conflicts": conflicts,
        "total_unique_bar_ids": len(by_id),
        "conflict_count": len(conflicts),
    }
