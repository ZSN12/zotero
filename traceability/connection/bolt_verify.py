"""螺栓群构造与力学验算（Gap 2）。

解析图纸螺栓标注（如 2M16X50），校验边距 e1/e2、孔距 3d0、螺栓干涉。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, Rule, ValidationStatus


@dataclass
class BoltSpec:
    count: int
    diameter_mm: float
    length_mm: float

    @property
    def hole_diameter_mm(self) -> float:
        return self.diameter_mm + 1.5  # 标准孔径近似 d+1.5


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
    holes: List[Tuple[float, float]]  # 孔中心 (x, y) local or global
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


def _min_edge_distance(hole: Tuple[float, float], outline: List[Tuple[float, float]]) -> float:
    if len(outline) < 2:
        return float("inf")
    px, py = hole
    best = float("inf")
    for i in range(len(outline)):
        ax, ay = outline[i]
        bx, by = outline[(i + 1) % len(outline)]
        best = min(best, _point_to_segment_dist(px, py, ax, ay, bx, by))
    return best


def verify_bolt_group(
    group: BoltGroup,
    *,
    min_edge_factor: float = 1.2,
    min_spacing_factor: float = 3.0,
) -> Dict[str, Any]:
    """验算螺栓群：边距 e1/e2 ≥ min_edge_factor * d0，孔距 ≥ min_spacing_factor * d0。"""
    d0 = group.spec.hole_diameter_mm
    min_edge = min_edge_factor * d0
    min_spacing = min_spacing_factor * d0
    issues: List[str] = []
    edge_checks: List[Dict[str, Any]] = []
    spacing_checks: List[Dict[str, Any]] = []

    if group.plate_outline:
        for i, h in enumerate(group.holes):
            e = _min_edge_distance(h, group.plate_outline)
            ok = e >= min_edge
            edge_checks.append({"hole_index": i, "edge_distance_mm": round(e, 2), "passed": ok})
            if not ok:
                issues.append(f"孔{i} 边距 {e:.1f}mm < {min_edge:.1f}mm (1.2*d0)")

    for i in range(len(group.holes)):
        for j in range(i + 1, len(group.holes)):
            d = math.hypot(
                group.holes[i][0] - group.holes[j][0],
                group.holes[i][1] - group.holes[j][1],
            )
            ok = d >= min_spacing
            spacing_checks.append({
                "pair": [i, j],
                "spacing_mm": round(d, 2),
                "passed": ok,
            })
            if not ok:
                issues.append(f"孔{i}-孔{j} 间距 {d:.1f}mm < {min_spacing:.1f}mm (3d0)")

    passed = len(issues) == 0 and len(group.holes) >= group.spec.count
    if len(group.holes) < group.spec.count:
        issues.append(f"孔位数 {len(group.holes)} < 标注数量 {group.spec.count}")

    return {
        "group_id": group.group_id,
        "passed": passed,
        "status": ValidationStatus.PASSED.value if passed else ValidationStatus.FAILED.value,
        "min_edge_required_mm": round(min_edge, 2),
        "min_spacing_required_mm": round(min_spacing, 2),
        "edge_checks": edge_checks,
        "spacing_checks": spacing_checks,
        "issues": issues,
    }


def inject_bolt_verification_rule(model, group: BoltGroup, result: Dict[str, Any]) -> None:
    rid = f"r_bolt_group_{group.group_id}"
    model.add_rule(Rule(
        id=rid,
        name=f"螺栓群验算 {group.group_id}",
        description="边距 e1/e2 与孔距 3d0 校验",
        applies_to=[f"bolt_group_{group.group_id}"],
        status=ValidationStatus.PASSED if result["passed"] else ValidationStatus.FAILED,
        message="; ".join(result["issues"]) if result["issues"] else "passed",
    ))
