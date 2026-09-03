"""模块 3D 构建与 master BOM 辅助（M8 / Gap 1）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, Dimension, EngineeringModel
from ..intake.tower_spec import load_tower_spec
from .assembly import assemble_modules


def _root_stem(cid: str) -> str:
    """组件 id → 物理杆根 stem（去四面镜像 + split 细分链）。

    * 四面展开实例 ``4f_<stem>_F/_B/_L/_R``：剥面后缀，F/B/L/R 共享一杆；
    * split/panel 细分链 ``<stem>__splitN[__splitM...]``：剥 __split 链，
      同一识别线的所有细分段合并回一根（BOM 数的是整件，不是段）；
    * sidegen 侧读注入对 ``sidegen__bNNNN_l/_r``（P5 2026-09-03）：小写
      l/r 是同一物理杆的直读 + 镜像孪生——此前只有大写四面后缀被剥，
      孪生被计成 2 根物理杆，bar 122/140 数量 2>1 假冲突直接引爆
      r_project_bom_master。
    其余后缀（_front_56 等母杆序号）保留——不同识别线是不同物理杆。
    """
    s = cid[3:] if cid.startswith("4f_") else cid
    for suf in ("_F", "_B", "_L", "_R"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    if s.startswith("sidegen__") and s.rsplit("_", 1)[-1] in ("l", "r"):
        s = s.rsplit("_", 1)[0]
    while "__split" in s:
        s = s[: s.index("__split")]
    return s


def physical_bar_counts(model: EngineeringModel, *, labeled_only: bool = True) -> Dict[str, int]:
    """合并模型中各 bar_id 物理根数（tower_bar 计数）。

    阶段 9：用 is_physical_bar 的语义过滤（fail-closed），只统计物理杆件
    （recognized + reconstructed），排除 derived（corner_leg/diaphragm/center）
    与 canonical/unknown，避免 BOM 数量因派生展示几何而虚高。

    V1（2026-09-02）：按 root stem 计数——同一物理杆的四面镜像（F/B/L/R）
    与 split/panel 细分段只计 1 根。此前逐实例计数把 112 计成 30、402 计成
    16，全是四面×细分的乘法伪影，不是真实数量差。
    """
    from ..eval.metrics import is_physical_bar
    stems: Dict[str, Dict[str, set]] = {}  # bar_id -> {root_stem: faces}
    for cid, comp in model.components.items():
        if comp.kind != "tower_bar":
            continue
        props = comp.properties or {}
        # 阶段 9：物理杆件语义过滤（derived/canonical/unknown 不计入）
        if not is_physical_bar(props):
            continue
        bid = str(props.get("bar_id") or "")
        if not bid or bid == "None":
            continue
        if labeled_only and bid.startswith("UNLABELED"):
            continue
        stems.setdefault(bid, {}).setdefault(_root_stem(cid), set()).add(
            str(props.get("face") or ""))
    # 每 bar_id 取 root stem 数为物理根数；同 stem 内 front 为识别源头（记录用）
    return {bid: len(stem_map) for bid, stem_map in stems.items()}


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


# --------------------------------------------------------------------------- #
# Phase 3  M1–M6 长链条多模块装配定义
# --------------------------------------------------------------------------- #

MODULE_DEFINITIONS: List[Dict[str, Any]] = [
    {"id": "M1_LEG",        "name": "基础与塔脚段",   "z_range": (0.0, 9000.0)},
    {"id": "M2_LOWER_BODY", "name": "下塔身段",       "z_range": (9000.0, 18000.0)},
    {"id": "M3_MID_BODY",   "name": "中塔身段",       "z_range": (18000.0, 24000.0)},
    {"id": "M4_UPPER_BODY", "name": "上塔身段",       "z_range": (24000.0, 30000.0)},
    {"id": "M5_CROSSARM",   "name": "导线曲臂横担段", "z_range": (30000.0, 33500.0)},
    {"id": "M6_HEAD",       "name": "猫耳地线支架段", "z_range": (33500.0, 36600.0)},
]


def _node_z_of(comp: Component) -> Optional[float]:
    z = comp.properties.get("z")
    if z is None:
        z = comp.properties.get("view_y")
    return float(z) if z is not None else None


def split_merged_by_modules(
    model: EngineeringModel,
    definitions: Optional[List[Dict[str, Any]]] = None,
    *,
    interface_tol_mm: float = 500.0,
) -> List[EngineeringModel]:
    """按 M1–M6 标高接口把合并模型切分为 6 个子模块。

    节点 z 落在 [z0 - tol, z1 + tol] 时属于该模块；杆件两端节点均属于某模块
    时该杆件归入该模块。接口节点（靠近分界标高）会同时进入相邻两个模块，
    供 Phase 3 装配时做 top/bottom 配对。
    """
    defs = definitions or MODULE_DEFINITIONS
    # 收集实际存在的节点 z 层；固定 z_range 边界吸附到最近实际 z 层，使相邻模块
    # 共享同一接口层（变截面棱台固定标高往往没有恰好落上的节点）。
    z_levels = sorted({round(float(_node_z_of(c)), 1)
                       for c in model.components.values() if c.kind == "tower_node"
                       and _node_z_of(c) is not None})

    def _snap(z: float) -> float:
        if not z_levels:
            return z
        return min(z_levels, key=lambda zz: abs(zz - z))

    modules: List[EngineeringModel] = []
    for mdef in defs:
        mid = mdef["id"]
        z0 = _snap(float(mdef["z_range"][0]))
        z1 = _snap(float(mdef["z_range"][1]))
        sub = EngineeringModel(name=f"module-{mid}")

        node_membership: Dict[str, bool] = {}
        for cid, comp in model.components.items():
            if comp.kind != "tower_node":
                continue
            z = _node_z_of(comp)
            if z is None:
                continue
            node_membership[cid] = z0 - interface_tol_mm <= z <= z1 + interface_tol_mm

        for cid, comp in model.components.items():
            if comp.kind == "tower_node":
                if node_membership.get(cid):
                    props = dict(comp.properties)
                    props["module_id"] = mid
                    sub.add_component(Component(
                        id=cid, name=comp.name, kind=comp.kind,
                        source=comp.source, properties=props, tags=list(comp.tags),
                    ))
            elif comp.kind == "tower_bar":
                f, t = comp.properties.get("from_node"), comp.properties.get("to_node")
                if f and t and node_membership.get(f) and node_membership.get(t):
                    props = dict(comp.properties)
                    props["module_id"] = mid
                    sub.add_component(Component(
                        id=cid, name=comp.name, kind=comp.kind,
                        source=comp.source, properties=props, tags=list(comp.tags),
                    ))
            elif comp.kind == "drawing_file":
                props = dict(comp.properties)
                props.update({"view_mode": "module_slice", "module_id": mid,
                              "z_range": [z0, z1],
                              "module_z0": z0, "module_z1": z1})
                sub.add_component(Component(
                    id=cid, name=comp.name, kind=comp.kind,
                    source=comp.source, properties=props, tags=list(comp.tags),
                ))

        for did, dim in model.dimensions.items():
            if dim.applies_to and sub.components.get(dim.applies_to):
                sub.add_dimension(Dimension(
                    id=did, name=dim.name, value=dim.value, unit=dim.unit,
                    origin=dim.origin, source=dim.source,
                    applies_to=dim.applies_to, status=dim.status,
                ))
        modules.append(sub)

    return [m for m in modules if sum(1 for c in m.components.values() if c.kind == "tower_node") > 0]


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


def try_assembly_m1_m6_from_merged(
    merged_model: EngineeringModel,
    overlay: Optional[str | Path | dict],
) -> Optional[Dict[str, Any]]:
    """Phase 3：M1–M6 六模块长链条装配（刚体 [R|T] 对齐 + 缝合）。

    overlay 可用键：
        module_definitions: "m1_m6" 或显式 [{id,name,z_range:[z0,z1]}, ...]
        assembly_tol_mm:     拼接公差（默认 5.0）
        assembly_interface_tol_mm: 切分接口容差（默认 500.0）
    """
    ov = load_tower_spec(overlay) if overlay else {}
    if not ov.get("enable_module_assembly"):
        return None

    defs = ov.get("module_definitions")
    if defs is None:
        return None
    if isinstance(defs, str) and defs == "m1_m6":
        definitions = MODULE_DEFINITIONS
    elif isinstance(defs, list) and defs:
        definitions = [
            {
                "id": str(d.get("id") or f"M{i + 1}"),
                "name": str(d.get("name") or f"模块 {i + 1}"),
                "z_range": (float(d["z_range"][0]), float(d["z_range"][1])),
            }
            for i, d in enumerate(defs)
            if isinstance(d, dict) and d.get("z_range")
        ]
    else:
        return None

    tol = float(ov.get("assembly_tol_mm") or 5.0)
    interface_tol = float(ov.get("assembly_interface_tol_mm") or 500.0)
    modules = split_merged_by_modules(
        merged_model, definitions=definitions, interface_tol_mm=interface_tol,
    )
    if len(modules) < 2:
        return None

    asm_model, reports = assemble_modules(modules, tol_mm=tol, rigid=True)
    closed = all(bool(r.get("closed")) for r in reports)
    max_gap = max((float(r.get("max_gap_mm") or 0.0) for r in reports), default=0.0)
    return {
        "model": asm_model,
        "reports": reports,
        "module_ids": [d["id"] for d in definitions if any(
            m.name == f"module-{d['id']}" for m in modules)],
        "mode": "m1_m6_rigid_chain",
        "tol_mm": tol,
        "closed": closed,
        "max_gap_mm": round(max_gap, 3),
    }
