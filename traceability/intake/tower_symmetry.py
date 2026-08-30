"""单立面 → 四面封闭空间网架展开（Phase 2）。

从 tower_views.py 拆出的四向镜像对称展开职责（P1 模块拆分）：
把模型里的单立面杆件（front/elevation）展开为四面封闭空间网架，原地改写
EngineeringModel（旧 tower_node/tower_bar 替换为 4 面构件，保留
drawing_file / BOM / 节点板等上下文）。

依赖 ..solve.tower_geometry 的展开算法；不反向依赖 tower_views。
"""

from __future__ import annotations

import copy
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
    # （debug/eval）时才从 debug.gt_profile 注入 GT 剖面；生产默认不注入。
    # 生产路径改为从立面主腿证据拟合 half_width(z)（阶段3.2），严禁 abs(t)
    # 冒充塔身半宽。注入 GT 半宽时产物必须打 gt_aligned 标记，正式评测拒绝。
    half_width_fn = None
    crossarm_half_width_fn = None
    half_width_fitted = False
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
    # 多段大模型按 Z 跨度分段处理，避免全局 O(N^2) 耗时过长。
    if bool(spec.get("close_face_intersections")):
        from ..solve.tower_geometry import close_face_intersections
        snap_inter_tol = float(spec.get("intersection_snap_tol_mm", 50.0))
        # 收集 Z 范围做分段打断
        zs = [pos[2] for pos in work_nodes.values()]
        if len(work_bars) > 300 and zs and max(zs) - min(zs) > 6000.0:
            # 6 段塔身分块打断
            z_bins = [
                (0.0, 5500.0), (5500.0, 11000.0), (11000.0, 16000.0),
                (16000.0, 23000.0), (23000.0, 30000.0), (30000.0, 40000.0)
            ]
            merged_split_nodes: NodeMap = {}
            merged_split_bars: List[dict] = []
            for z_lo, z_hi in z_bins:
                sub_b = [b for b in work_bars if z_lo <= (work_nodes[b["from"]][2] + work_nodes[b["to"]][2]) / 2.0 <= z_hi]
                if not sub_b:
                    continue
                sub_n = {nid: work_nodes[nid] for b in sub_b for nid in (b["from"], b["to"])}
                nn, nb = close_face_intersections(sub_n, sub_b, snap_tol=snap_inter_tol, max_rounds=2)
                merged_split_nodes.update(nn)
                merged_split_bars.extend(nb)
            # 补充跨段杆件（如果有）
            handled_ids = {b["id"] for b in merged_split_bars}
            for b in work_bars:
                if b["id"] not in handled_ids:
                    merged_split_bars.append(b)
                    merged_split_nodes[b["from"]] = work_nodes[b["from"]]
                    merged_split_nodes[b["to"]] = work_nodes[b["to"]]
            work_nodes, work_bars = merged_split_nodes, merged_split_bars
        else:
            work_nodes, work_bars = close_face_intersections(
                work_nodes, work_bars,
                snap_tol=snap_inter_tol,
                max_rounds=3,
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
    # 阶段 5.3：多段立面拼接边界自动缝合（消除段间重叠横杆与重复节点）。
    if bool(spec.get("stitch_boundaries", True)):
        from ..solve.tower_geometry import stitch_segment_boundaries
        stitch_tol = float(spec.get("boundary_stitch_tol_mm", 80.0))
        snapped_nodes, snapped_bars, _stitch_rep = stitch_segment_boundaries(
            snapped_nodes, snapped_bars, boundary_tol_mm=stitch_tol,
        )

    # 阶段3.2：生产路径（非 GT）从立面主腿证据拟合 half_width(z)，替代 abs(t)。
    # 拟合失败时 half_width_fn 保持 None（仍走旧 abs(t) 路径，但打 review_required
    # 标记，不假装闭合）。
    if half_width_fn is None:
        from ..solve.tower_geometry import fit_tower_half_width_from_face
        fitted = fit_tower_half_width_from_face(snapped_nodes, snapped_bars)
        if fitted is not None:
            half_width_fn = fitted
            half_width_fitted = True

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
        # 阶段 0.2 GT 隔离：仅「GT 半宽注入」（use_gt_half_width）才打 gt_aligned。
        # 生产路径的 fit 拟合半宽不是 GT，严禁误标（否则正式评测会误拒）。
        if spec.get("use_gt_half_width"):
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
        # 阶段 4.5：深拷贝 projection_refs，避免多根展开杆件共享同一 dict（改一根
        # 会污染其它杆件）。list() 只是浅拷贝，元素 dict 仍被共享。
        projection_refs = copy.deepcopy(
            b.get("projection_refs") or (orig_comp.properties.get("projection_refs") if orig_comp else []) or []
        )

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

        # geometry_class（阶段 2 语义冻结）：
        #   derived      —— corner_leg / diaphragm / center 轴（纯展示几何）
        #   reconstructed—— 对称展开重建产物（mirrored b/l/r 面，含闭合补全）
        #   recognized   —— primary（front）面识别原貌（非派生）
        if evidence_status == "derived":
            geometry_class = "derived"
        elif evidence_status == "mirrored":
            geometry_class = "reconstructed"
        else:
            geometry_class = "recognized"

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
            "geometry_class": geometry_class,
            "projection_refs": projection_refs,
            "evidence_status": evidence_status,
            "length_mm_3d": round(
                math.sqrt(sum((face_nodes[b["to"]][i] - face_nodes[b["from"]][i]) ** 2 for i in range(3))), 2,
            ),
        }
        # 阶段 0.2 GT 隔离：仅「GT 半宽注入」（use_gt_half_width）才打 gt_aligned。
        if spec.get("use_gt_half_width"):
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

    # 阶段 4.3：证据链悬空引用修复。mirrored（b/l/r）杆件的 derived_from
    # 原样复制自原始二维构件 ID（展开后被删除，导致悬空）。这里把 mirrored
    # 杆件的 derived_from 重写为「front 面对应物理杆件」的组件 ID（展开后存在，
    # 可解析），形成 mirrored → front 物理杆件 → 原始 DXF 构件的完整追溯链。
    # front（recognized）杆件的 derived_from 保持指向原始二维构件（外部来源，
    # 不要求组件内可解析）。
    _front_by_stem: Dict[str, str] = {}
    for cid, comp in keep_components.items():
        if comp.kind == "tower_bar" and comp.properties.get("face") == "f":
            # comp_id 形如 4f_<stem>_F，stem 是去掉 _F 后缀的部分
            _front_by_stem[cid] = cid
    # 建立 stem -> front 组件 ID 映射（stem = 去掉尾缀 _F 后的公共前缀）
    _stem_to_front: Dict[str, str] = {}
    for cid in _front_by_stem:
        stem = cid[:-2] if cid.endswith("_F") else cid
        _stem_to_front.setdefault(stem, cid)

    for cid, comp in keep_components.items():
        if comp.kind != "tower_bar":
            continue
        p = comp.properties
        if p.get("geometry_class") != "reconstructed":
            continue
        stem = cid[:-2] if (cid.endswith("_B") or cid.endswith("_L") or cid.endswith("_R")) else None
        if stem is None:
            continue
        front_cid = _stem_to_front.get(stem)
        if front_cid and front_cid != cid:
            p["derived_from"] = front_cid

    # 阶段 4.6：rules/dimensions 的 applies_to 重指（M0 门槛「悬空引用为 0」）。
    # 四面展开把组件从 <old_id> 重建为 4f_<old_id>_{F/B/L/R}，但 rules 与
    # dimensions 的 applies_to 仍指向展开前的旧 ID，全部悬空。这里把：
    #   * bar 引用（old_bar_id）→ front 面物理杆件（4f_<old_bar_id>_F，识别源头）
    #   * node 引用（old_node_id）→ 该 node 对应的展开后节点
    # 使 rules/dimensions 在展开后保持引用完整，不产生悬空 applies_to。
    _retarget_applies_to(model, _stem_to_front, set(src_nodes), face_nodes)

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
            # 阶段3.2：生产路径半宽来源标记（fit=立面主腿拟合，gt=GT注入，none=退化）
            "half_width_source": ("gt" if spec.get("use_gt_half_width")
                                  else "fit" if half_width_fitted else "none"),
            "half_width_degraded": (not spec.get("use_gt_half_width") and not half_width_fitted),
        })
    return model


