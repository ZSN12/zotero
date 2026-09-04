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


def bolt_assembly_meshes(group, plate_normal=(0.0, 0.0, 1.0), plate_center=(0.0, 0.0, 0.0)):
    """生成一个 bolt_group 的六角头、垫圈、杆和螺母合并网格。

    ``group`` 可为 Component 或包含 holes/component_id 的字典；holes 是板局部
    2D 坐标，整体以孔心质心居中。plate_center 是板中面中心，法向定义层叠轴。
    """
    import numpy as np
    import trimesh

    props = getattr(group, "properties", group if isinstance(group, dict) else {})
    holes = [h for h in (props.get("holes") or []) if len(h) >= 2]
    if not holes:
        empty = trimesh.Trimesh(vertices=np.empty((0, 3)), faces=np.empty((0, 3), dtype=np.int64))
        empty.metadata = {"component_id": getattr(group, "id", None) or props.get("component_id"), "bolt_count": 0}
        return empty
    n = np.asarray(plate_normal, dtype=float)
    if n.shape != (3,) or not np.all(np.isfinite(n)) or np.linalg.norm(n) < 1e-12:
        raise ValueError("plate_normal must be a finite non-zero 3-vector")
    n /= np.linalg.norm(n)
    c = np.asarray(plate_center, dtype=float)
    if c.shape != (3,) or not np.all(np.isfinite(c)):
        raise ValueError("plate_center must be a finite 3-vector")
    # For the common horizontal plate preserve drawing local +X/+Y.  For an
    # arbitrary normal choose a deterministic right-handed in-plane frame.
    if np.allclose(n, (0.0, 0.0, 1.0), atol=1e-8):
        u, v = np.array((1.0, 0.0, 0.0)), np.array((0.0, 1.0, 0.0))
    elif np.allclose(n, (0.0, 0.0, -1.0), atol=1e-8):
        u, v = np.array((1.0, 0.0, 0.0)), np.array((0.0, -1.0, 0.0))
    else:
        ref = np.array((0.0, 0.0, 1.0)) if abs(n[2]) < 0.9 else np.array((1.0, 0.0, 0.0))
        u = np.cross(ref, n); u /= np.linalg.norm(u)
        v = np.cross(n, u)
    # Do not re-center individual groups: all groups share the caller's plate
    # coordinate origin, so their relative locations remain intact.
    centers = np.asarray([[float(h[0]), float(h[1])] for h in holes])
    thickness = float(props.get("plate_thickness_mm") or 8.0)
    meshes = []
    for xy in centers:
        p = c + u * xy[0] + v * xy[1]
        def part(radius, height, sections, axial_center):
            m = trimesh.creation.cylinder(radius=radius, height=height, sections=sections)
            # map local z to n and translate to requested axial coordinate
            T = np.eye(4); T[:3, :3] = np.column_stack((u, v, n)); T[:3, 3] = p + n * axial_center
            m.apply_transform(T)
            return m
        # Plate mid-plane is w=0.  Head-side washer/head and nut-side shank
        # are placed by accumulated interfaces (no magic offsets).
        washer_h, head_h, nut_h = 2.0, 10.0, 8.0
        washer_center = thickness / 2 + washer_h / 2
        head_center = thickness / 2 + washer_h + head_h / 2
        nut_center = -thickness / 2 - washer_h - nut_h / 2
        # Nominal bolt length is measured from the head bearing face toward
        # the tail; keep the requested M16x40 rod length rather than deriving
        # a shorter value from the decorative nut placement.
        shank_h = float(props.get("length_mm") or 40.0)
        shank_top = thickness / 2 + washer_h
        shank_center = shank_top - shank_h / 2
        meshes.extend((part(13.9, head_h, 6, head_center),
                       part(16.0, washer_h, 16, washer_center),
                       part(float(props.get("diameter_mm") or 16.0) / 2, shank_h, 20, shank_center),
                       part(13.9, nut_h, 6, nut_center)))
    merged = trimesh.util.concatenate(meshes)
    merged.metadata = {"component_id": getattr(group, "id", None) or props.get("component_id") or props.get("group_id"),
                       "bolt_count": len(centers), "bolt_parts_per_bolt": 4,
                       "plate_normal": n.tolist(), "plate_center": c.tolist()}
    try:
        merged.visual.material = trimesh.visual.material.PBRMaterial(name="hot_dip_galvanized", metallicFactor=0.85, roughnessFactor=0.40, baseColorFactor=[170, 175, 182, 255])
    except Exception:
        pass
    return merged


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
