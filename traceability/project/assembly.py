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
    """取模块模型在 top/bottom 面的边界节点（按 z 极值聚类）。

    若模块 drawing_file 带 module_z0 / module_z1（Phase 3 切分时写入），
    优先用声明的接口标高；否则按模型内 z 极值推断。
    """
    nodes = [(cid, c) for cid, c in model.components.items() if c.kind == "tower_node"]
    zs = [(float(z), cid) for cid, c in nodes if (z := _node_z(c)) is not None]
    if not zs:
        return [cid for cid, _ in nodes]
    df = model.components.get("drawing_file")
    declared = None
    if df is not None:
        key = "module_z1" if face == "top" else "module_z0"
        v = df.properties.get(key)
        if v is not None:
            declared = float(v)
    if declared is not None:
        # 声明的接口标高优先；若该标高附近无节点，回退到 z 极值。
        near = [cid for z, cid in zs if abs(z - declared) <= max(tol_z, 200.0)]
        if near:
            return near
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


def _estimate_rigid_transform(
    source_pts: List[Tuple[float, float, float]],
    target_pts: List[Tuple[float, float, float]],
) -> Tuple[Tuple[Tuple[float, ...], ...], Tuple[float, float, float]]:
    """Kabsch/Umeyama 求解使 target ≈ R·source + T 的最优刚体变换 [R|T]。

    返回 (R, T)。R 为 3x3 旋转矩阵（嵌套 tuple），T 为平移三元组。
    少于 3 对匹配时退化为纯平移（质心差）。
    """
    import numpy as np

    n = len(source_pts)
    if n == 0:
        raise ValueError("刚体变换需要至少 1 对匹配点")
    src = np.asarray(source_pts, dtype=float)
    dst = np.asarray(target_pts, dtype=float)
    if n < 3:
        t = dst.mean(axis=0) - src.mean(axis=0)
        return tuple(tuple(float(r) for r in row) for row in np.eye(3)), tuple(float(v) for v in t)

    src_c = src - src.mean(axis=0)
    dst_c = dst - dst.mean(axis=0)
    cov = src_c.T @ dst_c
    u, _s, vt = np.linalg.svd(cov)
    diag = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    diag[2, 2] = 1.0 if np.linalg.det(vt.T @ u.T) >= 0 else -1.0
    R = vt.T @ diag @ u.T
    T = dst.mean(axis=0) - R @ src.mean(axis=0)
    R = tuple(tuple(float(r) for r in row) for row in R)
    T = tuple(float(v) for v in T)
    return R, T


def _apply_rigid_transform(
    model: EngineeringModel,
    R: Tuple[Tuple[float, ...], ...],
    T: Tuple[float, float, float],
) -> None:
    """把 [R|T] 应用到模型内所有 tower_node/tower_bar 的三轴坐标。"""
    import numpy as np

    R_arr = np.asarray(R, dtype=float)
    T_arr = np.asarray(T, dtype=float)
    for comp in model.components.values():
        if comp.kind not in ("tower_node", "tower_bar"):
            continue
        p = comp.properties
        if None in (p.get("x"), p.get("y"), p.get("z")):
            continue
        v = np.array([float(p["x"]), float(p["y"]), float(p["z"])])
        w = R_arr @ v + T_arr
        p["x"], p["y"], p["z"] = round(float(w[0]), 3), round(float(w[1]), 3), round(float(w[2]), 3)
        if comp.kind == "tower_node":
            p["assembly_aligned_to"] = "module_boundary"
            p["solve_status"] = "assembly_aligned"


def align_boundary_pair(
    lower: EngineeringModel,
    upper: EngineeringModel,
    *,
    tol_mm: float = 5.0,
    rigid: bool = False,
) -> Dict[str, Any]:
    """将 lower 模块 top 面与 upper 模块 bottom 面节点配对，并对 upper 整模块对齐。

    rigid=False：XY 平移 + Z 堆叠（Gap 1 原有行为）。
    rigid=True ：计算空间刚体变换矩阵 [R|T]（Kabsch），应用到整个 upper 模块。
    """
    lower_ids = _boundary_nodes(lower, "top")
    upper_ids = _boundary_nodes(upper, "bottom")
    pairs: List[Dict[str, Any]] = []
    used_upper: set = set()
    translations: List[Tuple[float, float, float]] = []
    src_pts: List[Tuple[float, float, float]] = []
    dst_pts: List[Tuple[float, float, float]] = []

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
        src_pts.append(uxyz)
        dst_pts.append(lxyz)
        pairs.append({
            "lower_node": lid,
            "upper_node": best_uid,
            "xy_distance_mm": round(best_d, 3),
            "dz_mm": round(dz, 3),
            "within_tol": True,
        })

    applied = False
    R = tuple(tuple(float(r) for r in row) for row in [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    T = (0.0, 0.0, 0.0)
    if src_pts:
        if rigid:
            R, T = _estimate_rigid_transform(src_pts, dst_pts)
            _apply_rigid_transform(upper, R, T)
            applied = True
        else:
            dx = sum(t[0] for t in translations) / len(translations)
            dy = sum(t[1] for t in translations) / len(translations)
            dz = sum(t[2] for t in translations) / len(translations)
            T = (dx, dy, dz)
            for comp in upper.components.values():
                if comp.kind in ("tower_node", "tower_bar"):
                    _translate_component(comp, dx, dy, dz)
                    if comp.kind == "tower_node":
                        comp.properties["assembly_aligned_to"] = "module_boundary"
                        comp.properties["solve_status"] = "assembly_aligned"
            applied = True

    # 对齐后拼缝公差：重新计算配对点残差
    max_gap = 0.0
    mean_gap = 0.0
    for p in pairs:
        lc = lower.components.get(p["lower_node"])
        uc = upper.components.get(p["upper_node"])
        if not lc or not uc:
            continue
        lxyz = _node_xyz(lc)
        uxyz = _node_xyz(uc)
        if not lxyz or not uxyz:
            continue
        gap = math.dist(lxyz, uxyz)
        max_gap = max(max_gap, gap)
        mean_gap += gap
    if pairs:
        mean_gap /= len(pairs)

    return {
        "lower_module": lower.name,
        "upper_module": upper.name,
        "pairs": pairs,
        "matched": len(pairs),
        "within_tol_matched": sum(1 for p in pairs if p["within_tol"]),
        "tol_mm": tol_mm,
        "rigid_translation_applied": applied,
        "rigid": rigid,
        "R": R,
        "T": T,
        "max_gap_mm": round(max_gap, 3),
        "mean_gap_mm": round(mean_gap, 3),
        "closed": bool(pairs) and max_gap <= tol_mm,
    }


def assemble_modules(
    models: List[EngineeringModel],
    *,
    tol_mm: float = 5.0,
    rigid: bool = False,
) -> Tuple[EngineeringModel, List[Dict[str, Any]]]:
    """按顺序拼接多个模块模型，返回合并模型 + 每对拼接报告。

    rigid=True 时使用空间刚体变换 [R|T]（Kabsch）对齐上下模块接口。
    """
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
        reports.append(align_boundary_pair(
            prefixed[i], prefixed[i + 1], tol_mm=tol_mm, rigid=rigid,
        ))

    df = merged.components.setdefault("drawing_file", Component(
        id="drawing_file", name="装配模型", kind="drawing_file",
        properties={"view_mode": "multi_module_assembly"},
    ))
    df.properties["assembly_reports"] = reports
    df.properties["module_count"] = len(models)
    return merged, reports
