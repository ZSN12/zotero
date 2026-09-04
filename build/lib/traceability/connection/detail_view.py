"""大样详图局部坐标 → 全局空间坐标变换（Gap 2）。

图纸中的局部放大圈（如「节点 K1 大样 1:10」）需在 EngineeringModel 中
建立可验证的变换链：detail_local → sheet → global。

scale_to_real：大样标题 1:N 表示纸面 1 单位 = 实长 N mm（如 1:10 → ×10）。
未锚定到全局节点前，禁止输出 polygon_global。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel, SourceRef, SourceType


@dataclass
class DetailViewTransform:
    """大样视图变换参数。"""
    detail_id: str
    scale_to_real: float = 1.0       # 纸面坐标 → 实长 mm（1:10 → 10.0）
    origin_local: Tuple[float, float] = (0.0, 0.0)
    origin_global: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_deg: float = 0.0
    anchor_node_id: Optional[str] = None
    anchored: bool = False           # 仅 anchored=True 时可输出全局坐标
    source: Optional[SourceRef] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detail_id": self.detail_id,
            "scale_to_real": self.scale_to_real,
            "origin_local": list(self.origin_local),
            "origin_global": list(self.origin_global),
            "rotation_deg": self.rotation_deg,
            "anchor_node_id": self.anchor_node_id,
            "anchored": self.anchored,
        }


_DETAIL_SCALE_RE = re.compile(
    r"(?:1\s*[:：/]\s*(\d+(?:\.\d+)?))|(?:比例\s*1\s*[:：/]\s*(\d+))",
    re.IGNORECASE,
)
_DETAIL_NODE_RE = re.compile(r"(?:节点\s*)?([Kk]\d+|[Mm]\d+)", re.IGNORECASE)


def parse_detail_view_meta(title: str, region: Optional[List[float]] = None) -> DetailViewTransform:
    """从标题/区域解析大样元数据（确定性规则，不猜全局坐标）。"""
    detail_id = "detail"
    m = _DETAIL_NODE_RE.search(title or "")
    if m:
        detail_id = m.group(1).upper()
    scale_to_real = 1.0
    sm = _DETAIL_SCALE_RE.search(title or "")
    if sm:
        denom = float(sm.group(1) or sm.group(2))
        if denom > 0:
            # 1:10 → 纸面 1mm 图距 = 实长 10mm
            scale_to_real = denom
    origin_local = (0.0, 0.0)
    if region and len(region) >= 4:
        origin_local = (float(region[0]), float(region[2]))
    return DetailViewTransform(
        detail_id=detail_id,
        scale_to_real=scale_to_real,
        origin_local=origin_local,
        anchored=False,
        source=SourceRef(SourceType.DRAWING, title or "detail", detail=title, confidence=0.7),
    )


def anchor_transform(
    transform: DetailViewTransform,
    origin_global: Tuple[float, float, float],
    anchor_node_id: Optional[str] = None,
) -> DetailViewTransform:
    """人工或求解器锚定大样到全局坐标系。"""
    transform.origin_global = origin_global
    transform.anchor_node_id = anchor_node_id
    transform.anchored = True
    return transform


def local_to_global(
    x_local: float,
    y_local: float,
    transform: DetailViewTransform,
    z_global: Optional[float] = None,
) -> Optional[Tuple[float, float, float]]:
    """大样局部纸面坐标 → 全局实长 (mm)。未 anchored 返回 None。"""
    if not transform.anchored:
        return None
    rad = math.radians(transform.rotation_deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    lx = (x_local - transform.origin_local[0]) * transform.scale_to_real
    ly = (y_local - transform.origin_local[1]) * transform.scale_to_real
    gx = transform.origin_global[0] + lx * cos_r - ly * sin_r
    gy = transform.origin_global[1] + lx * sin_r + ly * cos_r
    gz = z_global if z_global is not None else transform.origin_global[2]
    return round(gx, 2), round(gy, 2), round(gz, 2)


def attach_detail_transform(model: EngineeringModel, transform: DetailViewTransform) -> None:
    """把大样变换写入 detail_view 组件（默认 pending，无全局坐标）。"""
    cid = f"detail_view_{transform.detail_id}"
    model.add_component(Component(
        id=cid,
        name=f"大样 {transform.detail_id}",
        kind="detail_view",
        source=transform.source,
        properties={
            **transform.to_dict(),
            "solve_status": "pending_review" if not transform.anchored else "verified",
        },
    ))
    df = model.components.get("drawing_file")
    if df:
        df.properties.setdefault("detail_views", []).append(transform.detail_id)
