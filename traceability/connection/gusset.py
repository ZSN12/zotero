"""节点板（Gusset Plate）实体模型（Gap 2）。"""

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
            if self.transform.anchored:
                global_poly = []
                for p in self.polygon_local:
                    gp = local_to_global(p[0], p[1], self.transform)
                    if gp is not None:
                        global_poly.append(list(gp))
                if global_poly:
                    props["polygon_global"] = global_poly
                    props["solve_status"] = "verified"
            else:
                props["global_coords"] = "pending_anchor"
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
    src = comp.source
    if plate.thickness_mm is not None:
        model.add_dimension(Dimension(
            id=f"dim_gusset_t_{plate.plate_id}",
            name=f"节点板厚度 {plate.plate_id}",
            value=plate.thickness_mm,
            unit="mm",
            origin=DimensionOrigin.MEASURED,
            source=src,
            applies_to=comp.id,
        ))
    else:
        model.add_dimension(Dimension(
            id=f"dim_gusset_t_{plate.plate_id}",
            name=f"节点板厚度 {plate.plate_id}",
            value=None,
            unit="mm",
            origin=DimensionOrigin.PLACEHOLDER,
            source=src,
            applies_to=comp.id,
        ))
    return comp


def _triangulate_polygon(pts):
    """Ear-clipping 三角化简单多边形（支持凹形），返回 CCW 三角形索引表。

    输入 pts 为去重后的多边形顶点（任意绕向）。O(n²) 对节点板轮廓（n<100）
    足够；找不到耳即失败（自交输入），由调用方兜底 watertight 校验。
    """
    def shoelace(poly):
        return sum(poly[i][0] * poly[(i + 1) % len(poly)][1]
                   - poly[(i + 1) % len(poly)][0] * poly[i][1]
                   for i in range(len(poly))) / 2.0

    def tri_area(a, b, c):
        return ((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0

    def point_in_tri(p, a, b, c):
        d1 = tri_area(a, b, p); d2 = tri_area(b, c, p); d3 = tri_area(c, a, p)
        has_neg = min(d1, d2, d3) < -1e-9
        has_pos = max(d1, d2, d3) > 1e-9
        return not (has_neg and has_pos)

    idx = list(range(len(pts)))
    flip = shoelace(pts) < 0
    if flip:
        idx.reverse()
    work = [pts[i] for i in idx]
    tris = []
    guard = 0
    while len(idx) > 3 and guard < 10000:
        guard += 1
        m = len(idx)
        clipped = False
        for i in range(m):
            ia, ib, ic = idx[(i - 1) % m], idx[i], idx[(i + 1) % m]
            a, b, c = pts[ia], pts[ib], pts[ic]
            if tri_area(a, b, c) <= 1e-9:      # 凹角/退化，不是耳
                continue
            if any(point_in_tri(pts[j], a, b, c) for j in idx if j not in (ia, ib, ic)):
                continue
            tris.append((ia, ib, ic) if not flip else (ia, ic, ib))
            idx.pop(i)
            clipped = True
            break
        if not clipped:
            raise ValueError("ear clipping failed: polygon self-intersecting or degenerate")
    if len(idx) == 3:
        tris.append((idx[0], idx[1], idx[2]) if not flip else (idx[0], idx[2], idx[1]))
    else:
        raise ValueError("ear clipping failed: residual polygon")
    return tris


def make_gusset_shell(polygon_2d, thickness_mm):
    """Create a watertight thin plate from a 2-D simple polygon (T4: concave-safe).

    Caps are triangulated with ear clipping (handles concave node plates), walls
    extruded along +Z by thickness_mm. No optional triangulation backend needed.
    """
    import numpy as np
    import trimesh
    pts = [(float(p[0]), float(p[1])) for p in polygon_2d]
    if len(pts) < 3 or float(thickness_mm) <= 0:
        raise ValueError("gusset shell requires >=3 points and positive thickness")
    # Remove a repeated closing point and consecutive duplicates.
    if pts[-1] == pts[0]:
        pts.pop()
    clean = []
    for p in pts:
        if not clean or p != clean[-1]:
            clean.append(p)
    pts = clean
    if len(pts) < 3:
        raise ValueError("degenerate gusset polygon")
    area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))
    if abs(area) < 1e-9:
        raise ValueError("degenerate gusset polygon")
    if area < 0:
        pts.reverse()
    n = len(pts)
    verts = np.array([(x, y, 0.0) for x, y in pts] + [(x, y, float(thickness_mm)) for x, y in pts], dtype=float)
    # T4：ear clipping 三角化（凹多边形安全）——原扇形切法只对凸轮廓可靠，
    # 凹节点板会产生穿出轮廓/自交的三角形。
    tris = _triangulate_polygon(pts)
    faces = []
    for (a, b, c) in tris:
        faces.append((a, c, b))            # 底盖（法向 -Z）
        faces.append((n + a, n + b, n + c))  # 顶盖（法向 +Z）
    for i in range(n):
        j = (i + 1) % n
        faces.extend(((i, j, n + j), (i, n + j, n + i)))
    mesh = trimesh.Trimesh(vertices=verts, faces=np.asarray(faces, dtype=np.int64), process=True)
    if not mesh.is_watertight:
        raise ValueError("gusset shell is not watertight")
    return mesh
