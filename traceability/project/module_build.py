"""模块 3D 构建与 master BOM 辅助（M8 / Gap 1）。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel
from ..intake.tower_spec import load_tower_spec
from .assembly import assemble_modules


def physical_bar_counts(model: EngineeringModel, *, labeled_only: bool = True) -> Dict[str, int]:
    """合并模型中各 bar_id 物理根数（tower_bar 计数）。"""
    counts: Counter = Counter()
    for comp in model.components.values():
        if comp.kind != "tower_bar":
            continue
        bid = str(comp.properties.get("bar_id") or "")
        if not bid or bid == "None":
            continue
        if labeled_only and bid.startswith("UNLABELED"):
            continue
        counts[bid] += 1
    return dict(counts)


def resolve_master_bom_path(
    input_dir: str | Path,
    layer_map_path: Optional[str | Path] = None,
    bom_path: Optional[str | Path] = None,
) -> Optional[Path]:
    """解析 master BOM：CLI 参数 > overlay.master_bom > 目录内 *bom*.csv。"""
    if bom_path:
        p = Path(bom_path)
        return p if p.exists() else None
    input_dir = Path(input_dir)
    ov = load_tower_spec(layer_map_path) if layer_map_path else {}
    rel = ov.get("master_bom")
    if rel:
        candidates = [
            input_dir / rel,
            Path(rel),
        ]
        if layer_map_path:
            candidates.insert(0, Path(layer_map_path).parent / rel)
        for p in candidates:
            if p.exists():
                return p
    for pattern in ("*bom*.csv", "*BOM*.csv"):
        found = sorted(input_dir.glob(pattern))
        if found:
            return found[0]
    return None


def _node_z(comp: Component) -> Optional[float]:
    z = comp.properties.get("z")
    if z is None:
        z = comp.properties.get("view_y")
    return float(z) if z is not None else None


def split_merged_by_z(
    model: EngineeringModel,
    *,
    ratio: float = 0.5,
    lower_id: str = "M1",
    upper_id: str = "M2",
    interface_tol_mm: float = 50.0,
) -> Tuple[EngineeringModel, EngineeringModel]:
    """按 z 分位将 cross_file 合并模型拆成上下两模块（装配演示）。

    界面节点（|z - z_split| <= tol）同时进入上下模块，供边界对齐。
    """
    zs: List[float] = []
    for comp in model.components.values():
        if comp.kind != "tower_node":
            continue
        z = _node_z(comp)
        if z is not None:
            zs.append(z)
    if not zs:
        raise ValueError("split_merged_by_z 需要带 z 的 tower_node")
    zs_sorted = sorted(zs)
    idx = max(0, min(len(zs_sorted) - 1, int(len(zs_sorted) * ratio)))
    z_split = zs_sorted[idx]

    def _node_role(comp: Component) -> Optional[str]:
        z = _node_z(comp)
        if z is None:
            return None
        if abs(z - z_split) <= interface_tol_mm:
            return "interface"
        return "upper" if z > z_split else "lower"

    def _in_partition(comp: Component, upper: bool) -> bool:
        if comp.kind == "tower_node":
            role = _node_role(comp)
            if role == "interface":
                return True
            if role is None:
                return False
            return role == "upper" if upper else role == "lower"
        if comp.kind == "tower_bar":
            fn = comp.properties.get("from_node")
            tn = comp.properties.get("to_node")
            fc = model.components.get(fn) if fn else None
            tc = model.components.get(tn) if tn else None
            if fc is None or tc is None:
                return False
            return _in_partition(fc, upper) and _in_partition(tc, upper)
        return False

    def _extract(module_id: str, upper: bool) -> EngineeringModel:
        sub = EngineeringModel(name=f"module-{module_id}")
        for cid, comp in model.components.items():
            if not _in_partition(comp, upper):
                continue
            props = dict(comp.properties)
            props["module_id"] = module_id
            props["solve_status"] = props.get("solve_status") or "solved"
            sub.add_component(Component(
                id=cid, name=comp.name, kind=comp.kind,
                source=comp.source, properties=props, tags=list(comp.tags),
            ))
        df = sub.components.setdefault("drawing_file", Component(
            id="drawing_file", name=module_id, kind="drawing_file",
            properties={"view_mode": "module_slice", "module_id": module_id, "z_split": z_split},
        ))
        df.properties["z_split"] = z_split
        df.properties["interface_tol_mm"] = interface_tol_mm
        return sub

    return _extract(lower_id, False), _extract(upper_id, True)


def try_assembly_from_merged(
    merged_model: EngineeringModel,
    overlay: Optional[str | Path | dict],
) -> Optional[Dict[str, Any]]:
    """cross_file 合并模型就绪后尝试模块装配（z 拆分 demo 或 overlay 开关）。"""
    ov = load_tower_spec(overlay) if overlay else {}
    if not ov.get("enable_module_assembly"):
        return None

    ratio = ov.get("assembly_demo_z_split")
    if ratio is None:
        return None

    ratio_f = float(ratio)
    lower, upper = split_merged_by_z(merged_model, ratio=ratio_f)
    lower_nodes = sum(1 for c in lower.components.values() if c.kind == "tower_node")
    upper_nodes = sum(1 for c in upper.components.values() if c.kind == "tower_node")
    if lower_nodes == 0 or upper_nodes == 0:
        return None

    tol = float(ov.get("assembly_tol_mm") or 10.0)
    asm_model, reports = assemble_modules([lower, upper], tol_mm=tol)
    return {
        "model": asm_model,
        "reports": reports,
        "module_ids": ["M1", "M2"],
        "mode": "assembly_demo_z_split",
        "z_split_ratio": ratio_f,
    }
