"""跨页 BOM 树状汇总与去重（Gap 1）。

处理分册局部物料表与总材料表的层级映射与数量核对。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import re

from ..model import EngineeringModel
from ..intake.tower_bom import parse_bom_csv


# 配件截面（非杆件）：螺栓 / 连板 / 垫圈。BOM 数量核对只对杆件（角钢轴杆），
# 配件走装配/螺栓管线，不在 tower_bar 轴线模型里 —— 混进来只会产生伪冲突。
_FITTING_SECTION_RE = re.compile(r"^\s*(\d+M|-\d|Q345-\d)", re.IGNORECASE)
_ANGLE_SECTION_RE = re.compile(r"L\d", re.IGNORECASE)


def is_fitting_section(section: str) -> bool:
    """螺栓（5M16X40）/ 连板（-6X128、Q345-12X135）等配件截面。"""
    return bool(_FITTING_SECTION_RE.match(str(section or "")))


def is_angle_section(section: str) -> bool:
    """角钢杆件截面（L40X3 / Q345L70X5 / L56X5）。"""
    return bool(_ANGLE_SECTION_RE.search(str(section or "")))


def _select_master_row(rows: List[Dict]) -> Optional[Dict]:
    """同一 bar_id 多行（图纸 BOM 件号与螺栓表撞号）：选结构杆行。

    规则（诚实）：qty<=0 跳过；角钢行优先（杆件核对对象）；多角钢行取
    qty 最大；无角钢行时返回 None（纯配件撞号，不进杆件比对）。
    """
    usable = [r for r in rows if int(r.get("qty", 0) or 0) > 0]
    if not usable:
        return None
    angles = [r for r in usable if is_angle_section(r.get("section"))]
    pool = angles or usable
    return max(pool, key=lambda r: int(r.get("qty", 0) or 0))


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
    physical_bar_counts: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """汇总多模型 BOM 行，按 bar_id 去重并核对数量。

    physical_bar_counts：cross_file 合并模型的物理件号根数（M8），优先于 bom_row 汇总。

    返回 {
        tree: [BomTreeNode...],
        conflicts: [{bar_id, aggregated_qty, master_qty, ...}],
        total_unique_bar_ids: int,
        only_in_master: [...],
        only_in_model: [...],
    }
    """
    by_id: Dict[str, BomTreeNode] = {}
    qty_by_source: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    if physical_bar_counts:
        for bid, qty in physical_bar_counts.items():
            by_id[bid] = BomTreeNode(
                bar_id=bid,
                qty=int(qty),
                sources=["merged_model"],
            )
            qty_by_source[bid]["merged_model"] = int(qty)

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

    for bid, node in by_id.items():
        if "merged_model" not in node.sources:
            node.qty = sum(qty_by_source[bid].values())

    master: Dict[str, Dict] = {}
    master_fittings: List[str] = []   # 配件（螺栓/连板）撞号或纯配件行，不进杆件比对
    if master_bom_path:
        rows_by_id: Dict[str, List[Dict]] = defaultdict(list)
        for row in parse_bom_csv(master_bom_path):
            rows_by_id[row["bar_id"]].append(row)
        for bid, rows in rows_by_id.items():
            sel = _select_master_row(rows)
            if sel is None:
                # qty<=0 或纯配件行：跳过数量比对（诚实跳过，不是通过）
                master_fittings.append(bid)
                continue
            master[bid] = sel
            if len(rows) > 1:
                # 记录撞号明细（审计用）
                master[bid]["_collided_rows"] = [
                    {"section": r.get("section"), "qty": r.get("qty"),
                     "length_mm": r.get("length_mm")} for r in rows
                ]

    conflicts: List[Dict[str, Any]] = []          # 超计（真实数据损坏，FAILED）
    under_identified: List[Dict[str, Any]] = []   # 欠计（标注覆盖缺口，待 P4，PENDING）
    only_in_master: List[str] = []
    only_in_model: List[str] = []
    tree: List[BomTreeNode] = []

    compare_ids = set(by_id)
    if physical_bar_counts:
        compare_ids = set(physical_bar_counts)

    if master:
        for bid in sorted(master):
            if bid not in compare_ids:
                only_in_master.append(bid)
        for bid in sorted(compare_ids):
            if bid not in master:
                only_in_model.append(bid)

    for bid in sorted(by_id):
        node = by_id[bid]
        actual_qty = int(physical_bar_counts.get(bid, node.qty)) if physical_bar_counts else node.qty
        node.qty = actual_qty
        if bid in master:
            mrow = master[bid]
            mqty = int(mrow.get("qty", 1))
            base = {
                "bar_id": bid,
                "aggregated_qty": actual_qty,
                "master_qty": mqty,
                "qty_by_source": dict(qty_by_source.get(bid, {})),
                "source": "merged_model" if physical_bar_counts else "sheet_bom_row",
            }
            if actual_qty > mqty:
                # 模型比图纸 BOM 还多 = 真实冲突（split 未并/件号错挂/重复实例）
                base["kind"] = "over_count"
                conflicts.append(base)
            elif actual_qty < mqty:
                # 模型少于图纸 = 识别/标注不全（A1/A3 关联率 31% 的已知缺口，
                # 归 P4 件号绑定；不该在 BOM 核对里重复算 failed）
                base["kind"] = "under_identified"
                under_identified.append(base)
            node.children.append(BomTreeNode(
                bar_id=f"master:{bid}",
                section=mrow.get("section", ""),
                length_mm=float(mrow.get("length_mm", 0)),
                qty=mqty,
                sources=["master_bom"],
            ))
        tree.append(node)

    return {
        "tree": [n.to_dict() for n in tree],
        "conflicts": conflicts,
        "under_identified": under_identified,
        "fittings_skipped": sorted(master_fittings),
        "total_unique_bar_ids": len(by_id) if by_id else len(compare_ids),
        "conflict_count": len(conflicts),
        "under_identified_count": len(under_identified),
        "only_in_master": only_in_master,
        "only_in_model": only_in_model,
        "master_bom_path": master_bom_path,
        "physical_qty_source": "merged_model" if physical_bar_counts else None,
    }
