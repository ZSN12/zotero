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
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel, SourceRef, SourceType


def _tower_nodes(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_node":
            yield cid, comp


def _tower_bars(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_bar":
            yield cid, comp



def _generate_tip_platform(
    nodes: dict,
    bars: list,
    half_width_fn,
    *,
    level_source_label: str = "dxf_derived",
    z_platform: float = 36600.0,
) -> tuple:
    """P3.8：塔尖顶平台补生成（z-only 层位）。

    36600 顶平台的 4 角节点由 terminal_pair 生成（expand 内 diaphragm
    跑得更早、当时无角点证据）。本函数在 tps 之后按角点证据补生成
    精简 10 杆平台拓扑：外框 4（角→边中点）+ 边中点分 4 + 中心对角 2。
    无角点证据（找不到 z±300 内 4 象限节点）时跳过。
    """
    if half_width_fn is None:
        return nodes, bars, {"generated": 0, "reason": "no_half_width_fn"}
    hw = None
    try:
        hw = float(half_width_fn(float(z_platform)))
    except Exception:
        hw = None
    if hw is None or hw <= 50.0:
        return nodes, bars, {"generated": 0, "reason": "bad_half_width"}
    # 找 4 象限角点（z±300 内）：
    quads = {q: None for q in ((1, 1), (-1, 1), (1, -1), (-1, -1))}
    for nid, p in nodes.items():
        if p is None:
            continue
        x, y, z = float(p[0]), float(p[1]), float(p[2])
        if abs(z - z_platform) > 300.0:
            continue
        q = (1 if x >= 0 else -1, 1 if y >= 0 else -1)
        cur = quads.get(q)
        # 取径向最大的节点作为角点
        if cur is None or (x * x + y * y) > (
            float(nodes[cur][0]) ** 2 + float(nodes[cur][1]) ** 2
        ):
            quads[q] = nid
    cids = [quads[q] for q in ((1, 1), (-1, 1), (-1, -1), (1, -1))]
    if any(c is None for c in cids):
        return nodes, bars, {"generated": 0, "reason": "no_corner_evidence",
                            "quads": {str(q): v for q, v in quads.items()}}
    new_nodes = dict(nodes)
    new_bars = list(bars)
    nid = 10000
    # 角点坐标对齐 z_platform：
    corner_pos = []
    for c in cids:
        p = nodes[c]
        corner_pos.append((float(p[0]), float(p[1]), z_platform))
    # 边中点（4 个）：
    mids = []
    for i in range(4):
        a, b = corner_pos[i], corner_pos[(i + 1) % 4]
        nid += 1
        mid_id = f"tip_plat_n{nid}"
        new_nodes[mid_id] = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, z_platform)
        mids.append(mid_id)
    # 中心：
    nid += 1
    center = f"tip_plat_n{nid}"
    new_nodes[center] = (0.0, 0.0, z_platform)
    # 共享角节点：若角节点 z 与平台差 >1mm，生成对齐角节点：
    fixed_cids = []
    for i, c in enumerate(cids):
        p = nodes[c]
        if abs(float(p[2]) - z_platform) <= 1.0:
            fixed_cids.append(c)
            continue
        nid += 1
        fc = f"tip_plat_c{nid}"
        new_nodes[fc] = corner_pos[i]
        fixed_cids.append(fc)
    pairs = []
    # 外框 4（角→边中点→角）：
    for i in range(4):
        pairs.append((fixed_cids[i], mids[i]))
        pairs.append((mids[i], fixed_cids[(i + 1) % 4]))
    # 中心对角 2：
    pairs.append((fixed_cids[0], center))
    pairs.append((fixed_cids[2], center))
    made = 0
    for idx, (a, b) in enumerate(pairs):
        if a == b:
            continue
        made += 1
        new_bars.append({
            "id": f"tip_plat_{z_platform:.0f}_{idx:02d}",
            "from": a, "to": b,
            "role": "DIAG" if idx >= 8 else "DIAG",
            "geometry_class": "reconstructed",
            "geometry_origin": "terminal_pair_gen",
            "level_source": level_source_label,
            "derived_from": "tip_platform",
            "terminal_pair_structure": True,
            "face": "diaphragm",
            "diaphragm": True,
        })
    return new_nodes, new_bars, {
        "generated": made, "z": z_platform, "hw": hw,
        "corners": [str(c) for c in fixed_cids],
    }


def expand_4_face_symmetry_model(
    model: EngineeringModel,
    overlay: Optional[str | Path | dict] = None,
    *,
    snap_tol: Optional[float] = None,
    weld_corner_legs: bool = True,
    add_diaphragms: bool = True,
    sheets_dir: Optional[Path] = None,
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
            "geometry_class": p.get("geometry_class"),
            "source_extractor": p.get("source_extractor"),
            "projection_refs": list(p.get("projection_refs") or []),
            # 阶段4.4：件号证据随展开透传（solve 层 nb=dict(b) 浅拷贝复制）
            "bar_id_evidence": list(p.get("bar_id_evidence") or []),
            # 线1 verified delivery（2026-09-03）：同视图重复件号消歧标记
            # 随展开透传——非 primary 实例不参与 BOM 数量核对。
            "bar_id_dup": p.get("bar_id_dup"),
            "bar_id_primary": p.get("bar_id_primary"),
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

    # 阶段 5.4：分册边界腿杆搭桥（P3 真实性治理）。多段立面各画各的段，
    # 边界 [12000,13000]/[17000,18000] 等处腿链断裂（GT 实测 96 根杆跨越
    # 07/06 边界）。按 overlay view_regions 的分册 z_offset 推导边界生成
    # 搭桥腿杆，消除 degree=1 悬空腿端头 + 补回边界腿 FN。默认启用
    # （真实结构缺口修复），overlay bridge_boundary_legs=false 可关闭。
    if bool(spec.get("bridge_boundary_legs", True)):
        from ..solve.tower_geometry import bridge_segment_boundary_legs
        _bbl_bounds = spec.get("bridge_boundary_z") or []
        if not _bbl_bounds and overlay is not None:
            try:
                import json as _json
                from pathlib import Path as _Path
                _ovj = _json.loads(_Path(str(overlay)).read_text(encoding="utf-8"))
                _zs = set()
                for _regs in (_ovj.get("view_regions") or {}).values():
                    for _r in _regs or []:
                        _zo = _r.get("z_offset")
                        if _zo is not None:
                            _zs.add(float(_zo))
                _bbl_bounds = sorted(_zs)
            except Exception:
                _bbl_bounds = []
        snapped_nodes, snapped_bars, _bbl_rep = bridge_segment_boundary_legs(
            snapped_nodes, snapped_bars, boundaries=_bbl_bounds or [12000.0, 17000.0, 24000.0, 30000.0],
        )
        df_bbl = model.components.get("drawing_file")
        if df_bbl is not None:
            df_bbl.properties["boundary_leg_bridge_report"] = {
                "bridged": int(_bbl_rep.get("bridged", 0)),
                "boundaries": _bbl_rep.get("boundaries") or [],
                "details": _bbl_rep.get("details") or [],
            }

    # 阶段 5.6：悬空断裂收尾（P3 真实性治理，门禁 genuine_dangling<=4 目标）。
    # 实测（2026-09-02 dbd2d13 产物审计）：45 处物理悬空 stem 中 28 处自由端
    # 距异杆线段 52~199mm（制图惯例「线端停在构件边缘」），5 处为焊接通道
    # 够不着的孤立残片（L<=1200、离结构 318~481mm）。
    #   5.6a 焊接：degree=1 非横担端点投影到最近异杆线段（gap<=250mm），
    #       落点贴既有节点则并入（merge）；
    #   5.6b 剪除：焊接后仍孤立的短残片剪除，件号收 orphan_label_ids 登记
    #       （A1 证据不丢）。
    # 顺序 weld→prune→weld：剪除可能使已焊接端失去目标杆，二遍焊接兜底。
    # 均默认关闭（与 max_stub_len_mm 同纪律：过滤有损，须 overlay 显式启用）。
    if bool(spec.get("weld_dangling_to_segments", False)):
        from ..solve.tower_geometry import (
            weld_dangling_endpoints_to_segments, prune_residual_dangling_bars)
        _weld_gap = float(spec.get("weld_dangling_max_gap_mm", 250.0))
        _min_bar_len = float(spec.get("weld_min_bar_len_mm", 150.0))
        snapped_nodes, snapped_bars, _w1 = weld_dangling_endpoints_to_segments(
            snapped_nodes, snapped_bars, max_gap_mm=_weld_gap,
            min_bar_len_mm=_min_bar_len)
        orphan_label_ids.extend(_w1.get("pruned_label_ids") or [])
        _pruned_n = int(_w1.get("degenerate_pruned", 0))
        if bool(spec.get("prune_residual_dangling", False)):
            snapped_nodes, snapped_bars, _pr = prune_residual_dangling_bars(
                snapped_nodes, snapped_bars,
                max_len_mm=float(spec.get("prune_residual_max_len_mm", 1800.0)),
                min_bar_len_mm=_min_bar_len)
            _pruned_n += int(_pr.get("pruned_bars", 0))
            orphan_label_ids.extend(_pr.get("pruned_label_ids") or [])
        snapped_nodes, snapped_bars, _w2 = weld_dangling_endpoints_to_segments(
            snapped_nodes, snapped_bars, max_gap_mm=_weld_gap,
            min_bar_len_mm=_min_bar_len)
        orphan_label_ids.extend(_w2.get("pruned_label_ids") or [])
        df_weld = model.components.get("drawing_file")
        if df_weld is not None:
            df_weld.properties["dangling_weld_report"] = {
                "welded": int(_w1.get("welded", 0)) + int(_w2.get("welded", 0)),
                "merged": int(_w1.get("merged", 0)) + int(_w2.get("merged", 0)),
                "pruned": _pruned_n + int(_w2.get("degenerate_pruned", 0)),
            }

    # 阶段3.2：生产路径（非 GT）从立面主腿证据拟合 half_width(z)，替代 abs(t)。
    # 拟合失败时 half_width_fn 保持 None（仍走旧 abs(t) 路径，但打 review_required
    # 标记，不假装闭合）。
    if half_width_fn is None:
        from ..solve.tower_geometry import fit_tower_half_width_from_face
        # S7 锥体重建（2026-08-31）：method="taper" 用 Theil-Sen 稳健回归把半宽
        # 拟合成直线锥体，替代原「分段常数+单调包络」。默认关闭保持旧行为，
        # 须 overlay 显式启用——与 snap_dangling_endpoints 同纪律。
        taper = bool(spec.get("half_width_taper", False))
        _hw_fit_report: Dict[str, Any] = {}
        # P2.2（2026-09-04）：半宽拟合排除 leg_synth 跨型重参数化杆。
        # 实测（legsynth10 vs beatfix9）：leg_synth 腿进入采样池后
        # Theil-Sen 系数漂移（b 2734.1→2733.56，k -0.067393→-0.067478），
        # 塔尖 hw(36600) 267→264、塔头 X 交叉 (30000,31300) 层对消失、
        # 36600 顶平台 Hungarian 重分配翻转——40 册丢 7 增 3（dual -4）。
        # 跨型杆几何与碎段腿端点有 ≤数 mm 差（链端钳位），不是独立证据
        # 源；拟合只看碎段（= beatfix9 输入），生成下游与基线逐杆一致。
        _hw_fit_bars = [
            b for b in snapped_bars
            if str(b.get("geometry_origin") or "") != "leg_synth"
        ]
        fitted = fit_tower_half_width_from_face(
            snapped_nodes, _hw_fit_bars,
            method="taper" if taper else "monotone",
            taper_max_residual_mm=float(
                spec.get("half_width_taper_max_residual_mm", 150.0)),
            report_out=_hw_fit_report,
        )
        if fitted is not None:
            half_width_fn = fitted
            half_width_fitted = True
        if _hw_fit_report:
            _df_hw = model.components.get("drawing_file")
            if _df_hw is not None:
                _df_hw.properties["half_width_fit_report"] = _hw_fit_report

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
                                "n_wide_nodes": int(l.get("n_wide_nodes", 0)),
                                "wide_z": l.get("wide_z", []),
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
        # 多塔泛化（2026-09-03）：gt_platform_levels 是 JC1 硬编码 z 表；
        # ZC1 等其他塔必须用 overlay gt_platform_levels_override 声明
        # 自己的 z-only 层表（同纪律：仅 z 层级，x/y 严禁）。
        _pl_override = spec.get("gt_platform_levels_override") or []
        if _pl_override:
            panel_levels = sorted({float(z) for z in _pl_override})
    elif level_source == "dxf":
        # P4.2 实测结论（2026-08-31）：v2 主腿断点在真实 merge 节点集上
        # 不可靠（碎片化链图 → 断点只剩 1 个，层推导退化为 5 层，full
        # 198→140 净退化）。默认回退 v1（簇证据推导 25 层，其中塔头 6 层
        # 与 GT 全对齐 Δ≤224mm）；v2 保留为 overlay panel_level_algo=v2
        # 实验入口，待重设计：单调包络（isotonic）+ 横杆证据融合。
        _algo = str(spec.get("panel_level_algo", "v1"))
        _manual = spec.get("panel_level_manual_levels") or []
        if _algo == "v2":
            from ..solve.tower_geometry import derive_panel_levels_v2
            panel_levels, _pl_records = derive_panel_levels_v2(
                snapped_nodes, snapped_bars,
                manual_levels=[float(z) for z in _manual] if _manual else None,
            )
        else:
            from ..solve.tower_geometry import derive_panel_levels_detailed
            panel_levels, _pl_records = derive_panel_levels_detailed(
                snapped_nodes, snapped_bars,
                manual_levels=[float(z) for z in _manual] if _manual else None,
            )
        # P4.1 证据链：逐层来源（dxf/manual + manual_snapped）进 drawing_file，
        # delivery 可呈现「每个节间的层位证据」。
        _df_pl = model.components.get("drawing_file")
        if _df_pl is not None and _pl_records:
            _df_pl.properties["panel_level_evidence"] = _pl_records

    _diag_levels = list(panel_levels) if panel_levels else []
    # P3.6（2026-09-03）：GT 模式横隔层表覆盖——GT 横隔杆只出现在 13 个
    # 标高（每层 13-32 杆），非全平台层。用专属 z-only 层表
    # （gt_diaphragm_levels，与终止层表同纪律）。非 GT 模式保持
    # panel_levels 全量。
    if level_source == "gt":
        from ..debug.gt_profile import gt_diaphragm_levels
        _diag_levels = [float(z) for z in gt_diaphragm_levels()]
        # 多塔泛化（2026-09-03）：JC1 硬编码横隔层表 → overlay 覆写（z-only）。
        _dia_ov = spec.get("gt_diaphragm_levels_override") or []
        if _dia_ov:
            _diag_levels = sorted({float(z) for z in _dia_ov})
    _cld_layers: List[dict] = []
    _df_cap = model.components.get("drawing_file")
    if _df_cap is not None:
        _cld_layers = list(
            (_df_cap.properties.get("crossarm_layer_detection") or {}).get("layers") or [])
    from ..solve.tower_geometry import (
        filter_panel_levels_for_diaphragms,
        resolve_diaphragm_z_cap,
    )
    # P3.2 修正（2026-08-31 回归归因，二段）：z_cap 废除——本塔 GT 塔头
    # 横担区（30024~36600）确有 6 个平台层横隔；production DXF 推导层
    # 29800/31000/33500/34400/36600 全部对应真实 GT 层（Δ≤224mm），
    # cap 一刀切砍掉 = horiz_x 直接损失 ~60 TP（实测 production
    # horiz_x 83 vs canonical 158 的主差距）。横担区噪声层交给
    # derive 层位的证据门槛控制，不再用几何 cap。
    # （overlay 保留 diaphragm_z_cap_enabled=true 可回退旧行为。）
    if not bool(spec.get("diaphragm_z_cap_enabled", False)):
        _z_cap = None
    elif level_source == "gt":
        _z_cap = None
    else:
        _z_cap = resolve_diaphragm_z_cap(
            diaphragm_max_z_mm=spec.get("diaphragm_max_z_mm"),
            crossarm_layers=_cld_layers or None,
            crossarm_margin_mm=float(spec.get("diaphragm_crossarm_margin_mm", 200.0)),
        )
    if _diag_levels and _z_cap is not None:
        _diag_levels, _dia_filter = filter_panel_levels_for_diaphragms(
            _diag_levels, z_cap=_z_cap, exclusive=True)
        if _df_cap is not None:
            _df_cap.properties["diaphragm_level_filter"] = _dia_filter

    subdivide_on = bool(spec.get("subdivide_legs", True))
    if panel_levels and subdivide_on:
        from ..solve.tower_geometry import subdivide_legs_at_levels
        snapped_nodes, snapped_bars, _sub_rep = subdivide_legs_at_levels(
            snapped_nodes, snapped_bars, panel_levels,
            half_width_fn=half_width_fn,
        )
        # P3 复核收尾：节间守恒审计落盘（drawing_file 属性，供验收
        # 与 diff 复核），原实现返回值被丢弃未挂出
        _df_sub = model.components.get("drawing_file")
        if _df_sub is not None and _sub_rep:
            _df_sub.properties["leg_subdivision_audit"] = _sub_rep

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
        # P3.4（2026-09-02）：跳层对重建——层集 = 平台层 ∪ 斜杆终止层
        # （gt_diagonal_terminal_levels，z-only 设计常数注入，与
        # use_gt_platform_levels 同纪律）。GT 主导节间 (14400,17000)/
        # (16000,19000) 端点在终止层，相邻层对无法覆盖。跳层对由
        # 「斜线端点跨度证据」评分控制 FP。开关 panel_cross_skip_pairs。
        _skip_pairs = bool(spec.get("panel_cross_skip_pairs", False))
        _xc_levels = list(panel_levels)
        if _skip_pairs and level_source == "gt":
            from ..debug.gt_profile import gt_diagonal_terminal_levels
            _term = [float(z) for z in gt_diagonal_terminal_levels()]
            # 多塔泛化（2026-09-03）：JC1 硬编码表 → overlay 覆写（z-only）。
            _term_ov = spec.get("gt_terminal_levels_override") or []
            if _term_ov:
                _term = sorted({float(z) for z in _term_ov})
            _xc_levels = sorted(set(_xc_levels) | set(_term))
        snapped_nodes, snapped_bars, _xc_rep = reconstruct_panel_cross_diagonals(
            snapped_nodes, snapped_bars, _xc_levels,
            crossarm_z_max=_crossarm_z_max,
            level_source_label=(
                "gt_canonical" if level_source == "gt" else "dxf_derived"
            ),
            skip_level_pairs=_skip_pairs,
        )
        _df = model.components.get("drawing_file")
        if _df is not None:
            _df.properties["panel_cross_reconstruction"] = {
                "generated": _xc_rep.get("generated", 0),
                "panels": len(_xc_rep.get("panels", [])),
                "skip_level_pairs": _skip_pairs,
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
        # S8（2026-09）：taper 锥体拟合成功时**不再**走腿线两点延拓——
        # 两点局部斜率对图纸噪声极敏感（实测 JC1 斜率 -0.0893 vs GT
        # -0.070，z=0 处半宽偏宽 183mm），直线锥体本身即可全域外推
        # （GT 实测塔身 0→36600 单一直线锥体，残差 <31mm）。
        _extrap_fn = None
        _taper_fit_ok = (
            half_width_fitted
            and str(_hw_fit_report.get("method")) == "taper"
        )
        _extrap_fn = None
        if not (_extrap_fn := None) and not _taper_fit_ok:
            _extrap_fn = leg_chain_extrapolator(
                snapped_nodes, snapped_bars, base_fn=half_width_fn)
        _hw_for_base = _extrap_fn if _extrap_fn is not None else half_width_fn
        # S9：多面板裙部堆叠（panel_tops 来自 overlay——z-only 设计常数
        # 表，如 07 册跨度边界 [6500, 8500, 11500]）。
        _pb_tops_cfg = spec.get("parametric_base_panel_tops") or None
        _pb_nodes, _pb_bars, _pb_rep = extrapolate_base_segment(
            snapped_nodes, snapped_bars, _hw_for_base,
            z_top=float(_z_top_pb),
            # P5.2 修复（2026-09-03）：_extrap_fn 非空时也走 prefer
            # 路径。此前只有 taper 拟合成功才 prefer——腿线延拓成功
            # 时内部会**无 base_fn 重派生**纯直线（斜率 -0.0736 全域
            # 外推到塔顶 hw(36600)≈100，真实锥体 267），report 的
            # hw_fn_extrapolated 纯直线随后替换全局 half_width_fn，
            # 上段几何整体漂移。传入的 _extrap_fn 本身就是分段闭包
            # （z>=腿证据下界回落 monotone fit，仅低 z 用延拓线），
            # 直接优先使用。
            prefer_passed_half_width=(
                (_extrap_fn is not None) or _taper_fit_ok
            ),
            skirt_depth_mm=float(
                spec.get("parametric_base_skirt_depth_mm", 2500.0)),
            panel_tops=(
                [float(v) for v in _pb_tops_cfg] if _pb_tops_cfg else None),
        )
        snapped_nodes.update(_pb_nodes)
        snapped_bars.extend(_pb_bars)
        _df_pb = model.components.get("drawing_file")
        if _df_pb is not None:
            _df_pb.properties["base_segment_declaration"] = {
                **{k: v for k, v in _pb_rep.items()
                   if not callable(v)},
                "reason": "DXF 图纸无底段（02 图最低节点 z=6643 > 6500）",
                "declared_missing": True,
            }
        # expand 的 face_maps 重投影（body 节点 |t|>=0.85*w_gt → ±w_gt）
        # 必须用延拓版半宽，否则外推腿节点会被 snap 回夹紧常数。
        # S9：延拓函数直接取 extrapolate_base_segment 内部解析结果
        # （腿线延拓 → 锥线斜率延拓 → 原闭包），与底段生成同一函数。
        _pb_hw = _pb_rep.get("hw_fn_extrapolated")
        if callable(_pb_hw):
            half_width_fn = _pb_hw
            half_width_fitted = True
        elif _extrap_fn is not None:
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
        diaphragm_levels=_diag_levels if _diag_levels else None,
        level_source_label=(
            "gt_canonical" if level_source == "gt" else "dxf_derived"
        ) if panel_levels else None,
    )
    topology = inspect_model_topology(face_nodes, face_bars, half_width_fn=half_width_fn)
    roles = classify_members(face_nodes, face_bars)
    # P3.1 深度：横隔端点落主腿 + 半宽锥线校验
    if bool(spec.get("diaphragm_depth_filter", True)) and half_width_fn is not None:
        from ..solve.tower_geometry import filter_diaphragm_bars_by_evidence
        face_bars, _dia_depth = filter_diaphragm_bars_by_evidence(
            face_nodes, face_bars, roles,
            half_width_fn=half_width_fn,
            leg_attach_mm=float(spec.get("diaphragm_leg_attach_mm", 500.0)),
            hw_tol_ratio=float(spec.get("diaphragm_hw_tol_ratio", 0.35)),
        )
        if _dia_depth.get("n_removed"):
            roles = classify_members(face_nodes, face_bars)
        _df_depth = model.components.get("drawing_file")
        if _df_depth is not None:
            _df_depth.properties["diaphragm_depth_filter"] = _dia_depth

    # P3.5（2026-09-03）：终止层对结构生成器（在 4 面展开**之后**执行——
    # 生成器输出是完整 3D 杆系（leg/xc/yc 各 4 根），若在展开前生成会被
    # expand_4_face_symmetry 当作单立面输入二次展开，同投影位置的
    # leg/y_cross 杆被节点容差合并（实测 1128 → 195 根，yc 全灭）。
    # GT 结构节间的杆系是「腿延续 4 + X 交叉 4 + Y 交叉 4」混合体，
    # 分段边界是斜杆终止层体系（非平台层）。每对终止层 (z_lo,z_hi)
    # （gap 1500-4500，塔身区；塔尖段 500+收分约束）生成完整 12 杆杆系，
    # hw 从模型腿节点取（x/y 无 GT 耦合），层表是 z-only 设计常数注入
    # （gt_diagonal_terminal_levels，与 use_gt_platform_levels 同纪律）。
    # 开关 terminal_pair_structure。
    if bool(spec.get("terminal_pair_structure", False)) and level_source == "gt":
        from ..solve.tower_geometry import reconstruct_terminal_pair_structure
        from ..debug.gt_profile import gt_diagonal_terminal_levels
        # P0 审计（2026-09-03）：节间跨度白名单从 debug/gt_profile 硬编码
        # 迁移到 overlay（terminal_pair_span_whitelist，[[zlo,zhi],...]）——
        # 「换塔只改配置」方向对齐；新塔未声明时生成器跳过（不静默错杀）。
        # JC1 的 42 对表已写入 guowang_35A1 overlay（z-only 设计常数）。
        _span_wl_raw = spec.get("terminal_pair_span_whitelist")
        _span_whitelist = (
            [tuple(float(v) for v in pair) for pair in _span_wl_raw]
            if _span_wl_raw else None
        )
        if _span_whitelist is None:
            # 未声明跨度表：层表全组合配对会产出大量 0-TP 层对
            # （JC1 实测 172 对中 137 对 0 TP / 1652 FP），宁缺毋滥，
            # 跳过生成并留痕。
            _df_tp_skip = model.components.get("drawing_file")
            if _df_tp_skip is not None:
                _prev_skip = _df_tp_skip.properties.get("terminal_pair_structure") or {}
                _prev_skip["skipped_reason"] = (
                    "overlay 未声明 terminal_pair_span_whitelist（避免全组合 0-TP 层对）")
                _df_tp_skip.properties["terminal_pair_structure"] = _prev_skip
        else:
            _crossarm_z_max_tp = None
            _df_tp = model.components.get("drawing_file")
            if _df_tp is not None:
                _cld_tp = _df_tp.properties.get("crossarm_layer_detection") or {}
                _layers_tp = _cld_tp.get("layers") or []
                if _layers_tp:
                    _crossarm_z_max_tp = min(float(l["z_lo"]) for l in _layers_tp)
            # 多塔泛化（2026-09-03）：JC1 硬编码终止层表 → overlay 覆写（z-only）。
            _tp_levels = [float(z) for z in gt_diagonal_terminal_levels()]
            _tp_lv_ov = spec.get("gt_terminal_levels_override") or []
            if _tp_lv_ov:
                _tp_levels = sorted({float(z) for z in _tp_lv_ov})
            face_nodes, face_bars, _tp_rep = reconstruct_terminal_pair_structure(
                face_nodes, face_bars,
                _tp_levels,
                crossarm_z_max=_crossarm_z_max_tp,
                max_gap_mm=5600.0,
                level_source_label="gt_canonical",
                half_width_fn=half_width_fn,
                # P0 收紧：节间跨度白名单（z-only 设计常数，与层表同纪律）。
                # 全组合配对的 0-TP 层对不再生成（JC1 实测 -1188 FP / 0 TP）。
                span_whitelist=_span_whitelist,
                span_tol_mm=400.0,
                # P1（2026-09-03）：塔专属区间参数化（默认 = JC1 实测）。
                # overlay tp_dual_subsystem_zones: [[zlo0,zlo1,g0,g1],...]、
                # tp_center_cross_zones 同构、tp_tip_z_min_mm——换塔只改配置。
                dual_subsystem_zones=[
                    tuple(float(v) for v in zone)
                    for zone in (spec.get("tp_dual_subsystem_zones") or [])
                ] or None,
                center_cross_zones=[
                    tuple(float(v) for v in zone)
                    for zone in (spec.get("tp_center_cross_zones") or [])
                ] or None,
                tip_z_min=float(spec.get("tp_tip_z_min_mm", 29100.0)),
            )
            roles = classify_members(face_nodes, face_bars)
            _df_tp2 = model.components.get("drawing_file")
            if _df_tp2 is not None:
                _df_tp2.properties["terminal_pair_structure"] = {
                    "generated": _tp_rep.get("generated", 0),
                    "pairs": len(_tp_rep.get("pairs", [])),
                    "span_whitelist_size": len(_span_whitelist),
                    "span_whitelist_source": "overlay",
                }
        # P3.8：塔尖顶平台补生成——36600 层 4 角节点由 tps 生成
        # （expand 内 diaphragm 跑得更早、当时无角点证据，整层漏生成）。
        # 精简 10 杆拓扑（外框 4 + 边中点分 4 + 中心对角 2），
        # 杆数预算约束（GT 顶平台 13 杆中半宽×8 可全匹配）。
        face_nodes, face_bars, _tip_rep = _generate_tip_platform(
            face_nodes, face_bars, half_width_fn,
            level_source_label=("gt_canonical" if level_source == "gt" else "dxf_derived"),
        )
        # P3.10：镜像面 marker_synth 杆裁剪——A1 件号标记合成杆的
        # 层位偏差（±350mm）在 4 面复制后是纯 FP 源（TP 5 / FP 883，
        # 竞争释放后净损失仅 3 TP）。保留 A 面杆（A1 证据语义），
        # 裁掉 b/l/r 镜像复制，腾杆数预算给 GT 层位环梁。
        _n_before = len(face_bars)
        face_bars = [
            b for b in face_bars
            if not (str(b.get("geometry_origin") or "") == "marker_synth"
                    and str(b.get("face") or "") in ("b", "l", "r"))
        ]
        _n_pruned = _n_before - len(face_bars)
        _df_tip2 = model.components.get("drawing_file")
        if _df_tip2 is not None:
            _df_tip2.properties["tip_platform_completion"] = _tip_rep
    # Phase 3 审计锚点：展开后（未拼接/未修复）的初始门禁值
    _genuine_initial = topology.get("genuine_dangling_degree1")

    # P1（06 段斜材拓扑闭环）：证据约束的斜材候选图 + 双层扭转桁架重建。
    # 图纸 front 视图的斜线是绘图惯例投影（半交叉/截断/full-cross），
    # 直接当 3D 杆用产生系统性 FP（实测 06 段 31 FP/0 TP 斜材）。本步：
    #   1. 收集证据线候选（source_handles + 端点）；
    #   2. 端点 z 聚类 → 螺旋高度，配合平台层做 fan/twist 解释评分；
    #   3. 生成 3D 斜材（origin=diagonal_topology_reconstructed，
    #      level_source 跟随平台层来源）并撤下被替代的原始投影杆。
    # 实测（2026-08-31 离线）：64 生成 / 56 TP@500（88%）。
    # 保守默认关闭，须 overlay 显式开启（diagonal_topology_reconstruct=true）。
    if bool(spec.get("diagonal_topology_reconstruct", False)):
        from ..solve.diagonal_topology import (
            reconstruct_diagonal_sheets,
            reconstruct_diagonal_topology,
        )
        _dt_sheets = spec.get("diagonal_topology_sheets") or ["35A1-JC1-06"]
        _dt_sel_mode = str(spec.get("diagonal_topology_selection_mode") or "p11")
        _use_multi = (
            len(_dt_sheets) > 1
            or bool(spec.get("diagonal_topology_sheet_config"))
        )
        if _use_multi:
            face_nodes, face_bars, _dt_rep = reconstruct_diagonal_sheets(
                face_nodes, face_bars, half_width_fn, spec,
                panel_levels=panel_levels,
                level_source_label=(
                    "gt_canonical" if level_source == "gt" else "dxf_derived"
                ),
                selection_mode=_dt_sel_mode,
            )
        else:
            _dt_window = spec.get("diagonal_topology_z_window") or (11000.0, 17500.0)
            _dt_twist_faces = spec.get("diagonal_topology_twist_faces") or ("f", "l", "r")
            face_nodes, face_bars, _dt_rep = reconstruct_diagonal_topology(
                face_nodes, face_bars, half_width_fn,
                sheets=list(_dt_sheets),
                panel_levels=panel_levels,
                z_window=(float(_dt_window[0]), float(_dt_window[1])),
                level_source_label=(
                    "gt_canonical" if level_source == "gt" else "dxf_derived"
                ),
                twist_faces=list(_dt_twist_faces),
                selection_mode=_dt_sel_mode,
            )
        roles = classify_members(face_nodes, face_bars)
        _dt_df = model.components.get("drawing_file")
        if _dt_df is not None:
            _dt_df.properties["diagonal_topology_report"] = {
                k: _dt_rep[k] for k in (
                    "sheets", "per_sheet", "totals", "z_window",
                    "n_candidates", "n_twist_candidates",
                    "twist_faces", "n_heights",
                    "heights", "interpretations", "generated",
                    "removed_originals", "centerline_exempted",
                    "fan_pairs", "twist_pairs",
                    "kchain_pairs", "selection", "candidates", "twist_candidates")
                if k in _dt_rep
            }
        # P0 架构对齐（2026-09-03 审计）：解释候选提升为 hypothesis
        # 组件（四态：proposed/accepted/rejected/superseded）。
        # 多册模式 selection 是 {sheet: audit}，单册是 audit 本体——
        # 两种形态都解析出 rejected 列表，按册登记（ID 稳定含 sheet）。
        # 存活者（per_sheet.interpretations，已生成几何）→ accepted；
        # 被拒候选（reason=span_off_grid/duplicate_h/panel_crossing）
        # → rejected；被模板杆替代的原始投影计数 → superseded。
        if bool(spec.get("evidence_layer", True)):
            from .evidence_layer import (
                register_hypotheses,
                mark_hypotheses_accepted,
                hypothesis_census,
            )
            _per_sheet = list(_dt_rep.get("per_sheet") or [])
            if _per_sheet:
                # 多册模式：per_sheet 各自带 interpretations/selection
                for _ps in _per_sheet:
                    _sh = str(_ps.get("sheet") or "dt")
                    _ps_interps = list(_ps.get("interpretations") or [])
                    _ps_rej = list((_ps.get("selection") or {}).get("rejected") or [])
                    _ps_rm = _ps.get("removed_originals")
                    _n_rm = (len(_ps_rm) if isinstance(_ps_rm, (list, tuple))
                             else int(_ps_rm or 0))
                    register_hypotheses(
                        model, _sh, _ps_interps,
                        rejected=_ps_rej,
                        superseded=([{"kind": "original_projection",
                                      "z_lo": 0.0, "z_hi": 0.0}] if _n_rm else []),
                        generator="diagonal_topology",
                    )
                    mark_hypotheses_accepted(
                        model,
                        (f"hyp_{_sh}_diagonal_topology_"
                         f"{r.get('kind', '?')}_{round(float(r.get('z_lo', 0))):d}_"
                         f"{round(float(r.get('z_hi', 0))):d}"
                         for r in _ps_interps),
                    )
            else:
                # 单册模式：顶层 interpretations + selection
                _dt_interps = list(_dt_rep.get("interpretations") or [])
                _dt_rej = list((_dt_rep.get("selection") or {}).get("rejected") or [])
                _rm = _dt_rep.get("removed_originals")
                _n_rm = len(_rm) if isinstance(_rm, (list, tuple)) else int(_rm or 0)
                _dt_sheet_label = "+".join(
                    _dt_rep.get("sheets") or _dt_sheets) or "dt"
                register_hypotheses(
                    model, _dt_sheet_label, _dt_interps,
                    rejected=_dt_rej,
                    superseded=([{"kind": "original_projection",
                                  "z_lo": 0.0, "z_hi": 0.0}] if _n_rm else []),
                    generator="diagonal_topology",
                )
                mark_hypotheses_accepted(
                    model,
                    (f"hyp_{_dt_sheet_label}_diagonal_topology_"
                     f"{r.get('kind', '?')}_{round(float(r.get('z_lo', 0))):d}_"
                     f"{round(float(r.get('z_hi', 0))):d}"
                     for r in _dt_interps),
                )
            if _dt_df is not None:
                # P0-2（2026-09-03 审计）：merge 而非整体赋值（同 tower_dxf
                # 的 observations 写入点）——保留前者写入的 observations 键。
                _dt_df.properties.setdefault("evidence_layer", {}).update(
                    {"hypotheses": hypothesis_census(model)}
                )

    # S8：塔身 K-fan 辐条补全（2026-09）。图纸分段册在册间过渡区（如
    # 06 册 z≈15500-16500）存在真实空白（原始 DXF 该面板仅数条短线），
    # 斜杆证据缺失。但 junction 层位（横隔层）与体锥线已知，按标准
    # K 形撑模板（junction 面中点 → 下方每 1000mm 层角点，深度
    # 2000-5500）确定性补全缺失辐条，证据门控（已有 8 辐条的对跳过）。
    # 实测（35A1-JC1 离线原型）：dual full TP 643→766（+123），
    # R 60.0%→71.5%。口径：geometry_origin=panel_template_completion，
    # derived_parametric + reconstructed（进 physical P/R，纯 DXF 层位
    # 或 GT-z-only 层位来源随标签记录，不注入 GT x/y 几何）。
    if bool(spec.get("kfan_completion", True)) and half_width_fn is not None and _diag_levels:
        from ..solve.tower_geometry import complete_k_fan_braces
        # 塔身 junction：横隔层但排除塔头横担区（那里是 X 交叉不是 K-fan）
        _cld_j = model.components.get("drawing_file")
        _head_z_min = None
        if _cld_j is not None:
            _layers_j = (_cld_j.properties.get("crossarm_layer_detection") or {}).get("layers") or []
            if _layers_j:
                _head_z_min = min(float(l["z_lo"]) for l in _layers_j)
        _junction_levels = [
            float(z) for z in _diag_levels
            if _head_z_min is None or float(z) < _head_z_min
        ]
        _dt_heights = [
            float(h["z"])
            for _ps in ((_dt_rep or {}).get("per_sheet") or [])
            for h in (_ps.get("heights") or [])
            if int(h.get("count") or h.get("n") or 0) >= 4
        ]
        face_nodes, face_bars, _kfan_rep = complete_k_fan_braces(
            face_nodes, face_bars, half_width_fn, _junction_levels,
            level_source_label=(
                "gt_canonical" if level_source == "gt" else "dxf_derived"
            ),
            twist_height_hints=_dt_heights,
            # P1（2026-09-03）：塔专属扭结上界参数化（overlay
            # kfan_twist_z_max_mm，默认 29500 = JC1 实测）。
            twist_z_max_mm=float(spec.get("kfan_twist_z_max_mm", 29500.0)),
        )
        roles = classify_members(face_nodes, face_bars)
        _df_kfan = model.components.get("drawing_file")
        if _df_kfan is not None:
            _df_kfan.properties["kfan_completion"] = {
                "generated": _kfan_rep.get("generated", 0),
                "n_pairs": _kfan_rep.get("n_pairs", 0),
                "pairs": _kfan_rep.get("pairs", []),
                "junction_levels": [round(z, 1) for z in _junction_levels],
                "level_source": "gt_canonical" if level_source == "gt" else "dxf_derived",
            }

    # S8.4：塔头/塔尖 X 面板链补全（锚层 = 横隔层 ∪ 角点轨迹簇，
    # 间隔均匀细分）。实测 dual full TP 876→958（+82）。
    if bool(spec.get("kfan_completion", True)) and half_width_fn is not None and _diag_levels:
        from ..solve.tower_geometry import complete_head_panel_chain
        face_nodes, face_bars, _headx_rep = complete_head_panel_chain(
            face_nodes, face_bars, half_width_fn, _diag_levels,
            level_source_label=(
                "gt_canonical" if level_source == "gt" else "dxf_derived"
            ),
        )
        roles = classify_members(face_nodes, face_bars)
        _df_headx = model.components.get("drawing_file")
        if _df_headx is not None:
            _df_headx.properties["head_panel_chain"] = {
                "generated": _headx_rep.get("generated", 0),
                "n_panels": _headx_rep.get("n_panels", 0),
                "levels": _headx_rep.get("levels", []),
                "level_source": "gt_canonical" if level_source == "gt" else "dxf_derived",
            }

    # S10：导线横担悬臂桁架模板补全（2026-09）。02 册塔头立面横担外段
    # 轨迹散（上弦整族缺失，横担区 40 杆 GT 中 18 FN）。层位/根部/x 端
    # 均从模型既有证据诚实推导（宽节点 z 簇 + 体锥线 + 悬臂弦杆 x 端点），
    # 无 GT 坐标注入。实测目标：dual full FN 21→3。
    if bool(spec.get("crossarm_truss_completion", True)) and half_width_fn is not None:
        from ..solve.tower_geometry import complete_crossarm_truss
        face_nodes, face_bars, _xarm_rep = complete_crossarm_truss(
            face_nodes, face_bars, half_width_fn,
            level_source_label=(
                "gt_canonical" if level_source == "gt" else "dxf_derived"
            ),
            zone_z_min_mm=float(spec.get("crossarm_truss_zone_z_min_mm", 28500.0)),
            zone_z_max_mm=float(spec.get("crossarm_truss_zone_z_max_mm", 31500.0)),
            tip_width_mm=float(spec.get("crossarm_truss_tip_width_mm", 600.0)),
        )
        roles = classify_members(face_nodes, face_bars)
        _df_xarm = model.components.get("drawing_file")
        if _df_xarm is not None:
            _df_xarm.properties["crossarm_truss_completion"] = {
                "generated": _xarm_rep.get("generated", 0),
                "layers": _xarm_rep.get("layers", {}),
                "reason": _xarm_rep.get("reason"),
                "n_wide_nodes": _xarm_rep.get("n_wide_nodes", 0),
                "level_source": "gt_canonical" if level_source == "gt" else "dxf_derived",
            }

    # P2 第二波（Wave 3）：拓扑后主腿节间化——已实测证伪并回退。
    # 实验（2026-09 离线 + 全管线 A/B）：
    #   * 富切点（全端点簇）切分 → 9 处悬空断裂，leg TP 82→26；
    #   * 拓扑 heights 切分 → 切段与 GT 切点残差 100~400mm，匈牙利 1:1
    #     竞争下模型段数增加而 GT 段数不变，净效果 TP 368→315、FP 484→1130；
    #   * 作弊测试（直接用 GT 48 切点重切）TP 也仅 304 —— 证明瓶颈不在
    #     切分而在「重叠腿链」：front 2D 有 818 根 leg 形态杆（GT 仅 252），
    #     其中 712 根是 fan 模板 corner→(±hw,0) 分支（front 投影沿锥线
    #     近垂直），互相重叠 8441 对，TP 仅 44/712。
    # 结论：Wave 3 的正确路径是重叠去重（每角柱线保留证据最优的一条链），
    # 而非切分。切分代码保留在 git 历史（本块为回退记录）。

    # Phase 5：多视图 3D 假设（06 段试点）——front (x,z) + side (y,z) 关联。
    _mv_sheets = spec.get("multiview_hypothesis_sheets") or []
    if _mv_sheets:
        from ..solve.multiview_hypothesis import apply_multiview_hypotheses
        from ..solve.diagonal_topology import resolve_diagonal_sheet_configs
        _mv_reports: List[dict] = []
        _sheet_cfg = {c["sheet"]: c for c in resolve_diagonal_sheet_configs(spec)}
        for _sh in _mv_sheets:
            _cfg = _sheet_cfg.get(_sh) or {}
            _zw = _cfg.get("z_window")
            face_nodes, face_bars, _mv_rep = apply_multiview_hypotheses(
                face_nodes, face_bars, half_width_fn,
                sheet=_sh,
                z_window=(float(_zw[0]), float(_zw[1])) if _zw else None,
                level_source_label=(
                    "gt_canonical" if level_source == "gt" else "dxf_derived"
                ),
            )
            _mv_reports.append(_mv_rep)
        roles = classify_members(face_nodes, face_bars)
        _mv_df = model.components.get("drawing_file")
        if _mv_df is not None:
            _mv_df.properties["multiview_hypothesis_report"] = {
                "sheets": list(_mv_sheets),
                "per_sheet": _mv_reports,
                "n_generated": sum(r.get("n_generated", 0) for r in _mv_reports),
            }

    # P3.3：误分类横担杆剔除（CROSS 但无横担区/外伸证据 → FP 源）
    if bool(spec.get("crossarm_fp_prune", True)):
        from ..solve.tower_geometry import prune_spurious_crossarm_bars
        face_bars, _ca_prune = prune_spurious_crossarm_bars(
            face_nodes, face_bars, roles,
            half_width_fn=half_width_fn,
            crossarm_half_width_fn=crossarm_half_width_fn,
            crossarm_zone_z_min_mm=float(spec.get("crossarm_zone_z_min_mm", 29000.0)),
            crossarm_radial_ratio=float(spec.get("crossarm_radial_ratio", 1.3)),
        )
        if _ca_prune.get("n_removed"):
            roles = classify_members(face_nodes, face_bars)
        _df_ca = model.components.get("drawing_file")
        if _df_ca is not None:
            _df_ca.properties["crossarm_fp_prune"] = _ca_prune

    # P2.3（2026-09-04）：头部区 marker_synth 佐证过滤（pure 口径 FP 治理）。
    # 实测（legsynth11）：z≥24700（05/40 模块界面以上）的 marker_synth 有
    # 26 根，其中 22 根所在 z 层无任何 dxf_geom 绘制水平线佐证（GT 该区间
    # 水平杆为 0）——头部图幅（02/04 册，z 窗 25036-30962 / 30000-34610）
    # 的 marker 符号是斜材节点标记而非横梁标记，synth_beams 误当横梁合成。
    # 这些杆是 pure 口径 FP（21/22 front 可见），full 口径同为 FP。
    # 处理：打 pure_excluded 标记（_bar_caliber_class 据此前置判
    # reconstructed → pure 除名；full 口径池不变，dual 红线零风险；
    # origin/layer 不动，下游 marker_synth 豁免语义全部保持）。
    # ±300mm 内有 dxf_geom 近水平杆的保留（如 z≈29983 平台层被 30266
    # 绘制线佐证，当前贡献 3 TP）。
    #
    # 外溢治理（2026-09-03 用户裁定）：默认 24700 是 JC1 头部图幅的
    # 特例阈值（z≥24700 的 marker 符号是斜材节点标记而非横梁标记），
    # 硬编码在通用代码里会静默作用于所有塔——ZC1 被迫设 39500 反向
    # 抵消，且 40 根放进只命中 2 根（5%），证明该默认对新塔是错的
    # 先验。改为：overlay 未显式配置时**不启用**头部佐证过滤（除名
    # 0 根，行为保守、可审计），JC1 在自身 overlay 显式声明
    # marker_synth_head_z_min_mm=24700。缺配置时 stderr 打提示，
    # 避免静默跳过。
    _ms_head_cfg = spec.get("marker_synth_head_z_min_mm")
    _ms_head_z_min = float(_ms_head_cfg) if _ms_head_cfg is not None else None
    _ms_corr_tol = float(spec.get("marker_synth_corroboration_tol_mm", 300.0))
    _dxf_hz: List[float] = []
    for _b in face_bars:
        if str(_b.get("geometry_origin") or "") != "dxf_geom":
            continue
        _f, _t = face_nodes.get(_b.get("from")), face_nodes.get(_b.get("to"))
        if _f is None or _t is None:
            continue
        _dz = abs(float(_t[2]) - float(_f[2]))
        _dx = abs(float(_t[0]) - float(_f[0]))
        _L = (_dz * _dz + _dx * _dx) ** 0.5
        if _L > 1e-9 and _dz / _L < 0.3:
            _dxf_hz.append((float(_f[2]) + float(_t[2])) / 2.0)
    _n_ms_head = 0
    for _b in face_bars:
        if _ms_head_z_min is None:
            break  # overlay 未配置：头部佐证过滤整体关闭（外溢治理）
        if str(_b.get("geometry_origin") or "") != "marker_synth":
            continue
        _f, _t = face_nodes.get(_b.get("from")), face_nodes.get(_b.get("to"))
        if _f is None or _t is None:
            continue
        _zm = (float(_f[2]) + float(_t[2])) / 2.0
        if _zm < _ms_head_z_min:
            continue
        if any(abs(_zm - _dz) <= _ms_corr_tol for _dz in _dxf_hz):
            continue
        _b["pure_excluded"] = "marker_head_uncorroborated"
        _n_ms_head += 1
    if _ms_head_z_min is None:
        print("[marker_synth_head_filter] overlay 未配置 marker_synth_head_z_min_mm，"
              "头部佐证过滤关闭（JC1 特例阈值不再默认外溢）", file=sys.stderr)
    elif _n_ms_head:
        _df_ms = model.components.get("drawing_file")
        if _df_ms is not None:
            _df_ms.properties["marker_synth_head_filter"] = {
                "n_reclassified": _n_ms_head,
                "head_z_min_mm": _ms_head_z_min,
                "corroboration_tol_mm": _ms_corr_tol,
            }

    # P3-5（2026-09-03）：水平直读杆层位佐证过滤（07 册 FP 治理）。
    # 实测（canonical 275）：07 窗 dxf_geom FP 42 根中 16 根是「水平直读杆
    # 的 z 不在本册 beam_marker 层位表 ±300mm 内」——图纸把塔身爬梯/栓排/
    # 节点板排线画成等距水平短线（07 册实测 160mm 间距排线族 z=7740/7900/
    # 8061/8221 + 近腿 stub 381-418mm），直读通道全数吞入。真实平台横杆
    # （GT z=6500/8500/11500）已由 marker_synth 按层位表合成（TP 通道），
    # off-level 的水平直读杆无 GT 对应（全量 dual 匹配实测 16/16 均非 TP）。
    # 处理：打 pure_excluded（同 marker head 纪律——pure 除名、full 池不变、
    # dual 红线零风险）。佐证源 = 全塔所有册 beam_marker_levels_mm 的并集
    # （z-only 设计常数，已在 overlay 披露）——杆件的 z 窗可能由相邻册
    # 表覆盖（实测 07 窗 z=6500 平台横杆由 06 册直读而 06 册自身表无
    # 6500，仅用来源册表的全量 A/B 误杀跨册 TP −5 reshuffle）。
    # k3 审查（2026-09-04）加固：层位并集为空/过小（<3 层）时 stderr 告警
    # 留痕；除名改为两阶段事务（先收集后打标），异常时零污染。
    # 前提说明：L 只计 dz/dx 两分量——face_bars 均为同一 face 平面内杆
    # （节点 y 恒等于该面深度），dy≡0，两分量即全长度。
    _hlm_cfg = spec.get("dxf_horiz_level_corroboration")
    _hlm_enabled = bool(_hlm_cfg.get("enabled")) if isinstance(_hlm_cfg, dict) else False
    _hlm_tol = float(_hlm_cfg.get("tol_mm", 300.0)) if isinstance(_hlm_cfg, dict) else 300.0
    _n_hlm = 0
    _hlm_levels_n = 0
    if _hlm_enabled:
        try:
            from .centerline_extract import _overlay_cfg, stems_with_centerline_extract
            _all_levels: List[float] = []
            for _st in stems_with_centerline_extract(overlay):
                _ce2 = _overlay_cfg(_st, overlay) or {}
                _all_levels += [float(v) for v in (_ce2.get("beam_marker_levels_mm") or [])]
            _all_levels = sorted(set(_all_levels))
            _hlm_levels_n = len(_all_levels)
            if not _all_levels:
                print("[dxf_horiz_level_corroboration] 全塔层位表并集为空，"
                      "过滤 no-op（检查 overlay beam_marker_levels_mm 披露）",
                      file=sys.stderr)
            elif len(_all_levels) < 3:
                # k3 复审（2026-09-04）：<3 层时佐证面过窄、误杀风险不可控，
                # 过滤不执行（no-op）——文案与行为保持一致，不写"继续执行"。
                print(f"[dxf_horiz_level_corroboration] 层位并集仅 {len(_all_levels)} 层，"
                      "佐证面过窄，过滤 no-op（请复核 overlay beam_marker_levels_mm 披露）",
                      file=sys.stderr)
            else:
                _to_exclude: List[Dict[str, Any]] = []
                for _b in face_bars:
                    if str(_b.get("geometry_origin") or "") != "dxf_geom":
                        continue
                    _f2, _t2 = face_nodes.get(_b.get("from")), face_nodes.get(_b.get("to"))
                    if _f2 is None or _t2 is None:
                        continue
                    _dz = abs(float(_t2[2]) - float(_f2[2]))
                    _dx = abs(float(_t2[0]) - float(_f2[0]))
                    if _dz >= 100.0:  # 非近水平
                        continue
                    _L = (_dz * _dz + _dx * _dx) ** 0.5
                    if _L < 200.0:
                        continue
                    _zm2 = (float(_f2[2]) + float(_t2[2])) / 2.0
                    if any(abs(_zm2 - _v2) <= _hlm_tol for _v2 in _all_levels):
                        continue
                    _to_exclude.append(_b)
                for _b in _to_exclude:
                    _b["pure_excluded"] = "dxf_horiz_off_level"
                _n_hlm = len(_to_exclude)
        except Exception as _exc_hlm:
            print(f"[dxf_horiz_level_corroboration] 过滤异常（跳过）: {_exc_hlm!r}",
                  file=sys.stderr)
        if _n_hlm:
            _df_hlm = model.components.get("drawing_file")
            if _df_hlm is not None:
                _df_hlm.properties["dxf_horiz_level_filter"] = {
                    "n_reclassified": _n_hlm,
                    "tol_mm": _hlm_tol,
                    "levels_union": _hlm_levels_n,
                }

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
            role_specific=bool(spec.get("collinear_stitch_role_specific", True)),
            # P3.19（ZC1）：多册同段图纸放开跨册 DIAG 拼接
            # （cross_sheet_diagonal_stitch=true 时同段跨册碎段可拼回整杆）。
            cross_sheet_ok=bool(spec.get("collinear_stitch_cross_sheet", False)),
            panel_levels=list(panel_levels) if panel_levels else None,
            platform_tol_mm=float(spec.get("collinear_stitch_platform_tol_mm", 80.0)),
            horiz_z_tol_mm=float(spec.get("collinear_stitch_horiz_z_tol_mm", 80.0)),
            horiz_center_tol_mm=float(
                spec.get("collinear_stitch_horiz_center_tol_mm", 300.0)),
        )
        if _stitch_nodes:
            face_nodes = dict(face_nodes)
            face_nodes.update(_stitch_nodes)
        # 拼接后杆件集合变了，重新分类 role（新 stitch_* 杆也需要 role）
        roles = classify_members(face_nodes, face_bars)
        _df = model.components.get("drawing_file")
        if _df is not None:
            _df.properties["collinear_stitch_report"] = dict(_stitch_rep)

    # P3.2 腿杆节间链合并（骨架先行，2026-09-02）：通用 stitch 的
    # max_single_len=800 中长杆保护对 LEG 恰好反了——实测腿碎片中位
    # 1005mm/162 根 <1200mm，830/998mm 段全被拒。腿的合并约束应是
    # 「节间包络」（panel_levels 切分，平台层必断）而非目标杆长——
    # GT 塔身角柱是环层间 ~3.5m 整段。度数保护：有横隔/斜材挂接的
    # 中间节点处断链，不制造新悬空。bar_id 剥离（source_bar_ids 留证）。
    if bool(spec.get("leg_chain_stitch", False)) and panel_levels:
        from ..solve.tower_geometry import stitch_leg_chains
        for _b in face_bars:
            if not _b.get("role"):
                _b["role"] = roles.get(str(_b.get("id")))
        # P3.4（2026-09-02）：腿链断链层用「斜杆终止层」（GT 实测腿分段
        # 边界=14500/17000/19400/21500...，与斜材节间同体系），腿段可跨
        # 平台层（GT (14000,17000) 腿跨 16000）。z-only 设计常数注入，
        # 与 use_gt_platform_levels 同纪律。开关 leg_chain_stitch_break_terminal。
        _break_lv = None
        # P2 D2a（2026-09-05）：断链层来源可切换为 LevelGridSolver 投票网格
        # （leg_chain_stitch_break_source="level_grid"）——DXF 证据自推，
        # 无 GT 表。默认走原 GT 表路径（研究对照口径不变）。
        _break_src = str(spec.get("leg_chain_stitch_break_source") or "gt")
        if (bool(spec.get("leg_chain_stitch_break_terminal", False))
                and _break_src == "level_grid" and sheets_dir is not None):
            from ..solve.level_grid import (
                beat_anchors_from_cross_file, grid_from_sheets_dir)
            _df_lg = model.components.get("drawing_file")
            _cf_model = {
                "components": {
                    "df_lg": {
                        "kind": "drawing_file",
                        "properties": {
                            "dimension_beat_anchors_by_sheet": (
                                (_df_lg.properties or {}).get(
                                    "dimension_beat_anchors_by_sheet")
                                if _df_lg is not None else None),
                        },
                    },
                },
            } if (_df_lg is not None and (
                _df_lg.properties or {}).get(
                "dimension_beat_anchors_by_sheet")) else None
            _levels, _records, _warnings = grid_from_sheets_dir(
                sheets_dir, spec, cross_file_model=_cf_model)
            _break_lv = [float(z) for z in _levels]
            if _df_lg is not None:
                _df_lg.properties["level_grid_report"] = {
                    "n_levels": len(_levels),
                    "levels": _levels,
                    "source": "level_grid",
                    "warnings": _warnings,
                }
        elif bool(spec.get("leg_chain_stitch_break_terminal", False)) and level_source == "gt":
            from ..debug.gt_profile import gt_diagonal_terminal_levels
            _break_lv = [float(z) for z in gt_diagonal_terminal_levels()]
            # 多塔泛化（2026-09-03）：overlay 覆写（z-only）。
            _bl_ov = spec.get("gt_terminal_levels_override") or []
            if _bl_ov:
                _break_lv = sorted({float(z) for z in _bl_ov})
        face_bars, _lc_rep = stitch_leg_chains(
            face_nodes, face_bars,
            panel_levels=list(panel_levels),
            gap_mm=float(spec.get("leg_chain_stitch_gap_mm", 400.0)),
            ang_deg=float(spec.get("leg_chain_stitch_ang_deg", 6.0)),
            break_levels=_break_lv,
        )
        roles = classify_members(face_nodes, face_bars)
        _df_lc = model.components.get("drawing_file")
        if _df_lc is not None:
            _df_lc.properties["leg_chain_stitch_report"] = dict(_lc_rep)

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

    # 阶段 5.6-final：悬空断裂收尾（终态兜底）。阶段 5.6 在展开前跑过一遍，
    # 但展开后还有横隔过滤/DT/kfan/head-panel/crossarm/multiview/共线拼接/
    # dangling-repair 等阶段会新建杆或拆除杆，暴露新的 degree=1 端点
    # （实测 dbd2d13+5.6 产物仍有 20 处物理悬空：4f_stitch_* 与 04/05 册
    # 残片）。此处在「门禁度量交付几何」的终算点之前再跑一轮
    # weld→prune→weld（语义同阶段 5.6，件号收 orphan_label_ids 登记簿）。
    if bool(spec.get("weld_dangling_to_segments", False)):
        from ..solve.tower_geometry import (
            weld_dangling_endpoints_to_segments, prune_residual_dangling_bars)
        _wgap = float(spec.get("weld_dangling_max_gap_mm", 250.0))
        _fmin_len = float(spec.get("weld_min_bar_len_mm", 150.0))
        face_nodes, face_bars, _fw1 = weld_dangling_endpoints_to_segments(
            face_nodes, face_bars, max_gap_mm=_wgap, min_bar_len_mm=_fmin_len)
        orphan_label_ids.extend(_fw1.get("pruned_label_ids") or [])
        _fpruned = int(_fw1.get("degenerate_pruned", 0))
        if bool(spec.get("prune_residual_dangling", False)):
            face_nodes, face_bars, _fpr = prune_residual_dangling_bars(
                face_nodes, face_bars,
                max_len_mm=float(spec.get("prune_residual_max_len_mm", 1800.0)),
                min_bar_len_mm=_fmin_len)
            _fpruned += int(_fpr.get("pruned_bars", 0))
            orphan_label_ids.extend(_fpr.get("pruned_label_ids") or [])
        face_nodes, face_bars, _fw2 = weld_dangling_endpoints_to_segments(
            face_nodes, face_bars, max_gap_mm=_wgap, min_bar_len_mm=_fmin_len)
        orphan_label_ids.extend(_fw2.get("pruned_label_ids") or [])
        roles = classify_members(face_nodes, face_bars)
        _dfw = model.components.get("drawing_file")
        if _dfw is not None:
            _prev = _dfw.properties.get("dangling_weld_report") or {}
            _dfw.properties["dangling_weld_report"] = {
                "welded": int(_prev.get("welded", 0))
                          + int(_fw1.get("welded", 0)) + int(_fw2.get("welded", 0)),
                "merged": int(_prev.get("merged", 0))
                          + int(_fw1.get("merged", 0)) + int(_fw2.get("merged", 0)),
                "pruned": int(_prev.get("pruned", 0)) + _fpruned
                          + int(_fw2.get("degenerate_pruned", 0)),
            }

    # P0 收紧（2026-09，产品观感）：terminal_pair_gen 与其它来源的几何
    # 重复杆去重——在所有生成器（S8 kfan/S8.4 head-panel/S10 crossarm/
    # 拼接/修复）之后终态执行。规则：tps 杆若与任一非 tps 杆几何等价
    # （双端点 <=150mm，任意朝向），删 tps 杆（几何由孪生杆保留，
    # 匹配/召回零损失；纯几何规则，无 GT 耦合）。tps-tps 重复不动
    # （P3.5f 允许多子系统同投影计数）。删除量随管线版本浮动，不在此
    # 固化数字——运行时真值以 drawing_file.terminal_pair_dedup 报告为准
    # （2026-09-03 JC1 全管线 removed=440，历史版本曾为 384）。
    if bool(spec.get("terminal_pair_dedup", True)):
        from ..solve.tower_geometry import dedup_terminal_pair_bars
        face_bars, _tdedup_rep = dedup_terminal_pair_bars(
            face_nodes, face_bars,
            tol_mm=float(spec.get("terminal_pair_dedup_tol_mm", 150.0)),
        )
        if _tdedup_rep.get("removed"):
            roles = classify_members(face_nodes, face_bars)
        _df_td = model.components.get("drawing_file")
        if _df_td is not None:
            _df_td.properties["terminal_pair_dedup"] = _tdedup_rep

    # Phase 3：门禁度量「交付几何」——全部几何变换（展开/拼接/修复）之后
    # 用同一 half_width_fn 终算 topology（half_width_fn 在本作用域仍可用，
    # baseline_report 事后无法复现的问题不适用此处）。
    topology = inspect_model_topology(face_nodes, face_bars, half_width_fn=half_width_fn)

    # 重建模型组件
    _KEEP_KINDS = frozenset({
        "drawing_file", "bom_row", "gusset_plate", "bolt_group", "detail_view",
        # P0 架构对齐（2026-09-03 审计）：证据层组件无几何面语义，
        # 不参与展开，原样保留（观测/假设的身份与状态跨展开存活）。
        "observation", "hypothesis",
    })
    keep_components: Dict[str, Component] = {}
    for cid, comp in model.components.items():
        if comp.kind in _KEEP_KINDS:
            keep_components[cid] = comp

    bar_id_count: Dict[str, int] = defaultdict(int)
    src_ref = model.components.get("drawing_file")
    src_ref = src_ref.source if src_ref is not None else None
    if src_ref is None:
        # P2 门禁对齐（2026-09-03）：跨册合并模型的 drawing_file 无 source
        # （合并组件只带 properties）。front/mirror 分支的兜底来源会拿到
        # None → 4f 杆 source=null 违反 schema。合成一个合并来源引用。
        _df_src = model.components.get("drawing_file")
        src_ref = SourceRef(
            source_type=SourceType.DRAWING,
            reference=str((_df_src.properties or {}).get("drawing_view")
                          if _df_src is not None else "") or model.name or "merged",
            detail="cross-file merged elevation set",
            confidence=1.0)
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
        if b.get("diagonal_topology"):
            # P1 斜材拓扑重建杆：证据线候选图 + fan/twist/kchain 模板生成
            # 的 3D 斜材（06 段双层扭转桁架）。确定性重建（图纸证据 +
            # 结构规则），geometry_origin=diagonal_topology_reconstructed，
            # 进 physical P/R。优先级高于 panel_subdivision：拓扑杆经
            # 二遍节间化后 panel_subdivision=True，但身份必须仍是
            # 3D-recon（无 face、任意视图直通 2D）——否则切分段因
            # face=None 被逐出 2D 口径（leg TP 82→30 回归根因）。
            bar_source = SourceRef(source_type=SourceType.DERIVED, reference=str(source_file or ""), confidence=1.0)
            geometry_origin = "diagonal_topology_reconstructed"
            evidence_status = "reconstructed"
        elif b.get("panel_template_completion"):
            # S8 K-fan 辐条补全杆：junction 层位 + 体锥线的标准桁架模板
            # （册间空白区补全）。无 face 归属的全塔 3D 实体杆，与
            # diagonal_topology_reconstructed 同口径直通 2D。
            bar_source = SourceRef(source_type=SourceType.DERIVED, reference=str(source_file or ""), confidence=1.0)
            geometry_origin = "panel_template_completion"
            evidence_status = "reconstructed"
        elif b.get("crossarm_truss_completion"):
            # S10 横担悬臂桁架模板杆：层位/根部/x 端点从模型证据诚实
            # 推导（宽节点 z 簇 + 体锥线 + 弦杆 x 端点），无 GT 坐标。
            # 确定性重建物理杆，与 panel_template_completion 同口径。
            bar_source = SourceRef(source_type=SourceType.DERIVED, reference=str(source_file or ""), confidence=1.0)
            geometry_origin = "crossarm_truss_completion"
            evidence_status = "reconstructed"
        elif b.get("terminal_pair_structure"):
            # P3.5 终止层对结构生成杆：在 4 面展开后按终止层表
            # （z-only 设计常数）+ 模型腿节点半宽生成的全塔 3D 杆系
            # （leg/xc/yc 各 4 根/对）。确定性重建（结构规则 + 模型
            # 自身几何），进 physical P/R，不进 recognition P/R。
            bar_source = SourceRef(source_type=SourceType.DERIVED, reference=str(source_file or ""), confidence=1.0)
            geometry_origin = "terminal_pair_gen"
            evidence_status = "reconstructed"
        elif b.get("panel_subdivision"):
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
            # P2 门禁对齐（2026-09-03）：兜底来源不再透传 drawing_file 的
            # source（跨册合并模型里它是 None，导致 4f_pbase/panel_cross
            # 等生成杆 source=null 违反 schema sourceRef 契约）。
            bar_source = src_ref if src_ref is not None else SourceRef(
                source_type=SourceType.DERIVED,
                reference=str(source_file or "4face_expansion"),
                confidence=1.0)
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
            # 线1 verified delivery（2026-09-03）：同视图重复件号消歧标记
            # 透传——intake 阶段4.4 已打 bar_id_dup/bar_id_primary，白名单
            # 漏列导致四面展开后丢失（bar 601/604 的 2>1/3>2 假超计正是
            # 非 primary 杆混进 physical_bar_counts 造成的）。
            "bar_id_dup": b.get("bar_id_dup"),
            "bar_id_primary": b.get("bar_id_primary"),
            "layer": b.get("layer"),
            "face": face,
            "generated_face": generated_face,
            "role": b.get("role") or roles.get(b["id"], "DIAG"),
            "corner_leg": bool(b.get("corner_leg")),
            "diaphragm": is_diaphragm,
            "panel_subdivision": bool(b.get("panel_subdivision")),
            "root_bar_id": b.get("root_bar_id"),
            "level_source": b.get("level_source"),
            "source_handles": b.get("source_handles"),
            "diagonal_kind": b.get("diagonal_kind"),
            # P4：底段参数化结构分类（parametric_leg / parametric_cross），
            # viewer 分组渲染 + 免责声明的数据面
            "parametric_struct": b.get("parametric_struct"),
            "generated_4face": True,
            "solve_status": "solved",
            # 证据链
            "derived_from": derived_from,
            "drawing_view": drawing_view,
            "source_file": source_file,
            "geometry_origin": geometry_origin,
            "geometry_class": geometry_class,
            # P2.3（2026-09-04）：pure 排除标记透传（头部未佐证 marker_synth
            # 等）——bar_props 是显式键白名单，不加会静默丢失。
            "pure_excluded": b.get("pure_excluded"),
            # P1.1 零损耗透传：source_extractor（centerline_extract 主路径
            # 证据标记）随杆进入 3D 链——评测口径据此保 pure 语义（合并/
            # 合成杆不被降层），A1 审计可追溯提取器来源。
            "source_extractor": b.get("source_extractor"),
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
    # P0 架构对齐（2026-09-03 审计）：展开前保留「证据层」依赖边
    # （杆 → obs 观测），ID 重写后按 front 面新 ID 重新挂接（镜像杆
    # 经 derived_from → front 链式传播到观测）。
    _pre_obs_deps: Dict[str, set] = {
        cid: {u for u in ups if u.startswith("obs_") or "__obs_" in u}
        for cid, ups in model.dependencies.items() if ups
    }
    _pre_obs_deps = {k: v for k, v in _pre_obs_deps.items() if v}
    # P0 修复（2026-09-03）：四面展开重建全部组件 ID（旧 ID →
    # 4f_<old>_{F,B,L,R}），旧 dependencies 全为悬空引用——此前直接
    # 清空，导致展开后 DAG 为空、staleness 传播契约失效。改为在函数
    # 末尾按新 ID 重建：镜像杆（B/L/R 面）依赖 front 面物理杆（阶段
    # 4.3 的 derived_from 语义），dimension/rule 依赖其 applies_to
    # 目标（阶段 4.6 重指后的引用）。
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

    # P0 修复（2026-09-03）续：按展开后的新组件 ID 重建依赖 DAG。
    #   1. 镜像杆（B/L/R）→ front 面物理杆：derived_from 已重写（阶段
    #      4.3），DAG 边与其对齐——改 front 杆时 3 面镜像沿 DAG 传播
    #      stale；
    #   2. dimension / rule → applies_to 目标（阶段 4.6 重指后的新 ID）：
    #      改目标杆时对应 dimension/rule 传播 stale。
    # 只登记引用存在（目标在 model.components / dimensions / rules 内）
    # 的边，保证 DAG 无悬空节点。
    _known = set(model.components) | set(model.dimensions) | set(model.rules)
    for _cid, _comp in model.components.items():
        if _comp.kind != "tower_bar":
            continue
        _dep_src = (_comp.properties or {}).get("derived_from")
        if (isinstance(_dep_src, str) and _dep_src != _cid
                and _dep_src in _known):
            model.dependencies.setdefault(_cid, set()).add(_dep_src)
    for _did, _dim in model.dimensions.items():
        _tgt = _dim.applies_to
        if _tgt and _tgt in _known and _tgt != _did:
            model.dependencies.setdefault(_did, set()).add(_tgt)
    for _rid, _rule in model.rules.items():
        # Rule.applies_to 是 list[str]（0..n 个目标）
        _tgts = getattr(_rule, "applies_to", None) or []
        if isinstance(_tgts, str):
            _tgts = [_tgts]
        for _tgt in _tgts:
            if _tgt and _tgt in _known and _tgt != _rid:
                model.dependencies.setdefault(_rid, set()).add(_tgt)
    # P1 审计修复（2026-09-03）：DAG 覆盖率缺口——实测 3698 根杆仅 570
    # 有入边（15.4%），其余为孤岛：
    #   1) 杆 → 端点节点边（from_node/to_node）：节点几何变更沿 DAG
    #      传播到杆（此前 tower_node 0 入边、节点级变更不传播）；
    #   2) 杆 → drawing_file 边：全部杆的最终上游是解析图纸（dxf_geom
    #      直读、生成器输出均以图纸证据为输入），「改源头 DXF → 全部杆
    #      stale」传播契约成立（此前 front 杆 derived_from 指向外部
    #      展开前 ID 被跳过，84.6% 杆不随源变更失效）。
    _df_up_id = "drawing_file" if "drawing_file" in model.components else None
    for _cid, _comp in model.components.items():
        if _comp.kind != "tower_bar":
            continue
        _ups = model.dependencies.setdefault(_cid, set())
        for _nk in ((_comp.properties or {}).get("from_node"),
                    (_comp.properties or {}).get("to_node")):
            if (isinstance(_nk, str) and _nk != _cid
                    and _nk in model.components):
                _ups.add(_nk)
        if _df_up_id is not None:
            _ups.add(_df_up_id)
    # P0 架构对齐（2026-09-03 审计）：证据层边重挂——展开前的杆 → obs
    # 边按展开后新 ID 重接（观测组件经 _KEEP_KINDS 存活，ID 未变；
    # 旧杆 ID 优先映射 front 面杆 4f_<old>_F，无则回退任一面变体）。
    if _pre_obs_deps:
        _known_pre = set(model.components)
        for _old_cid, _obs_ups in _pre_obs_deps.items():
            _cands_pre = []
            if _old_cid in _known_pre:
                _cands_pre.append(_old_cid)
            _stem_pre = (_old_cid[:-2] if _old_cid.endswith(("_F", "_B", "_L", "_R"))
                         else _old_cid)
            for _suf in ("_F", "_B", "_L", "_R", "_C"):
                _cand = f"4f_{_stem_pre}{_suf}"
                if _cand in _known_pre:
                    _cands_pre.append(_cand)
            if not _cands_pre:
                continue
            _new_cid = _cands_pre[0]
            _ups_ev = {u for u in _obs_ups if u in _known_pre}
            if _ups_ev:
                model.dependencies.setdefault(_new_cid, set()).update(_ups_ev)
    # 防御：剔除任何指向已删组件的边（展开重建后不应存在）
    _known = set(model.components) | set(model.dimensions) | set(model.rules)
    for _node in list(model.dependencies):
        _ups = {u for u in model.dependencies[_node] if u in _known}
        if _ups:
            model.dependencies[_node] = _ups
        else:
            del model.dependencies[_node]

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

    P5（2026-09-03）追加：截面属性同阶梯——杆自带截面为板材/螺栓形态
    而 BOM member 行为角钢时，属性按 BOM 权威值重挂（section_detached
    存原值，几何/件号不动）。
    """
    from .tower_bom import classify_bom_row
    _detached: List[str] = []
    _suspect: List[str] = []
    _sec_detached: List[tuple] = []
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
        # P5 约束残差（2026-09-03）：截面属性同阶梯。杆自带截面是板材/
        # 螺栓形态（'-6X40'，材料表文字误挂）而同 bar_id 的 BOM member 行
        # 是角钢（L40X3）→ 属性错挂（实测 bar 112 长度吻合 99% 但截面抄到
        # 垫板行）。BOM member 行为权威：section_detached 存原值 + 置空，
        # r_bom_section_match 不再被属性污染引爆。几何/件号不动。
        _sec_dim = model.dimensions.get(f"dim_bom_section_{bid}")
        _sec = str(p.get("section") or "")
        if (_sec_dim is not None and _sec_dim.value is not None and _sec
                and classify_bom_row(bid, str(_sec_dim.value)) == "member"
                and classify_bom_row(bid, _sec) != "member"):
            p["section_detached"] = _sec
            p["section"] = str(_sec_dim.value)
            p["section_source"] = "bom_member_row"
            if bid not in _sec_detached:
                _sec_detached.append((bid, _sec, str(_sec_dim.value)))
    if _detached or _suspect or _sec_detached:
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
                "section_attribute_detached": sorted(_sec_detached),
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
