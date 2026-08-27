"""螺栓群构造与力学验算（Gap 2）。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, Rule, ValidationStatus


@dataclass
class BoltSpec:
    count: int
    diameter_mm: float
    length_mm: float

    @property
    def hole_diameter_mm(self) -> float:
        return self.diameter_mm + 1.5


_BOLT_RE = re.compile(
    r"(?:(\d+)\s*)?M\s*(\d+(?:\.\d+)?)\s*[Xx×*]\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def parse_bolt_annotation(text: str) -> Optional[BoltSpec]:
    m = _BOLT_RE.search(text or "")
    if not m:
        return None
    return BoltSpec(
        count=int(m.group(1) or 1),
        diameter_mm=float(m.group(2)),
        length_mm=float(m.group(3)),
    )


@dataclass
class BoltGroup:
    group_id: str
    spec: BoltSpec
    holes: List[Tuple[float, float]]
    plate_outline: Optional[List[Tuple[float, float]]] = None

    def to_component(self) -> Component:
        return Component(
            id=f"bolt_group_{self.group_id}",
            name=f"螺栓群 {self.group_id}",
            kind="bolt_group",
            properties={
                "count": self.spec.count,
                "diameter_mm": self.spec.diameter_mm,
                "length_mm": self.spec.length_mm,
                "hole_diameter_mm": self.spec.hole_diameter_mm,
                "holes": [list(h) for h in self.holes],
                "solve_status": "pending_review",
            },
        )


def _point_to_segment_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _point_in_polygon(px: float, py: float, outline: List[Tuple[float, float]]) -> bool:
    """射线法判断点是否在多边形内（含边界近似）。"""
    if len(outline) < 3:
        return False
    inside = False
    j = len(outline) - 1
    for i in range(len(outline)):
        xi, yi = outline[i]
        xj, yj = outline[j]
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi
        ):
            inside = not inside
        j = i
    return inside


def _min_edge_distance(hole: Tuple[float, float], outline: List[Tuple[float, float]]) -> float:
    if len(outline) < 2:
        return float("inf")
    px, py = hole
    return min(
        _point_to_segment_dist(px, py, outline[i][0], outline[i][1],
                               outline[(i + 1) % len(outline)][0], outline[(i + 1) % len(outline)][1])
        for i in range(len(outline))
    )


def verify_bolt_group(
    group: BoltGroup,
    *,
    min_edge_factor: float = 1.2,
    min_spacing_factor: float = 3.0,
) -> Dict[str, Any]:
    """验算螺栓群：边距 e1/e2 ≥ 1.2·d0，孔距 ≥ 3·d0，孔须在板内。"""
    d0 = group.spec.hole_diameter_mm
    min_edge = min_edge_factor * d0
    min_spacing = min_spacing_factor * d0
    issues: List[str] = []
    edge_checks: List[Dict[str, Any]] = []
    spacing_checks: List[Dict[str, Any]] = []

    outline = group.plate_outline or []
    has_valid_outline = len(outline) >= 3

    if not has_valid_outline:
        issues.append("缺少有效节点板轮廓（≥3 顶点），边距验算 pending")
    else:
        for i, h in enumerate(group.holes):
            if not _point_in_polygon(h[0], h[1], outline):
                issues.append(f"孔{i} 位于板轮廓外")
                edge_checks.append({"hole_index": i, "inside_plate": False, "passed": False})
                continue
            e = _min_edge_distance(h, outline)
            ok = e >= min_edge
            edge_checks.append({
                "hole_index": i,
                "inside_plate": True,
                "edge_distance_mm": round(e, 2),
                "passed": ok,
            })
            if not ok:
                issues.append(f"孔{i} 边距 {e:.1f}mm < {min_edge:.1f}mm (1.2*d0)")

    for i in range(len(group.holes)):
        for j in range(i + 1, len(group.holes)):
            d = math.hypot(
                group.holes[i][0] - group.holes[j][0],
                group.holes[i][1] - group.holes[j][1],
            )
            ok = d >= min_spacing
            spacing_checks.append({"pair": [i, j], "spacing_mm": round(d, 2), "passed": ok})
            if not ok:
                issues.append(f"孔{i}-孔{j} 间距 {d:.1f}mm < {min_spacing:.1f}mm (3d0)")

    if len(group.holes) < group.spec.count:
        issues.append(f"孔位数 {len(group.holes)} < 标注数量 {group.spec.count}")

    if not has_valid_outline:
        status = ValidationStatus.PENDING
        passed = False
    elif issues:
        status = ValidationStatus.FAILED
        passed = False
    else:
        status = ValidationStatus.PASSED
        passed = True

    return {
        "group_id": group.group_id,
        "passed": passed,
        "status": status.value,
        "min_edge_required_mm": round(min_edge, 2),
        "min_spacing_required_mm": round(min_spacing, 2),
        "edge_checks": edge_checks,
        "spacing_checks": spacing_checks,
        "issues": issues,
    }


def bolt_group_detail_key(group_id: str) -> str:
    """bolt group id → 节点板 detail key（D1_B1 → D1，兼容 cross_file 前缀）。"""
    gid = (
        group_id.split("__bolt_group_", 1)[-1]
        if "__bolt_group_" in group_id
        else group_id.removeprefix("bolt_group_")
    )
    return gid.rsplit("_B", 1)[0] if "_B" in gid else gid


def inject_bolt_verification_rule(
    model,
    group: BoltGroup,
    result: Dict[str, Any],
    *,
    component_id: Optional[str] = None,
) -> None:
    rid = f"r_bolt_group_{group.group_id}"
    st = ValidationStatus(result["status"])
    applies = component_id or f"bolt_group_{group.group_id}"
    model.add_rule(Rule(
        id=rid,
        name=f"螺栓群验算 {group.group_id}",
        description="边距 e1/e2 与孔距 3d0 校验",
        applies_to=[applies],
        status=st,
        message="; ".join(result["issues"]) if result["issues"] else "passed",
    ))
