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
            # 阶段4.4：件号证据随展开透传（solve 层 nb=dict(b) 浅拷贝复制）
            "bar_id_evidence": list(p.get("bar_id_evidence") or []),
            # 源组件的 SourceRef（若有），用于重建时保留原始来源
            "_source_ref": comp.source,
        })
        bar_meta[cid] = comp

    # 阶段 5.4：过滤节间短斜材（< min_diag_len_mm 的斜向杆）。
    # 国网节点板连接处的短角钢（154~850mm）在 DXF 里就是独立短段，其端点
    # 画到节点板边缘、未连到主腿中心线，展开后大量 degree=1 悬空（实测
    # 346 个真悬空里 278 个来自 <500mm 短杆）。GT 斜材最短 554mm、不统计
    # 这些节点板连接件，故过滤是「去噪」而非丢信息。用倾角区分：斜材倾角
    # 落在 [min_diag_incl, 90-min_diag_incl]（20°~70°，含反向 110°~160°），
    # 近竖直主腿（>70°）与近水平横隔（<20°）不受影响。
    # 默认关闭（min_diag_len_mm=0）：过滤有损，须在 overlay 显式启用
    # （guowang_35A1 生产 overlay 配 500mm），避免误杀测试/其它调用方的合法短斜材。
    min_diag_len_mm = float(spec.get("min_diag_len_mm", 0.0))
    min_diag_incl = float(spec.get("min_diag_incl_deg", 20.0))
    # S1c/Phase 2 件号登记簿：几何被规则清除的杆（短斜材过滤/残根剪除）
    # 携带的图纸件号收进这里——几何清除噪声，A1 文字识别证据不丢。
    orphan_label_ids: List[str] = []
    if min_diag_len_mm > 0:
        kept_bars: List[dict] = []
        dropped_short_diag = 0
        # Phase 2（2026-08-31）：被过滤短斜材携带的真实件号收进登记簿——
        # 几何按结构规则清除（GT 不统计节点板连接件），但图纸件号文字是
        # A1 证据，不许随几何消亡。与残根剪除的 orphan_label_ids 同语义。
        dropped_diag_labels: List[str] = []
        for b in src_bars:
            f, t = src_nodes.get(b["from"]), src_nodes.get(b["to"])
            if f is None or t is None:
                kept_bars.append(b)
                continue
            dx = t[0] - f[0]
            dz = t[2] - f[2]
            length = math.hypot(dx, dz)
            if length < min_diag_len_mm and length > 1e-9:
                # 倾角 = 与水平面夹角（单立面 z 为高、x 为宽）：竖直=90°、水平=0°。
                incl = abs(math.degrees(math.atan2(abs(dz), abs(dx))))
                if min_diag_incl <= incl <= 90.0 - min_diag_incl:
                    dropped_short_diag += 1
                    _bid = b.get("bar_id")
                    if _bid and not str(_bid).startswith("UNLABELED"):
                        dropped_diag_labels.append(str(_bid))
                    continue
            kept_bars.append(b)
        if dropped_short_diag:
            src_bars = kept_bars
            orphan_label_ids.extend(dropped_diag_labels)

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
            for bi, (z_lo, z_hi) in enumerate(z_bins):
                sub_b = [b for b in work_bars if z_lo <= (work_nodes[b["from"]][2] + work_nodes[b["to"]][2]) / 2.0 <= z_hi]
                if not sub_b:
                    continue
                sub_n = {nid: work_nodes[nid] for b in sub_b for nid in (b["from"], b["to"])}
                nn, nb = close_face_intersections(sub_n, sub_b, snap_tol=snap_inter_tol, max_rounds=2)
                # S1 修复（节点 ID 跨 bin 碰撞）：close_face_intersections 内部
                # _get_or_add_node 用 f"N{len(nodes):04d}" 生成新节点 ID，各 bin 的
                # len(nodes) 不同但可能撞出相同 ID（如两个 bin 都生成 N0041）。
                # 直接 update 会让后 bin 的 N0041 覆盖前 bin 的——杆件端点被
                # 「传送」到别的 z 段（实测 07 段主腿端点 z=6643 被改写到 z=32594，
                # 产生 21-27m 幽灵杆）。修复：合并前把「该 bin 新建的节点」重命名
                # 为全局唯一 ID（_zb{bi} 前缀），并重映射该 bin 杆件端点。
                new_ids = [nid for nid in nn if nid not in sub_n]
                if new_ids:
                    rename = {nid: f"{nid}_zb{bi}" for nid in new_ids}
                    nn = {rename.get(nid, nid): pos for nid, pos in nn.items()}
                    for b in nb:
                        b["from"] = rename.get(b["from"], b["from"])
                        b["to"] = rename.get(b["to"], b["to"])
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
    # S1c 复测（ID 碰撞修复后）：snap 仍是回归（A2 4.4%→4.0%，悬空 220→348，
    # 塔顶越界到 37349>GT 36600）——早期结论确认成立，非碰撞 bug 污染。
    if bool(spec.get("snap_diagonals")):
        snapped_nodes, snapped_bars = snap_diagonals_to_legs(
            work_nodes, work_bars, snap_tol=snap_tol,
        )
    else:
        snapped_nodes, snapped_bars = work_nodes, work_bars

    # 阶段 5.5（S1c）：迭代剪除短悬臂残根（degree=1 端点的 <阈值 短杆）。
    # DXF 标注引线 / 终止线 / T 打断残片是单端接触的短竖杆（85~300mm），
    # 非结构杆（GT 不统计），却把门禁 genuine_dangling 推高。长杆（≥阈值）
    # 即使悬空也保留——真实断裂需 S3/S4 拓扑缝合。
    # 默认关闭（max_stub_len_mm=0）：与 min_diag_len_mm 同纪律，过滤有损，
    # 须在 overlay 显式启用（guowang_35A1 生产 overlay 配 400mm），
    # 避免误杀测试/其它调用方的合法短杆（小型塔的短腿即合法悬臂）。
    # 件号保全：被剪残根若携带真实图纸件号（多为孤立标注残片，无结构附着
    # 点无法转移），收进 orphan_label_ids 登记簿——几何清除噪声，A1 证据不丢。
    max_stub_len = float(spec.get("max_stub_len_mm", 0.0))
    if max_stub_len > 0:
        from ..solve.tower_geometry import prune_short_stub_bars
        snapped_nodes, snapped_bars, stub_rep = prune_short_stub_bars(
            snapped_nodes, snapped_bars, max_stub_len_mm=max_stub_len,
        )
        # Phase 2：extend 而非覆盖——上面短斜材过滤已收集的件号不能被冲掉
        orphan_label_ids.extend(stub_rep.get("pruned_label_ids") or [])

    # Phase 2.3（2026-08-31 review）：受约束的局部端点吸附。
    # 与全局 snap_diagonals_to_legs 的区别：只处理 degree=1 长杆悬空端点，
    # 目标是真实存在的 leg 杆段（非拟合工作线），逐杆审计杆长变化 <=2%，
    # 不移动任何已共享节点。默认关闭（snap_dangling_endpoints=false），
    # 须在 overlay 显式启用——与 max_stub_len_mm 同纪律。
    if bool(spec.get("snap_dangling_endpoints", False)):
        from ..solve.tower_geometry import snap_dangling_endpoints_local
        snapped_nodes, snapped_bars, snap_rep = snap_dangling_endpoints_local(
            snapped_nodes, snapped_bars,
            max_gap_mm=float(spec.get("snap_dangling_max_gap_mm", 300.0)),
        )
        df_snap = model.components.get("drawing_file")
        if df_snap is not None:
            df_snap.properties["dangling_snap_report"] = {
                "snapped": int(snap_rep.get("snapped", 0)),
                "merged": int(snap_rep.get("merged", 0)),
                "rejected": snap_rep.get("rejected", {}),
            }

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
        # S7 锥体重建（2026-08-31）：method="taper" 用 Theil-Sen 稳健回归把半宽
        # 拟合成直线锥体，替代原「分段常数+单调包络」。默认关闭保持旧行为，
        # 须 overlay 显式启用——与 snap_dangling_endpoints 同纪律。
        taper = bool(spec.get("half_width_taper", False))
        fitted = fit_tower_half_width_from_face(
            snapped_nodes, snapped_bars,
            method="taper" if taper else "monotone",
            taper_max_residual_mm=float(
                spec.get("half_width_taper_max_residual_mm", 150.0)),
        )
        if fitted is not None:
            half_width_fn = fitted
            half_width_fitted = True

        # S7 生产横担层检测（2026-08-31）：从 DXF 证据找塔头横担外伸层，
        # 替代「传 None 导致横担节点被吸附到塔身锥线」的旧行为。仅在
        # 生产路径（未注入 GT 横担）且显式开启时启用。层位来自图纸证据
        # 本身（宽节点 z 链聚类），不依赖 GT 层表。
        if (
            crossarm_half_width_fn is None
            and half_width_fn is not None
            and bool(spec.get("detect_crossarm_layers", False))
        ):
            from ..solve.tower_geometry import detect_crossarm_layers_from_face
            _arm_fn, _arm_rep = detect_crossarm_layers_from_face(
                snapped_nodes, snapped_bars, half_width_fn)
            if _arm_fn is not None:
                crossarm_half_width_fn = _arm_fn
                _df = model.components.get("drawing_file")
                if _df is not None:
                    _df.properties["crossarm_layer_detection"] = {
                        "n_layers": len(_arm_rep.get("layers", [])),
                        "layers": [
                            {
                                "z_lo": round(float(l["z_lo"]), 1),
                                "z_hi": round(float(l["z_hi"]), 1),
                                "arm_mm": round(float(l["arm_mm"]), 1),
                            }
                            for l in _arm_rep.get("layers", [])
                        ],
                    }

    # S6 主腿节间化 + S2b 横隔层 z 对齐（用户 2026-08 裁定：canonical 平台
    # 标高 z-only 可注入，x/y 严禁注入 GT）。
    #
    # 旗标（2026-08-31 口径审计重构，风险3 拆分）：
    #   panel_level_source: "gt"   — canonical 平台标高（z-only GT 注入，level-assisted 口径）
    #                    | "dxf"   — derive_panel_levels DXF 证据推导（纯 DXF 口径）
    #                    | "off"   — 不启用平台标高（旧 2000mm 粗分桶横隔 + 通长腿）
    #   subdivide_legs: bool — 主腿节间化开关（默认随 levels 启用；显式 false
    #                          可单独隔离「横隔 Z 对齐」收益）
    # 兼容旧旗标：use_gt_platform_levels=true → panel_level_source="gt"；
    #            subdivide_legs=true（无 gt 旗标）→ "dxf"。
    level_source = spec.get("panel_level_source")
    if level_source is None:
        if bool(spec.get("use_gt_platform_levels")):
            level_source = "gt"
        elif bool(spec.get("subdivide_legs")):
            level_source = "dxf"
        else:
            level_source = "off"
    level_source = str(level_source)

    panel_levels: List[float] = []
    if level_source == "gt":
        from ..debug.gt_profile import gt_platform_levels
        panel_levels = list(gt_platform_levels())
    elif level_source == "dxf":
        from ..solve.tower_geometry import derive_panel_levels_detailed
        _manual = spec.get("panel_level_manual_levels") or []
        panel_levels, _pl_records = derive_panel_levels_detailed(
            snapped_nodes, snapped_bars,
            manual_levels=[float(z) for z in _manual] if _manual else None,
        )
        # P4.1 证据链：逐层来源（dxf/manual + manual_snapped）进 drawing_file，
        # delivery 可呈现「每个节间的层位证据」。
        _df_pl = model.components.get("drawing_file")
        if _df_pl is not None and _pl_records:
            _df_pl.properties["panel_level_evidence"] = _pl_records

    if panel_levels:
        subdivide_on = bool(spec.get("subdivide_legs", True))
        if subdivide_on:
            from ..solve.tower_geometry import subdivide_legs_at_levels
            snapped_nodes, snapped_bars, _sub_rep = subdivide_legs_at_levels(
                snapped_nodes, snapped_bars, panel_levels,
                half_width_fn=half_width_fn,
            )

    # Phase 3（P3.2/P3.3）：评分制节间 X 交叉重建。保守参数默认关闭，
    # 须 overlay 显式开启（panel_cross_reconstruct=true）。三层评分：
    #   塔身区限定（z_hi < 横担层 z_lo）+ 图纸斜线证据（>=2 根）
    #   + 腿位锚定。语义：geometry_origin=panel_cross_reconstructed
    #   （B 类 reconstructed，不入 pure 口径）。
    # 实测（35A1-JC1 塔身区）：TP@500 211→217（+6）/ FP +8 / P +0.2 点。
    if bool(spec.get("panel_cross_reconstruct", False)) and panel_levels:
        from ..solve.tower_geometry import reconstruct_panel_cross_diagonals
        # 横担层下界（塔身/塔头分界）：优先用生产检测的横担层，否则
        # 用 fit 半宽函数的外推失效高度（保守：无检测则全塔生成）。
        _crossarm_z_max = None
        _df_props = model.components.get("drawing_file")
        if _df_props is not None:
            _cld = _df_props.properties.get("crossarm_layer_detection") or {}
            _layers = _cld.get("layers") or []
            if _layers:
                _crossarm_z_max = min(float(l["z_lo"]) for l in _layers)
        snapped_nodes, snapped_bars, _xc_rep = reconstruct_panel_cross_diagonals(
            snapped_nodes, snapped_bars, panel_levels,
            crossarm_z_max=_crossarm_z_max,
            level_source_label=(
                "gt_canonical" if level_source == "gt" else "dxf_derived"
            ),
        )
        _df = model.components.get("drawing_file")
        if _df is not None:
            _df.properties["panel_cross_reconstruction"] = {
                "generated": _xc_rep.get("generated", 0),
                "panels": len(_xc_rep.get("panels", [])),
            }

    # P5：底段参数化外推（DXF 无底段图纸，02 图最低节点 z=6643）。
    # 沿生产拟合半宽锥线外推 z ∈ [0, 6500]（主腿节间 + X 交叉），
    # 紫色 derived_parametric——只进 parametric 口径（GT 隔离：半宽用
    # 生产 fit 函数，不读 GT）。overlay 开关 parametric_base_extrapolation。
    _z_top_pb = spec.get("parametric_base_z_top", 6500.0)
    if bool(spec.get("parametric_base_extrapolation", False)) and half_width_fn is not None:
        from ..solve.tower_geometry import (
            extrapolate_base_segment,
            leg_chain_extrapolator,
        )
        # P5.1 锥线延拓：monotone fit 闭包在低 z 夹紧到采样下界（实测
        # 2298.5 恒定），底段半宽须用腿线斜率延拓替代——且 expand 的
        # face_maps 重投影（|t|>=0.85*w_gt → ±w_gt）也用延拓版，否则
        # 外推节点会被 snap 回夹紧常数。分段闭包：z >= 腿证据下界回落
        # 原 fit（上段零改变），z < 下界用延拓线。
        _extrap_fn = leg_chain_extrapolator(
            snapped_nodes, snapped_bars, base_fn=half_width_fn)
        _hw_for_base = _extrap_fn if _extrap_fn is not None else half_width_fn
        _pb_nodes, _pb_bars, _pb_rep = extrapolate_base_segment(
            snapped_nodes, snapped_bars, _hw_for_base,
            z_top=float(_z_top_pb),
        )
        snapped_nodes.update(_pb_nodes)
        snapped_bars.extend(_pb_bars)
        _df_pb = model.components.get("drawing_file")
        if _df_pb is not None:
            _df_pb.properties["base_segment_declaration"] = {
                **_pb_rep,
                "reason": "DXF 图纸无底段（02 图最低节点 z=6643 > 6500）",
                "declared_missing": True,
            }
        # expand 的 face_maps 重投影（body 节点 |t|>=0.85*w_gt → ±w_gt）
        # 必须用延拓版半宽，否则外推腿节点会被 snap 回夹紧常数。
        if _extrap_fn is not None:
            half_width_fn = _extrap_fn
            half_width_fitted = True

    face_nodes, face_bars = expand_4_face_symmetry(
        snapped_nodes, snapped_bars,
        weld_corner_legs=weld_corner_legs,
        add_diaphragms=add_diaphragms,
        half_width_fn=half_width_fn,
        crossarm_half_width_fn=crossarm_half_width_fn,
        # S7：生产横担层保留真实 t（桁架内部节点不得推到外缘）；GT 注入路径
        # 保持旧行为（detect_crossarm_layers=False）。
        crossarm_preserve_t=bool(spec.get("detect_crossarm_layers", False)),
        diaphragm_levels=panel_levels if panel_levels else None,
        level_source_label=(
            "gt_canonical" if level_source == "gt" else "dxf_derived"
        ) if panel_levels else None,
    )
    topology = inspect_model_topology(face_nodes, face_bars, half_width_fn=half_width_fn)
    roles = classify_members(face_nodes, face_bars)
    # Phase 3 审计锚点：展开后（未拼接/未修复）的初始门禁值
    _genuine_initial = topology.get("genuine_dangling_degree1")

    # S4 贪心共线拼接（Phase 2）：把断裂碎片杆拼回整杆。
    # 关键教训（2026-08-31 实测，三轮复核）：
    #   1. 必须在 classify_members 之后挂——否则 face_bars 无 role，
    #      role=="CROSS" 跳过不生效，40 根横担被错拼，TP@500 208→188。
    #   2. 拼接端点用精确投影极值新建节点（stitch 返回的 new_nodes），
    #      严禁吸附到现存节点（吸附引入 ≤gap 偏移，实测 TP@500 209→188）。
    #   3. 【根因修正】旧参数 gap=300/ang=10°/maxLen=4500/maxseg=3 会把
    #      「本来已单独命中 GT 的中长杆（1100~1500mm）」与短残段并成 ~2000mm
    #      合成杆（贪心按 |L−2018| 优先，这类对得分最高），毁掉已有匹配：
    #      TP@500 208→188。离线实验脚本声称的 +1/+9 系「中间合成链重复
    #      输出」bug 的假增益（760 根源杆输出 465 根含重复几何）。
    #   4. 修法：max_single_len_mm=800 只允许「短残段」参与拼接（已接近
    #      GT 杆长 2005 中位的中长杆保护不动）；max_segments=2 只两两拼。
    #      诚实复测（生产函数离线跑基线模型 + 生产评测器）：
    #      TP@100 102 持平 / TP@200 138 持平 / TP@500 208→211 (+3) /
    #      Precision@500 33.1%→34.3% (+1.2点)，无任何口径回退。
    if bool(spec.get("collinear_stitch", False)):
        from ..solve.tower_geometry import stitch_collinear_bars
        for _b in face_bars:
            if not _b.get("role"):
                _b["role"] = roles.get(str(_b.get("id")))
        face_bars, _stitch_nodes, _stitch_rep = stitch_collinear_bars(
            face_nodes, face_bars,
            gap_mm=float(spec.get("collinear_stitch_gap_mm", 300.0)),
            ang_deg=float(spec.get("collinear_stitch_ang_deg", 10.0)),
            min_merged_len_mm=float(spec.get("collinear_stitch_min_len_mm", 600.0)),
            max_merged_len_mm=float(spec.get("collinear_stitch_max_len_mm", 4500.0)),
            max_segments=int(spec.get("collinear_stitch_max_segments", 2)),
            max_single_len_mm=float(spec.get("collinear_stitch_max_single_len_mm", 0.0)),
        )
        if _stitch_nodes:
            face_nodes = dict(face_nodes)
            face_nodes.update(_stitch_nodes)
        # 拼接后杆件集合变了，重新分类 role（新 stitch_* 杆也需要 role）
        roles = classify_members(face_nodes, face_bars)
        _df = model.components.get("drawing_file")
        if _df is not None:
            _df.properties["collinear_stitch_report"] = dict(_stitch_rep)

    # Phase 3：悬空断裂修复（微型残段清除 + 端点焊接）。
    # 与 snap_dangling_endpoints_local 的关键区别：snap 只在四面展开之前
    # 修 front 面原版，镜像 B/L/R 不继承其节点合并结果——这正是 17 个
    # 悬空节点里 6 组「F 面正常、镜像面断裂」的根因。本修复在四面展开 +
    # 共线拼接之后对所有面统一执行。实测（2026-08-31）：
    #   残段清除（<250mm 孤立短杆，7 根）：噪声残根，无法匹配 GT（GT 杆长
    #     中位 ~2005mm），删除同时降 FP；
    #   端点焊接（<=350mm 内最近有效节点，6 处）：端点位移在 500mm 评测
    #     容差内，TP 无回退；
    #   剩余 2 处物理断裂（伙伴杆整体缺失，周围 450mm+ 无可接结构）留
    #     review_queue 人工复核，不无中生有。
    if bool(spec.get("repair_dangling", False)):
        from ..solve.tower_geometry import repair_dangling_endpoints
        face_bars, _repair_rep = repair_dangling_endpoints(
            face_nodes, face_bars,
            stub_max_len_mm=float(spec.get("repair_stub_max_len_mm", 250.0)),
            weld_max_mm=float(spec.get("repair_weld_max_mm", 350.0)),
            half_width_fn=half_width_fn,
        )
        roles = classify_members(face_nodes, face_bars)
        _df = model.components.get("drawing_file")
        if _df is not None:
            _df.properties["dangling_repair_report"] = dict(_repair_rep)

    # Phase 3：门禁度量「交付几何」——全部几何变换（展开/拼接/修复）之后
    # 用同一 half_width_fn 终算 topology（half_width_fn 在本作用域仍可用，
    # baseline_report 事后无法复现的问题不适用此处）。
    topology = inspect_model_topology(face_nodes, face_bars, half_width_fn=half_width_fn)

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
        # 阶段4.4 件号证据传播：复制源杆 bar_id_evidence（深拷贝防共享污染）；
        # 非 front 面（镜像/派生）必须标记 symmetry propagation——这是同一次
        # 识别在展开中的传播，不得冒充四次独立识别。
        bar_id_evidence = copy.deepcopy(
            b.get("bar_id_evidence") or (orig_comp.properties.get("bar_id_evidence") if orig_comp else []) or []
        )

        is_diaphragm = bool(b.get("diaphragm"))
        face = b.get("face")
        generated_face = face.upper() if face and face not in ("diaphragm", "center", "corner") else face

        if bar_id_evidence and (face is None or str(face).lower() != "f"):
            for _ev in bar_id_evidence:
                _ev["propagated_via"] = "symmetry_4face"
                _ev["propagated_face"] = generated_face

        # 来源引用 + 语义冻结（阶段0）：
        #   recognized   —— primary 面（front）杆件，直接从 DXF 识别，进 physical P/R
        #   mirrored     —— 镜像派生面（b/l/r），几何派生但继承原组件 SourceRef
        #   derived      —— corner_leg / center 轴，纯展示几何，不进 P/R
        #   reconstructed—— 横隔（diaphragm）/主腿节间化（panel_subdivision），
        #                   确定性重建的真实物理杆（GT 有对应角钢）
        #                   进 physical P/R，但不进 recognition P/R
        if b.get("panel_subdivision"):
            # S6 主腿节间化杆：z 切点取 canonical 平台标高（z-only 注入，
            # 用户裁定），x/y 沿原杆直线插值（DXF 几何）。这是确定性重建
            # 的真实物理杆，非展示几何——reconstructed，进 physical P/R。
            bar_source = orig_comp.source if (orig_comp is not None and orig_comp.source is not None) else SourceRef(source_type=SourceType.DERIVED, reference=str(source_file or ""), confidence=1.0)
            geometry_origin = "panel_subdivision"
            evidence_status = "reconstructed"
        elif b.get("corner_leg") or face in ("center", "corner"):
            bar_source = SourceRef(source_type=SourceType.DERIVED, reference=str(source_file or ""), confidence=1.0)
            geometry_origin = "derived_4face"
            evidence_status = "derived"
        elif is_diaphragm or face == "diaphragm":
            # 横隔：确定性重建的真实物理杆（从腿节点对称推导，非展示几何）。
            # 保留 diaphragm 标记作溯源，但 evidence_status/geometry_class 判为
            # reconstructed（进 physical P/R，不进 recognition P/R）。
            # source_type 仍 DERIVED（数据来源确为「派生计算」），不影响 P/R 判定。
            bar_source = SourceRef(source_type=SourceType.DERIVED, reference=str(source_file or ""), confidence=1.0)
            geometry_origin = "diaphragm_reconstructed"
            evidence_status = "reconstructed"
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
        #   derived      —— corner_leg / center 轴（纯展示几何）
        #   reconstructed—— 对称展开重建产物（mirrored b/l/r 面 + 横隔 diaphragm）
        #   recognized   —— primary（front）面识别原貌（非派生）
        # P5 例外：derived_parametric（底段参数化外推）按原 class 透传
        # ——parametric 口径依赖 geometry_class=derived_parametric 归层
        # （caliber_of_bar），四向镜像不改变「参数化推断」语义。
        if evidence_status == "derived":
            geometry_class = "derived"
        elif str(b.get("geometry_class") or "") == "derived_parametric":
            geometry_class = "derived_parametric"
        elif evidence_status in ("mirrored", "reconstructed"):
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
            "panel_subdivision": bool(b.get("panel_subdivision")),
            "root_bar_id": b.get("root_bar_id"),
            "level_source": b.get("level_source"),
            "generated_4face": True,
            "solve_status": "solved",
            # 证据链
            "derived_from": derived_from,
            "drawing_view": drawing_view,
            "source_file": source_file,
            "geometry_origin": geometry_origin,
            "geometry_class": geometry_class,
            "projection_refs": projection_refs,
            "bar_id_evidence": bar_id_evidence,
            "evidence_status": evidence_status,
            "length_mm_3d": round(
                math.sqrt(sum((face_nodes[b["to"]][i] - face_nodes[b["from"]][i]) ** 2 for i in range(3))), 2,
            ),
        }
        # 阶段 0.2 GT 隔离：仅「GT 半宽注入」（use_gt_half_width）才打 gt_aligned。
        # canonical 平台标高 z-only 注入（use_gt_platform_levels）不触碰 x/y，
        # 用户裁定不需要 gt_aligned 拒评——但在组件上留痕以便审计。
        if spec.get("use_gt_half_width"):
            bar_props["gt_aligned"] = True
        if b.get("panel_subdivision") and level_source == "gt":
            bar_props["panel_levels_source"] = "gt_canonical_z_only"
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
        # S1c 件号登记簿：与既有 orphan_label_ids 合并（多视图/多次展开去重累积）
        merged_orphans = list(df.properties.get("orphan_label_ids") or [])
        for lab in orphan_label_ids:
            if lab not in merged_orphans:
                merged_orphans.append(lab)
        df.properties.update({
            "expanded_4_face": True,
            "face_count": 4,
            "corner_legs": len({b["corner_index"] for b in face_bars if b.get("corner_leg") and b.get("corner_index")}),
            "diaphragm_count": sum(1 for b in face_bars if b.get("diaphragm")),
            "topology_degree1": topology["dangling_degree1"],
            "topology_crossarm_tips": topology.get("crossarm_tip_count", 0),
            "topology_genuine_dangling": topology.get("genuine_dangling_degree1", topology["dangling_degree1"]),
            # Phase 3：物理去重口径（同一杆 4 面镜像 = 1 处物理断裂），
            # 门禁主口径；面实例数（topology_genuine_dangling）作审计辅口径。
            "topology_genuine_dangling_physical": topology.get("genuine_dangling_physical"),
            # Phase 3 审计锚点：展开后未拼接/未修复的初始门禁值
            "topology_genuine_dangling_initial": _genuine_initial,
            # Phase 3：真悬空节点明细（review_queue 的数据来源）
            "genuine_dangling_nodes": topology.get("genuine_dangling_detail", []),
            "topology_components": topology["components"],
            # 阶段3.2：生产路径半宽来源标记（fit=立面主腿拟合，gt=GT注入，none=退化）
            "half_width_source": ("gt" if spec.get("use_gt_half_width")
                                  else "fit" if half_width_fitted else "none"),
            "half_width_degraded": (not spec.get("use_gt_half_width") and not half_width_fitted),
            # S1c：被剪标注残片携带的件号（几何已清、A1 证据保留）
            "orphan_label_ids": merged_orphans,
        })

    # P4.3 件号长度一致性核验（错配剥离），见 _strip_misassociated_bar_ids。
    _strip_misassociated_bar_ids(
        model,
        strip_ratio=float(spec.get("bar_id_mismatch_strip_ratio", 2.5)),
        suspect_ratio=float(spec.get("bar_id_mismatch_suspect_ratio", 1.03)),
    )
    return model


