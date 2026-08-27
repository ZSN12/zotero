"""多模块分段拼装求解器（Gap 1）。

自动将模块 Mk 的顶端边界节点与 Mk+1 的底端边界节点在公差范围内贴合并闭合。
对齐策略：匹配边界节点对 → 计算 XY 平移 + Z 堆叠 → 对整个上模块刚体平移。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, Dimension, EngineeringModel


@dataclass
class ModuleBoundary:
    """模块拼接面：一组边界节点 + 期望对齐轴。"""
    module_id: str
    face: str  # top | bottom
    node_ids: List[str] = field(default_factory=list)
    z_level: Optional[float] = None


def _node_z(comp: Component) -> Optional[float]:
    p = comp.properties
    z = p.get("z")
    if z is None:
        z = p.get("view_y")
    return float(z) if z is not None else None


def _boundary_nodes(model: EngineeringModel, face: str, tol_z: float = 50.0) -> List[str]:
    """取模块模型在 top/bottom 面的边界节点（按 z 极值聚类）。"""
    nodes = [(cid, c) for cid, c in model.components.items() if c.kind == "tower_node"]
    zs = [(float(z), cid) for cid, c in nodes if (z := _node_z(c)) is not None]
    if not zs:
        return [cid for cid, _ in nodes]
    target = max(z for z, _ in zs) if face == "top" else min(z for z, _ in zs)
    return [cid for z, cid in zs if abs(z - target) <= tol_z]


def _node_xyz(comp: Component) -> Optional[Tuple[float, float, float]]:
    p = comp.properties
    vals = [p.get("x"), p.get("y"), p.get("z")]
    if any(v is None for v in vals):
        return None
    return float(vals[0]), float(vals[1]), float(vals[2])


def _translate_component(comp: Component, dx: float, dy: float, dz: float) -> None:
    p = comp.properties
    for key in ("x", "view_x", "x1", "x2", "x_px"):
        if p.get(key) is not None:
            p[key] = round(float(p[key]) + dx, 2)
    for key in ("y", "view_y", "y1", "y2", "y_px"):
        if p.get(key) is not None:
            p[key] = round(float(p[key]) + dy, 2)
    for key in ("z", "view_y"):
        if key == "z" and p.get("z") is not None:
            p["z"] = round(float(p["z"]) + dz, 2)


def align_boundary_pair(
    lower: EngineeringModel,
    upper: EngineeringModel,
    *,
    tol_mm: float = 5.0,
) -> Dict[str, Any]:
    """将 lower 模块 top 面与 upper 模块 bottom 面节点配对，并对 upper 整模块刚体平移。"""
    lower_ids = _boundary_nodes(lower, "top")
    upper_ids = _boundary_nodes(upper, "bottom")
    pairs: List[Dict[str, Any]] = []
    used_upper: set = set()
    translations: List[Tuple[float, float, float]] = []

    for lid in lower_ids:
        lc = lower.components.get(lid)
        if not lc:
            continue
        lxyz = _node_xyz(lc)
        if not lxyz:
            continue
        best_uid, best_d = None, float("inf")
        for uid in upper_ids:
            if uid in used_upper:
                continue
            uc = upper.components.get(uid)
            if not uc:
                continue
            uxyz = _node_xyz(uc)
            if not uxyz:
                continue
            d = math.dist(lxyz[:2], uxyz[:2])
            if d < best_d:
                best_d, best_uid = d, uid
        if best_uid is None or best_d > tol_mm:
            continue
        used_upper.add(best_uid)
        uc = upper.components[best_uid]
        uxyz = _node_xyz(uc)
        if not uxyz:
            continue
        dx, dy = lxyz[0] - uxyz[0], lxyz[1] - uxyz[1]
        dz = lxyz[2] - uxyz[2]
        translations.append((dx, dy, dz))
        pairs.append({
            "lower_node": lid,
            "upper_node": best_uid,
            "xy_distance_mm": round(best_d, 3),
            "dz_mm": round(dz, 3),
            "within_tol": True,
        })

    applied = False
    if translations:
        dx = sum(t[0] for t in translations) / len(translations)
        dy = sum(t[1] for t in translations) / len(translations)
        dz = sum(t[2] for t in translations) / len(translations)
        for comp in upper.components.values():
            if comp.kind in ("tower_node", "tower_bar"):
                _translate_component(comp, dx, dy, dz)
                if comp.kind == "tower_node":
                    comp.properties["assembly_aligned_to"] = "module_boundary"
                    comp.properties["solve_status"] = "assembly_aligned"
        applied = True

    return {
        "lower_module": lower.name,
        "upper_module": upper.name,
        "pairs": pairs,
        "matched": len(pairs),
        "within_tol_matched": len(pairs),
        "tol_mm": tol_mm,
        "rigid_translation_applied": applied,
    }


def assemble_modules(
    models: List[EngineeringModel],
    *,
    tol_mm: float = 5.0,
) -> Tuple[EngineeringModel, List[Dict[str, Any]]]:
    """按顺序拼接多个模块模型，返回合并模型 + 每对拼接报告。"""
    if not models:
        raise ValueError("assemble_modules 需要至少一个模块模型")

    merged = EngineeringModel(name="tower-assembly-merged")
    id_map: Dict[str, str] = {}  # old_id -> prefixed_id per source model

    for i, model in enumerate(models, start=1):
        prefix = f"m{i:02d}_"
        local_map: Dict[str, str] = {}
        for cid, comp in model.components.items():
            if comp.kind not in ("tower_bar", "tower_node", "drawing_file"):
                continue
            new_id = f"{prefix}{cid}"
            local_map[cid] = new_id
            props = dict(comp.properties)
            props["module_index"] = i
            props["source_module"] = model.name
            merged.add_component(type(comp)(
                id=new_id, name=comp.name, kind=comp.kind,
                source=comp.source, properties=props, tags=list(comp.tags),
            ))
        for did, dim in model.dimensions.items():
            new_did = f"{prefix}{did}"
            applies = dim.applies_to
            if applies and applies in local_map:
                applies = local_map[applies]
            elif applies and applies in model.components:
                applies = local_map.get(applies, f"{prefix}{applies}")
            merged.add_dimension(Dimension(
                id=new_did,
                name=dim.name,
                value=dim.value,
                unit=dim.unit,
                origin=dim.origin,
                source=dim.source,
                applies_to=applies,
                status=dim.status,
            ))
        id_map.update(local_map)

    # 杆件 from/to 节点引用按前缀重指
    for i, model in enumerate(models, start=1):
        prefix = f"m{i:02d}_"
        for cid, comp in model.components.items():
            if comp.kind != "tower_bar":
                continue
            new_bar = merged.components.get(f"{prefix}{cid}")
            if not new_bar:
                continue
            for end in ("from_node", "to_node"):
                nid = comp.properties.get(end)
                if nid:
                    new_bar.properties[end] = f"{prefix}{nid}"

    # 逐对对齐边界（在 prefixed 子模型视图上操作，对象与 merged 共享引用）
    prefixed: List[EngineeringModel] = []
    for i, model in enumerate(models, start=1):
        sub = EngineeringModel(name=model.name)
        prefix = f"m{i:02d}_"
        for cid, comp in merged.components.items():
            if cid.startswith(prefix):
                sub.components[cid[len(prefix):]] = comp
        prefixed.append(sub)

    reports: List[Dict[str, Any]] = []
    for i in range(len(prefixed) - 1):
        reports.append(align_boundary_pair(prefixed[i], prefixed[i + 1], tol_mm=tol_mm))

    df = merged.components.setdefault("drawing_file", Component(
        id="drawing_file", name="装配模型", kind="drawing_file",
        properties={"view_mode": "multi_module_assembly"},
    ))
    df.properties["assembly_reports"] = reports
    df.properties["module_count"] = len(models)
    return merged, reports
