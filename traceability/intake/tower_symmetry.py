"""单立面 → 四面封闭空间网架展开（Phase 2）。

从 tower_views.py 拆出的四向镜像对称展开职责（P1 模块拆分）：
把模型里的单立面杆件（front/elevation）展开为四面封闭空间网架，原地改写
EngineeringModel（旧 tower_node/tower_bar 替换为 4 面构件，保留
drawing_file / BOM / 节点板等上下文）。

依赖 ..solve.tower_geometry 的展开算法；不反向依赖 tower_views。
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel, SourceRef, SourceType


def _tower_nodes(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_node":
            yield cid, comp


def _tower_bars(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_bar":
            yield cid, comp


def expand_4_face_symmetry_model(
    model: EngineeringModel,
    overlay: Optional[str | Path | dict] = None,
    *,
    snap_tol: Optional[float] = None,
    weld_corner_legs: bool = True,
    add_diaphragms: bool = True,
) -> EngineeringModel:
    """Phase 2：把模型里的单立面杆件展开为四面封闭空间网架（原地改写）。

    流程：
        1. 取主视图（front/elevation）杆件与其已解算节点，构造单立面平面
           (t=x, z) 节点图（忽略当前可能错误的 y 合成值）；
        2. Phase 1：snap_diagonals_to_legs 吸附斜材端点到主腿工作线；
        3. Phase 2：expand_4_face_symmetry 四向镜像 + 四角主腿熔合 + 横隔面；
        4. 写回 EngineeringModel（旧 tower_node/tower_bar 替换为 4 面构件，
           保留 drawing_file / BOM / 节点板等上下文）。

    返回原 model（原地修改）。overlay 可覆盖 snap_tol 与开关。
    """
    from ..solve.tower_geometry import (
        NodeMap,
        snap_diagonals_to_legs,
        expand_4_face_symmetry,
        classify_members,
        inspect_model_topology,
    )

    spec = {}
    if overlay is not None:
        try:
            from .tower_spec import load_tower_spec
            spec = load_tower_spec(overlay)
        except Exception:
            spec = {}
    if snap_tol is None:
        snap_tol = float(spec.get("snap_tol_mm", 80.0))
    # GT 权威半宽（阶段 0.2 GT 隔离）：仅当 overlay 显式 use_gt_half_width=true
    # （debug/eval）时才从 debug.gt_profile 注入 GT 剖面；生产默认不注入，
    # 四面展开退回「信任立面图 x」的纯几何路径。注入 GT 半宽时产物必须打
    # gt_aligned 标记，正式评测检测到后拒绝评测。
    half_width_fn = None
    crossarm_half_width_fn = None
    if spec.get("use_gt_half_width"):
        from ..debug.gt_profile import gt_tower_half_width, gt_crossarm_half_width
        half_width_fn = gt_tower_half_width
        crossarm_half_width_fn = gt_crossarm_half_width

    bars = [c for _, c in _tower_bars(model)]
    if not bars:
        return model
    counts: Dict[str, int] = defaultdict(int)
    for c in bars:
        counts[c.properties.get("view_type") or "_all"] += 1
    primary = "front" if counts.get("front") else "elevation" if counts.get("elevation") else (
        max(counts, key=lambda k: counts[k])
    )

    # 单立面节点：t=x、z=z（y 归零，避免 synthetic side 的对角线污染）
    src_nodes: NodeMap = {}
    node_meta: Dict[str, Tuple[str, Component]] = {}
    for cid, comp in _tower_nodes(model):
        p = comp.properties
        if p.get("view_type") != primary:
            continue
        if p.get("x") is None or p.get("z") is None:
            continue
        x, z = float(p["x"]), float(p["z"])
        src_nodes[cid] = (x, 0.0, z)
        node_meta[cid] = (primary, comp)

    src_bars: List[dict] = []
    bar_meta: Dict[str, Component] = {}
    for cid, comp in _tower_bars(model):
        p = comp.properties
        if p.get("view_type") != primary:
            continue
        f, t = p.get("from_node"), p.get("to_node")
        if f not in src_nodes or t not in src_nodes:
            continue
        if f == t:
            continue
        # 证据链：把原始组件的来源元数据随 bar dict 传入展开算法，
        # 让每个生成杆件能追溯回原始 sheet / view / 二维构件。
        src_bars.append({
            "id": cid,
            "from": f,
            "to": t,
            "bar_id": p.get("bar_id"),
            "section": p.get("section"),
            "layer": p.get("layer"),
            # 证据链字段（展开算法 `nb = dict(b)` 会逐面复制）
            "derived_from": cid,
            "drawing_view": p.get("drawing_view"),
            "source_file": p.get("source_file"),
            "geometry_origin": p.get("geometry_origin"),
            "projection_refs": list(p.get("projection_refs") or []),
            # 源组件的 SourceRef（若有），用于重建时保留原始来源
            "_source_ref": comp.source,
        })
        bar_meta[cid] = comp

    if not src_bars:
        return model

    work_nodes, work_bars = src_nodes, src_bars

    # 可选：T 形交点打断，把「端点落在其它杆件线段上」的 2D 线段闭合为共享节点。
    if bool(spec.get("close_face_intersections")):
        from ..solve.tower_geometry import close_face_intersections
        work_nodes, work_bars = close_face_intersections(
            work_nodes, work_bars,
            snap_tol=float(spec.get("intersection_snap_tol_mm", 30.0)),
        )

    # Phase 1（可选）：斜材端点吸附到主腿工作线。
    # 系统重构：默认不启用 snap_diagonals_to_legs，因为它会把原本共享的
    # 节点重新吸附到新坐标，破坏已连通的杆件网络（实测 15 杆 1 组件 →
    # 48 杆 3 组件）。仅当 overlay 显式启用 snap_diagonals 时才执行。
    if bool(spec.get("snap_diagonals")):
        snapped_nodes, snapped_bars = snap_diagonals_to_legs(
            work_nodes, work_bars, snap_tol=snap_tol,
        )
    else:
        snapped_nodes, snapped_bars = work_nodes, work_bars

    # Phase 2：四面镜像展开 + 四角主腿熔合 + 横隔面
    face_nodes, face_bars = expand_4_face_symmetry(
        snapped_nodes, snapped_bars,
        weld_corner_legs=weld_corner_legs,
        add_diaphragms=add_diaphragms,
        half_width_fn=half_width_fn,
        crossarm_half_width_fn=crossarm_half_width_fn,
    )
    topology = inspect_model_topology(face_nodes, face_bars, half_width_fn=half_width_fn)
    roles = classify_members(face_nodes, face_bars)

    # 重建模型组件
    _KEEP_KINDS = frozenset({
        "drawing_file", "bom_row", "gusset_plate", "bolt_group", "detail_view",
    })
    keep_components: Dict[str, Component] = {}
    for cid, comp in model.components.items():
        if comp.kind in _KEEP_KINDS:
            keep_components[cid] = comp

    bar_id_count: Dict[str, int] = defaultdict(int)
    src_ref = model.components.get("drawing_file")
    src_ref = src_ref.source if src_ref is not None else None
    # 节点溯源：展开后的新节点 ID -> 原始节点 ID（solve 层未保留时回退为空）。
    # 我们通过「坐标回查 src_nodes」重建原始节点身份，用于保留原始节点 ID。
    coord_to_orig: Dict[Tuple[float, float, float], str] = {}
    for onid, (ox, oy, oz) in src_nodes.items():
        coord_to_orig[(round(ox, 3), round(oy, 3), round(oz, 3))] = onid

    node_orig: Dict[str, str] = {}
    for nid, pos in face_nodes.items():
        key = (round(pos[0], 3), round(pos[1], 3), round(pos[2], 3))
        node_orig[nid] = coord_to_orig.get(key, nid)

    for nid, pos in face_nodes.items():
        orig_nid = node_orig.get(nid, nid)
        node_props = {
            "x": round(pos[0], 4), "y": round(pos[1], 4), "z": round(pos[2], 4),
            "solve_status": "solved",
            "generated_4face": True,
            "original_node_id": orig_nid,
            "geometry_origin": "derived_4face",
        }
        # 阶段 0.2 GT 隔离：GT 半宽注入时，产物打 gt_aligned 标记（评测拒绝）。
        if half_width_fn is not None:
            node_props["gt_aligned"] = True
        keep_components[f"4f_{nid}"] = Component(
            id=f"4f_{nid}", name=orig_nid, kind="tower_node",
            source=src_ref,
            properties=node_props,
        )

    new_bar_ids: List[str] = []
    for b in face_bars:
        bid = str(b.get("bar_id") or b["id"])
        n = bar_id_count[bid]
        bar_id_count[bid] = n + 1
        comp_id = f"4f_{b['id']}"

        # 证据链：优先保留 solve 层透传的来源元数据；否则回退到原始组件 meta。
        derived_from = b.get("derived_from") or b.get("id")
        orig_comp = bar_meta.get(derived_from)
        drawing_view = b.get("drawing_view") or (orig_comp.properties.get("drawing_view") if orig_comp else None)
        source_file = b.get("source_file") or (orig_comp.properties.get("source_file") if orig_comp else None)
        geometry_origin = b.get("geometry_origin") or (orig_comp.properties.get("geometry_origin") if orig_comp else None) or "derived_4face"
        projection_refs = list(b.get("projection_refs") or (orig_comp.properties.get("projection_refs") if orig_comp else []) or [])

        is_diaphragm = bool(b.get("diaphragm"))
        face = b.get("face")
        generated_face = face.upper() if face and face not in ("diaphragm", "center", "corner") else face

        # 来源引用 + 语义冻结（阶段0）：
        #   recognized   —— primary 面（front）杆件，直接从 DXF 识别，进 physical P/R
        #   mirrored     —— 镜像派生面（b/l/r），几何派生但继承原组件 SourceRef
        #   derived      —— corner_leg / diaphragm / center 轴，纯展示几何，不进 P/R
        if is_diaphragm or b.get("corner_leg") or face in ("diaphragm", "center", "corner"):
            bar_source = SourceRef(source_type=SourceType.DERIVED, reference=str(source_file or ""), confidence=1.0)
            geometry_origin = "derived_4face"
            evidence_status = "derived"
        elif face and str(face).lower() == "f":
            # front（primary）面：识别原貌，非镜像派生。
            bar_source = orig_comp.source if (orig_comp is not None and orig_comp.source is not None) else (b.get("_source_ref") or src_ref)
            evidence_status = "recognized"
            # 保持原始 geometry_origin（dxf_geom 等），不覆盖为 derived_4face
        elif face and str(face).lower() in ("b", "l", "r"):
            # 镜像派生面：语义上就是 mirrored，不受 source 有无影响。
            bar_source = orig_comp.source if (orig_comp is not None and orig_comp.source is not None) else (b.get("_source_ref") or src_ref)
            evidence_status = "mirrored"
        elif orig_comp is not None and orig_comp.source is not None:
            bar_source = orig_comp.source
            evidence_status = "mirrored"
        elif b.get("_source_ref") is not None:
            bar_source = b["_source_ref"]
            evidence_status = "mirrored"
        else:
            bar_source = src_ref
            evidence_status = "derived"

        bar_props = {
            "bar_id": bid,
            "from_node": f"4f_{b['from']}",
            "to_node": f"4f_{b['to']}",
            "section": b.get("section"),
            "layer": b.get("layer"),
            "face": face,
            "generated_face": generated_face,
            "role": b.get("role") or roles.get(b["id"], "DIAG"),
            "corner_leg": bool(b.get("corner_leg")),
            "diaphragm": is_diaphragm,
            "generated_4face": True,
            "solve_status": "solved",
            # 证据链
            "derived_from": derived_from,
            "drawing_view": drawing_view,
            "source_file": source_file,
            "geometry_origin": geometry_origin,
            "projection_refs": projection_refs,
            "evidence_status": evidence_status,
            "length_mm_3d": round(
                math.sqrt(sum((face_nodes[b["to"]][i] - face_nodes[b["from"]][i]) ** 2 for i in range(3))), 2,
            ),
        }
        # 阶段 0.2 GT 隔离：GT 半宽注入时，产物打 gt_aligned 标记（评测拒绝）。
        if half_width_fn is not None:
            bar_props["gt_aligned"] = True
        keep_components[comp_id] = Component(
            id=comp_id, name=b["id"], kind="tower_bar",
            source=bar_source,
            properties=bar_props,
        )
        new_bar_ids.append(comp_id)

    model.components = keep_components
    model.staleness = {cid: st for cid, st in model.staleness.items() if cid in model.components}
    model.dependencies = {}

    df = model.components.get("drawing_file")
    if df is not None:
        df.properties.update({
            "expanded_4_face": True,
            "face_count": 4,
            "corner_legs": len({b["corner_index"] for b in face_bars if b.get("corner_leg") and b.get("corner_index")}),
            "diaphragm_count": sum(1 for b in face_bars if b.get("diaphragm")),
            "topology_degree1": topology["dangling_degree1"],
            "topology_crossarm_tips": topology.get("crossarm_tip_count", 0),
            "topology_genuine_dangling": topology.get("genuine_dangling_degree1", topology["dangling_degree1"]),
            "topology_components": topology["components"],
        })
    return model
