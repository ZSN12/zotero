"""节点板（Gusset Plate）实体模型（Gap 2）。

从 DXF 大样多边形 + 厚度标注生成 gusset_plate 组件，孔位单独挂 bolt_hole 子属性。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel, Dimension, DimensionOrigin, SourceRef, SourceType
from .detail_view import DetailViewTransform, local_to_global


@dataclass
class GussetPlate:
    plate_id: str
    polygon_local: List[Tuple[float, float]]
    thickness_mm: Optional[float] = None
    chamfers: List[Dict[str, float]] = field(default_factory=list)
    bolt_holes: List[Dict[str, Any]] = field(default_factory=list)
    material: str = ""
    transform: Optional[DetailViewTransform] = None

    def to_component(self) -> Component:
        props: Dict[str, Any] = {
            "polygon_local": [list(p) for p in self.polygon_local],
            "thickness_mm": self.thickness_mm,
            "chamfers": self.chamfers,
            "bolt_holes": self.bolt_holes,
            "material": self.material,
            "solve_status": "pending_review",
        }
        if self.transform:
            props["detail_id"] = self.transform.detail_id
            props["transform"] = self.transform.to_dict()
            props["polygon_global"] = [
                list(local_to_global(p[0], p[1], self.transform)) for p in self.polygon_local
            ]
        return Component(
            id=f"gusset_{self.plate_id}",
            name=f"节点板 {self.plate_id}",
            kind="gusset_plate",
            source=SourceRef(SourceType.DRAWING, self.plate_id, confidence=0.65),
            properties=props,
        )


_THICKNESS_RE = re.compile(
    r"(?:t\s*=?\s*|厚度\s*)(\d+(?:\.\d+)?)\s*(?:mm)?",
    re.IGNORECASE,
)


def parse_gusset_from_detail(
    plate_id: str,
    polygon_local: List[Tuple[float, float]],
    *,
    thickness_text: Optional[str] = None,
    transform: Optional[DetailViewTransform] = None,
    bolt_holes: Optional[List[Dict[str, Any]]] = None,
) -> GussetPlate:
    """从大样几何 + 文字标注解析节点板（缺厚度 → placeholder）。"""
    thickness = None
    if thickness_text:
        m = _THICKNESS_RE.search(thickness_text)
        if m:
            thickness = float(m.group(1))
    return GussetPlate(
        plate_id=plate_id,
        polygon_local=polygon_local,
        thickness_mm=thickness,
        bolt_holes=bolt_holes or [],
        transform=transform,
    )


def add_gusset_to_model(model: EngineeringModel, plate: GussetPlate) -> Component:
    comp = plate.to_component()
    model.add_component(comp)
    if plate.thickness_mm is not None:
        model.add_dimension(Dimension(
            id=f"dim_gusset_t_{plate.plate_id}",
            name=f"节点板厚度 {plate.plate_id}",
            value=plate.thickness_mm,
            unit="mm",
            origin=DimensionOrigin.MEASURED,
            applies_to=comp.id,
        ))
    else:
        model.add_dimension(Dimension(
            id=f"dim_gusset_t_{plate.plate_id}",
            name=f"节点板厚度 {plate.plate_id}",
            value=None,
            unit="mm",
            origin=DimensionOrigin.PLACEHOLDER,
            applies_to=comp.id,
        ))
    return comp
