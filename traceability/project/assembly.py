"""多模块分段拼装求解器（Gap 1）。

自动将模块 Mk 的顶端边界节点与 Mk+1 的底端边界节点在公差范围内贴合并闭合。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel


@dataclass
class ModuleBoundary:
    """模块拼接面：一组边界节点 + 期望对齐轴。"""
    module_id: str
    face: str  # top | bottom
    node_ids: List[str] = field(default_factory=list)
    z_level: Optional[float] = None


def _boundary_nodes(model: EngineeringModel, face: str, tol_z: float = 50.0) -> List[str]:
    """取模块模型在 top/bottom 面的边界节点（按 z 极值聚类）。"""
    nodes = [(cid, c) for cid, c in model.components.items() if c.kind == "tower_node"]
    zs = []
    for cid, c in nodes:
        z = c.properties.get("z")
        if z is None:
            z = c.properties.get("view_y")  # 立面投影高度
        if z is not None:
            zs.append((float(z), cid))
    if not zs:
        return [cid for cid, _ in nodes]
    if face == "top":
        target = max(z for z, _ in zs)
    else:
        target = min(z for z, _ in zs)
    return [cid for z, cid in zs if abs(z - target) <= tol_z]


def _node_xyz(comp: Component) -> Optional[Tuple[float, float, float]]:
    p = comp.properties
    vals = [p.get("x"), p.get("y"), p.get("z")]
    if any(v is None for v in vals):
        return None
    return float(vals[0]), float(vals[1]), float(vals[2])


def align_boundary_pair(
    lower: EngineeringModel,
    upper: EngineeringModel,
    *,
    tol_mm: float = 5.0,
) -> Dict[str, Any]:
    """将 lower 模块 top 面与 upper 模块 bottom 面节点配对（最近邻 + 公差）。"""
    lower_ids = _boundary_nodes(lower, "top")
    upper_ids = _boundary_nodes(upper, "bottom")
    pairs: List[Dict[str, Any]] = []
    used_upper: set = set()

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
            d = math.dist(lxyz[:2], uxyz[:2])  # XY 平面距离
            if d < best_d:
                best_d, best_uid = d, uid
        if best_uid and best_d <= tol_mm * 10:  # 拼接面 XY 容差放宽
            used_upper.add(best_uid)
            uc = upper.components[best_uid]
            lxyz = _node_xyz(lc)
            uxyz = _node_xyz(uc)
            dz = (lxyz[2] - uxyz[2]) if lxyz and uxyz else None
            pairs.append({
                "lower_node": lid,
                "upper_node": best_uid,
                "xy_distance_mm": round(best_d, 3),
                "dz_mm": round(dz, 3) if dz is not None else None,
                "within_tol": best_d <= tol_mm,
            })
            # 对齐：upper 节点 XY 贴到 lower（Z 保留 upper 模块内相对高度）
            if lxyz and uxyz:
                uc.properties["x"] = round(lxyz[0], 2)
                uc.properties["y"] = round(lxyz[1], 2)
                uc.properties["assembly_aligned_to"] = lid
                uc.properties["solve_status"] = "assembly_aligned"

    return {
        "lower_module": lower.name,
        "upper_module": upper.name,
        "pairs": pairs,
        "matched": len(pairs),
        "tol_mm": tol_mm,
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
    reports: List[Dict[str, Any]] = []

    # 合并组件（带模块前缀）
    for i, model in enumerate(models, start=1):
        prefix = f"m{i:02d}_"
        for cid, comp in model.components.items():
            if comp.kind not in ("tower_bar", "tower_node", "drawing_file"):
                continue
            new_id = f"{prefix}{cid}"
            props = dict(comp.properties)
            props["module_index"] = i
            props["source_module"] = model.name
            merged.add_component(type(comp)(
                id=new_id, name=comp.name, kind=comp.kind,
                source=comp.source, properties=props, tags=list(comp.tags),
            ))

    # 逐对对齐边界
    prefixed = []
    for i, model in enumerate(models, start=1):
        sub = EngineeringModel(name=model.name)
        prefix = f"m{i:02d}_"
        for cid, comp in merged.components.items():
            if cid.startswith(prefix):
                orig = cid[len(prefix):]
                sub.components[orig] = comp
        prefixed.append(sub)

    for i in range(len(prefixed) - 1):
        rep = align_boundary_pair(prefixed[i], prefixed[i + 1], tol_mm=tol_mm)
        reports.append(rep)

    df = merged.components.setdefault("drawing_file", Component(
        id="drawing_file", name="装配模型", kind="drawing_file",
        properties={"view_mode": "multi_module_assembly"},
    ))
    df.properties["assembly_reports"] = reports
    df.properties["module_count"] = len(models)
    return merged, reports