def _retarget_applies_to(
    model: EngineeringModel,
    stem_to_front: Dict[str, str],
    old_node_ids: set,
    face_nodes: Dict[str, Tuple[float, float, float]],
) -> None:
    """阶段 4.6：四面展开后重指 rules/dimensions 的 applies_to，消除悬空引用。

    bar 引用：old_bar_id（如 ``35A1-JC1-02__bar_108_front``）→ front 面物理杆件
    （``4f_35A1-JC1-02__bar_108_front_F``，识别源头，进 physical P/R）。

    node 引用：node 规则（r_node_fully_solved / r_cross_file_3d_partial 等）的
    applies_to 是「所有节点」，展开后重指为「所有展开后的 tower_node 组件」
    （四向镜像使节点数从 N 增到约 4N，逐节点坐标回查不可靠，语义上应检查全部）。
    """
    # old_bar_id -> front 面新组件 ID。stem_to_front 的 key 是「4f_<old_id>」
    # （去掉 _F 后缀），value 是 front 组件 ID（4f_<old_id>_F）。
    bar_map: Dict[str, str] = {
        stem[3:]: cid for stem, cid in stem_to_front.items()
        if stem.startswith("4f_")
    }
    all_node_ids = [
        cid for cid, comp in model.components.items()
        if comp.kind == "tower_node"
    ]

    # dimensions：applies_to 为单值，多为 bar 引用。
    for dim in model.dimensions.values():
        if dim.applies_to and dim.applies_to in bar_map:
            dim.applies_to = bar_map[dim.applies_to]

    # rules：applies_to 为列表，逐条重指 bar 引用；node 引用整体替换为全部新节点。
    for rule in model.rules.values():
        new_targets: List[str] = []
        has_node_ref = False
        for cid in rule.applies_to:
            if cid in bar_map:
                new_targets.append(bar_map[cid])
            elif cid in old_node_ids:
                # 旧 node ID —— 展开后节点被重建为 4f_Nxxxxx，无法可靠逐点映射，
                # 整体重指为所有展开后节点（node 规则语义是「检查所有节点」）。
                has_node_ref = True
            elif cid in model.components or cid in model.connections:
                # 已是展开后的有效引用（或连接），保留。
                new_targets.append(cid)
        if has_node_ref:
            # 语义：node 规则检查「所有节点」，展开后覆盖全部新节点。
            new_targets = new_targets + all_node_ids
        if new_targets:
            rule.applies_to = new_targets
