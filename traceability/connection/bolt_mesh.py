"""螺栓群 GLB 可视化（M6 / Gap 2）。"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..model import EngineeringModel
from .detail_view import DetailViewTransform, local_to_global
from .gusset_anchor import _transform_from_dict


def _gusset_for_bolt(model: EngineeringModel, bolt_cid: str) -> Optional[str]:
    """按 bolt_group id 前缀匹配 gusset（D1_B1 → gusset_*D1）。"""
    gid = bolt_cid.split("__bolt_group_", 1)[-1] if "__bolt_group_" in bolt_cid else bolt_cid.removeprefix("bolt_group_")
    plate_key = gid.rsplit("_B", 1)[0]
    for cid, comp in model.components.items():
        if comp.kind != "gusset_plate":
            continue
        detail_id = str(comp.properties.get("detail_id") or "")
        if detail_id == plate_key or cid.endswith(f"gusset_{plate_key}"):
            return cid
    return None


def bolt_holes_global(
    model: EngineeringModel,
    bolt_cid: str,
) -> List[Tuple[float, float, float]]:
    """螺栓孔局部坐标 → 全局（依赖已锚定 gusset transform）。"""
    bolt = model.components.get(bolt_cid)
    if bolt is None or bolt.kind != "bolt_group":
        return []
    gusset_cid = _gusset_for_bolt(model, bolt_cid)
    if not gusset_cid:
        return []
    gusset = model.components.get(gusset_cid)
    if gusset is None:
        return []
    tdict = dict(gusset.properties.get("transform") or {})
    if not tdict.get("anchored"):
        return []
    transform = _transform_from_dict(tdict)
    transform.origin_local = (0.0, 0.0)
    anchor_id = gusset.properties.get("anchor_node_id")
    z_base: Optional[float] = None
    if anchor_id and anchor_id in model.components:
        z_base = model.components[anchor_id].properties.get("z")
    out: List[Tuple[float, float, float]] = []
    for h in bolt.properties.get("holes") or []:
        if not h or len(h) < 2:
            continue
        gp = local_to_global(float(h[0]), float(h[1]), transform, z_global=float(z_base) if z_base is not None else None)
        if gp is not None:
            out.append(gp)
    return out


def bolt_hole_meshes(model: EngineeringModel):
    """生成螺栓孔圆柱 mesh 列表（trimesh）。"""
    import trimesh

    meshes = []
    meta = []
    for cid, bolt in model.components.items():
        if bolt.kind != "bolt_group":
            continue
        holes = bolt_holes_global(model, cid)
        if not holes:
            continue
        hole_d = float(bolt.properties.get("hole_diameter_mm") or bolt.properties.get("diameter_mm") or 16) + 1.5
        radius = max(hole_d / 2.0, 2.0)
        height = float(bolt.properties.get("length_mm") or 40.0)
        gid = cid.split("__bolt_group_", 1)[-1] if "__bolt_group_" in cid else cid.removeprefix("bolt_group_")
        for i, (x, y, z) in enumerate(holes):
            cyl = trimesh.creation.cylinder(radius=radius, height=height, sections=10)
            cyl.apply_translation([x, y, z - height / 2.0])
            cyl.visual.face_colors = [90, 90, 90, 255]
            extras = {"bar_id": f"bolt_{gid}_{i}", "component_id": cid, "kind": "bolt_hole"}
            cyl.metadata = dict(extras)
            meshes.append(cyl)
            meta.append(extras)
    return meshes, meta