def _strip_misassociated_bar_ids(
    model: EngineeringModel,
    *,
    strip_ratio: float = 2.5,
    suspect_ratio: float = 1.03,
) -> None:
    """P4.3：件号长度一致性核验（3D 长度解算后调用）。

    按杆核验 杆长/BOM长：
        ratio > strip_ratio → 件号错配：剥离 bar_id（bar_id_detached 存原值，
          bar_id_misassociation=True），件号进 orphan 登记簿——A1 口径按
          orphan 重算，BOM 长度核验自动跳过（bar_id=None 无 BOM dim）；
        1.03 < ratio <= strip_ratio → bar_id_length_suspect=True（保留件号，
          同号多杆歧义属 review 队列，不自动剥离）；
        ratio < 0.4（识别不全）不剥离——几何问题是 Phase 5/7 战场，
        件号关联本身没错，如实报超差（诚实失败）。
    """
    _detached: List[str] = []
    _suspect: List[str] = []
    for cid, comp in model.components.items():
        if comp.kind != "tower_bar":
            continue
        p = comp.properties or {}
        bid = str(p.get("bar_id") or "")
        if not bid or bid == "None":
            continue
        bom_dim = model.dimensions.get(f"dim_bom_length_{bid}")
        if bom_dim is None or bom_dim.value is None:
            continue
        try:
            bom_len = float(bom_dim.value)
        except (TypeError, ValueError):
            continue
        if bom_len <= 0:
            continue
        actual = p.get("length_mm_3d") or p.get("length_mm")
        if actual is None:
            continue
        ratio = float(actual) / bom_len
        if ratio > strip_ratio:
            p["bar_id_detached"] = bid
            p["bar_id_misassociation"] = True
            p["bar_id"] = None
            if bid not in _detached:
                _detached.append(bid)
        elif ratio > suspect_ratio:
            p["bar_id_length_suspect"] = True
            if bid not in _suspect:
                _suspect.append(bid)
    if _detached or _suspect:
        _df = model.components.get("drawing_file")
        if _df is not None:
            _orphans = list(_df.properties.get("orphan_label_ids") or [])
            for lab in _detached:
                if lab not in _orphans:
                    _orphans.append(lab)
            _df.properties.update({
                "orphan_label_ids": _orphans,
                "bar_id_misassociated_stripped": sorted(_detached),
                "bar_id_length_suspect": sorted(_suspect),
            })


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
