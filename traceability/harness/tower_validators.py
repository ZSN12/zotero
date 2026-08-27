"""铁塔专用验证器（方案 §4）。

五条规则：
    * r_topology_closed      每根杆件两端节点存在
    * r_bom_length_match     杆件 3D 长度 vs BOM 长度，偏差 ≤ 3%
    * r_bom_section_match    杆件截面 vs BOM 截面，归一化后相等
    * r_node_fully_solved    关键节点三轴坐标已知（无 placeholder）
    * r_no_duplicate_bar_id  杆件编号唯一
"""

from __future__ import annotations

import math
import re
from typing import Optional

from ..model import EngineeringModel, ValidationStatus
from .harness import ValidationResult


def _normalize_section(s: str) -> str:
    """截面规格归一化：L100x8 / L100×8 / L100*8 -> l100x8"""
    return re.sub(r"[×*xX]", "x", s.strip().lower().replace(" ", ""))


def _iter_bars(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_bar":
            yield cid, comp


def _iter_nodes(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_node":
            yield cid, comp


def validate_topology_closed(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """每根杆件两端节点必须存在。"""
    node_ids = {cid for cid, _ in _iter_nodes(model)}
    missing = []
    for cid, bar in _iter_bars(model):
        for end in ("from_node", "to_node"):
            nid = bar.properties.get(end)
            if nid not in node_ids:
                missing.append(f"{bar.properties.get('bar_id', cid)}.{end}={nid}")
    if not missing:
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                "所有杆件两端节点存在", "topology-closed")
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"{len(missing)} 处拓扑断裂：{missing[:5]}", "topology-closed")


def validate_bom_length_match(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """杆件 3D 长度 vs BOM 长度，偏差 ≤ 3%。"""
    failures = []
    matched = 0
    for cid, bar in _iter_bars(model):
        bid = bar.properties.get("bar_id")
        bom_dim = model.dimensions.get(f"dim_bom_length_{bid}")
        if bom_dim is None or bom_dim.value is None:
            continue
        # 跨视图合并后优先用 3D 长度；未合并的投影杆件用图纸投影长度
        actual = bar.properties.get("length_mm_3d")
        if actual is None:
            actual = bar.properties.get("length_mm")
        if actual is None:
            continue
        bom_len = float(bom_dim.value)
        if bom_len <= 0:
            continue
        matched += 1
        dev = abs(actual - bom_len) / bom_len
        if dev > 0.03:
            failures.append((bid, round(actual, 1), round(bom_len, 1), round(dev * 100, 1)))
    if matched == 0:
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "无足够数据做 BOM 长度核验", "bom-length")
    if failures:
        return ValidationResult(rule_id, ValidationStatus.FAILED,
                                f"{len(failures)} 根杆件长度超差：{failures[:3]}", "bom-length")
    return ValidationResult(rule_id, ValidationStatus.PASSED,
                            f"{matched} 根杆件长度与 BOM 偏差 ≤ 3%", "bom-length")


def validate_bom_section_match(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """杆件截面 vs BOM 截面。"""
    mismatch = []
    matched = 0
    for cid, bar in _iter_bars(model):
        bid = bar.properties.get("bar_id")
        bom_dim = model.dimensions.get(f"dim_bom_section_{bid}")
        actual = bar.properties.get("section")
        if bom_dim is None or bom_dim.value is None or actual is None:
            continue
        matched += 1
        if _normalize_section(str(actual)) != _normalize_section(str(bom_dim.value)):
            mismatch.append((bid, actual, bom_dim.value))
    if matched == 0:
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "无足够数据做 BOM 截面核验", "bom-section")
    if mismatch:
        return ValidationResult(rule_id, ValidationStatus.FAILED,
                                f"{len(mismatch)} 根杆件截面不符：{mismatch[:3]}", "bom-section")
    return ValidationResult(rule_id, ValidationStatus.PASSED,
                            f"{matched} 根杆件截面与 BOM 一致", "bom-section")


def validate_node_fully_solved(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """关键节点三轴坐标已知。"""
    unsolved = []
    for cid, node in _iter_nodes(model):
        p = node.properties
        if p.get("x") is None or p.get("y") is None or p.get("z") is None:
            unsolved.append(cid)
    if not unsolved:
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                "所有节点三轴坐标已知", "node-solved")
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"{len(unsolved)} 个节点缺坐标：{unsolved[:5]}", "node-solved")


def validate_scan_reviewed(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """P2-5 扫描→终版 3D 闸门：所有扫描候选必须 solve_status=verified。"""
    unreviewed = []
    for cid, comp in model.components.items():
        if comp.kind not in ("tower_bar", "tower_node"):
            continue
        status = comp.properties.get("solve_status")
        if status == "pending_review":
            unreviewed.append(cid)
    if not unreviewed:
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                "扫描候选已人工确认（solve_status=verified）", "scan-reviewed")
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"{len(unreviewed)} 个扫描候选仍待人工复核：{unreviewed[:5]}", "scan-reviewed")


def validate_no_duplicate_bar_id(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """杆件编号唯一（同视图内）。"""
    from collections import defaultdict
    by_id = defaultdict(list)
    for cid, bar in _iter_bars(model):
        bid = bar.properties.get("bar_id")
        view = bar.properties.get("view_type", "?")
        by_id[(bid, view)].append(cid)
    dups = {k: v for k, v in by_id.items() if len(v) > 1}
    if not dups:
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                "杆件编号在视图内唯一", "no-dup-bar-id")
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"{len(dups)} 组重复编号：{list(dups)[:3]}", "no-dup-bar-id")


# 规则 ID -> 验证器
tower_validators = {
    "r_topology_closed": validate_topology_closed,
    "r_bom_length_match": validate_bom_length_match,
    "r_bom_section_match": validate_bom_section_match,
    "r_node_fully_solved": validate_node_fully_solved,
    "r_no_duplicate_bar_id": validate_no_duplicate_bar_id,
    "r_scan_reviewed": validate_scan_reviewed,
}


TOWER_RULE_DEFS = [
    {
        "id": "r_topology_closed",
        "name": "铁塔杆件拓扑闭合",
        "description": "每根杆件两端节点必须存在（from_node/to_node 指向 tower_node）",
    },
    {
        "id": "r_bom_length_match",
        "name": "杆件长度与 BOM 核验",
        "description": "杆件 3D 长度 vs BOM 长度，偏差 ≤ 3%",
    },
    {
        "id": "r_bom_section_match",
        "name": "杆件截面与 BOM 核验",
        "description": "杆件截面规格归一化后与 BOM 相等",
    },
    {
        "id": "r_node_fully_solved",
        "name": "节点三轴坐标齐备",
        "description": "关键节点 x/y/z 三轴坐标已知，无 placeholder",
    },
    {
        "id": "r_no_duplicate_bar_id",
        "name": "杆件编号唯一",
        "description": "同一视图内杆件编号不重复",
    },
    {
        "id": "r_scan_reviewed",
        "name": "扫描图人工复核闸门",
        "description": "扫描图候选杆件/节点必须 solve_status=verified 才可进终版 3D",
    },
]


def inject_tower_rules(model: EngineeringModel) -> EngineeringModel:
    """把铁塔验证规则写入模型（applies_to 指向实际构件）。

    基础五条始终注入；r_scan_reviewed 只在存在扫描候选
    （solve_status=pending_review）时注入。
    """
    from ..model import Rule

    bar_ids = [cid for cid, c in model.components.items() if c.kind == "tower_bar"]
    node_ids = [cid for cid, c in model.components.items() if c.kind == "tower_node"]
    has_scan = any(
        c.properties.get("solve_status") == "pending_review"
        for c in model.components.values() if c.kind in ("tower_bar", "tower_node")
    )

    specs = list(TOWER_RULE_DEFS)
    if not has_scan:
        specs = [sp for sp in specs if sp["id"] != "r_scan_reviewed"]

    for spec in specs:
        rid = spec["id"]
        if rid in model.rules:
            continue
        if rid in ("r_topology_closed", "r_bom_length_match",
                   "r_bom_section_match", "r_no_duplicate_bar_id"):
            applies_to = bar_ids
        elif rid == "r_scan_reviewed":
            applies_to = bar_ids + node_ids
        else:
            applies_to = node_ids
        model.add_rule(Rule(
            id=rid,
            name=spec["name"],
            description=spec["description"],
            applies_to=applies_to,
        ))
    return model
