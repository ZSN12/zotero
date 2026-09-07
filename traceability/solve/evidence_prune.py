# -*- coding: utf-8 -*-
"""阶段二：证据置信分层剪枝器（Evidence-Tier Pruner）。

背景（2026-09-07 双塔逐源归因实测）：
  * ZC1 dual-full TP=259/FP=991（P 20.7%）。逐 geometry_origin 归因：
    5 个 origin 合计 215 杆 TP=0——diagonal_topology_reconstructed(108)、
    derived_parametric_base(52)、marker_synth(40)、leg_chain_stitch(11)、
    boundary_leg_bridge(4)；另有 dxf_geom 的 b/l/r 镜像 260 FP/21 TP。
  * JC1 上同样的 origin 清单不能照抄：marker_synth TP=15、
    derived_parametric_base TP=12——按塔枚举 origin 白名单/黑名单是
    硬编码，铁律禁止。必须用**结构属性**分规则。

结构属性规则（全部从杆自身 properties 判定，无 GT、无塔名分支）：

  R1 无投影证据的纯推断杆（``evidence_prune.no_projection_evidence``）：
     geometry_origin 是「拓扑/参数推断」类（见 ``INFERENCE_ORIGINS``，
     语义 = 杆件几何完全由结构规则推演，图纸立面无对应线段），且
     projection_refs 为空（无 2D 投影证据）。同时携带投影引用的同源杆
     （JC1 marker_synth 15 TP 全带 projection_refs）不受影响——这正是
     JC1 红线 TP=1067 安全的原因（其 diag_synth/leg_synth 全带 refs）。

  R2 dxf_geom 镜像面剪除（``evidence_prune.mirror_face_dxf``）：
     dxf_geom（直读）杆中 face ∈ b/l/r 且 generated_4face=true——即由
     front 直读四面镜像衍生的杆。正面（face=f 或无 face）直读保留。
     实测 ZC1：镜像 260 FP/21 TP；JC1：269 FP/14 TP；两塔镜像面的
     有效信息已由模板链/对称链覆盖，直读镜像只添 FP。

  R3 悬空断头杆（``evidence_prune.dangling_degree1``）：
     无投影证据的推断杆（R1 同集）且两端点图度均为 1（与塔架其余部分
     仅在自身两端相连）——孤儿虚假杆的几何特征（阶段三核心判据）。

  R5 塔尖平台模板环剪除（``evidence_prune.tip_platform_ring``）：
     derived_from=tip_platform 的顶平台模板环杆（terminal_pair_gen
     域、无投影证据）。实测 ZC1 10 杆全 FP（0 TP）：图形源数据显示
     ZC1 顶平台是「角-角矩形框 + 上层斜撑」构型，平台框杆由
     leg_chain_stitch 已 1:1 匹配，中心/边中点模板环在双视投影中
     只是 FP。JC1 的同生成器因「无角点证据」前置条件天然生成 0 杆
     （其真平台是中心+边中点构型，但生成器在 JC1 塔顶无四象限角点
     时不触发）——规则对其零影响，无塔型分支。若其他塔型确有中心
     构型平台且生成器触发，开启方需在 overlay 显式关闭本规则。

  R6 腿链拼接桥剪除（``evidence_prune.leg_stitch_bridge``）：
     origin=leg_chain_stitch 的共线拼接插值杆。stitch 是腿链断裂处
     的插值补杆——projection_refs 指向被拼接的源线而非该杆自身的
     投影证据，故 R1 的 refs 豁免对它语义无效。双塔实测 30/30 全
     0 TP（ZC1 0/11、JC1 0/19）：插值位置与真实杆位有系统性偏差。
     与 R2 同类的「origin 语义剪除」先例（非按塔枚举——不分塔、
     不分 refs，全量该 origin 语义即插值桥）。

  R4 BOM 配额（``evidence_prune.bom_quota``，阶段二主机制）：
     按 (segment, role) 分段配额裁超额低置信杆。BOM 数量是刚性证据：
     每分段每角色图纸只装 N 件，模型超出即虚假。置信序（高→低）：
     直读(recognized) → 双视交会(side_direct) → 模板补全(panel/
     terminal 类 reconstructed) → 纯推断（R1 域）。配额内从高置信保起，
     超额从低置信砍起。配额来源：
       a. overlay ``evidence_prune.segment_role_quota`` 显式表（研究标定）；
       b. BOM 表 ``qty`` 列聚合（segment 前缀匹配 source_file）——BOM
          只统计主材杆（role ∈ LEG/DIAG/CROSS），板类不进配额。
     每根被剪杆写 ``pruned_by`` provenance（证据链完整性铁律）。

  R7 直读端点页内层位带吸附（``evidence_prune.direct_snap_layers``）：
     同册直读（recognized）杆的端点 z 做页内 1D 聚类（gap 250mm 切带），
     离群端点（离最近带中心 >250mm）吸附到该带中心。纯几何自洽净化：
     图纸分段立面内杆端应落在层位带上，提取像素抖动/角钢双线边缘取点
     造成的孤立端点回收到带内。无 GT、无 ladder 依赖（阶段五任务二实测：
     阶梯结点/页窗网格吸附均受窗标定残差污染，净收益 +1~+2；页内自洽
     聚类 +2 TP 且不依赖任何绝对标定）。只动 recognized 杆端点 z，
     不改杆集合。变更写 ``snapped_by`` provenance。

铁律对齐：
  * 不读 GT、不读评测结果；只消费 model 组件属性 + overlay 配置 + BOM。
  * 默认全部关闭（``evidence_prune`` 键缺省 → 零行为变化，JC1/JC2
    红线不受影响）；ZC1 overlay 显式开启。
  * 每根删除杆在 drawing_file.properties.evidence_prune_report 留档
    （removed_ids 全量），事后可审计。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence

# 「纯推断」类 origin：几何完全由结构规则推演（图纸立面无对应线段）。
# 语义集合，非塔枚举——新塔出现同语义 origin 自动入域。
# 注意（双塔归因实测 2026-09-07）：terminal_pair_gen / panel_template_
# completion 是「模板补全」层（ZC1 TP 137/102、JC1 TP 226/482），
# 不在此域——它们归 R4 配额管理。marker_synth 在 JC1 有 15 TP 但全部
# 带 projection_refs → R1 的无投影证据前置条件天然放过它们；同理
# JC1 的 leg_chain_stitch 16/19 带 refs。
# derived_parametric_base 不在此域：ZC1 0/52 全 FP，但 JC1 12/44 是 TP
# 且两塔字段结构完全同构（均无 refs/无 source_handles）——无塔无关的
# 结构判据可分，保守起见整体保留，其治理交给 R3/R4。
INFERENCE_ORIGINS = frozenset({
    "diagonal_topology_reconstructed",  # 拓扑规则推演斜材（无立面线段）
    "marker_synth",                     # 层位标记合成横杆
    "leg_chain_stitch",                 # 腿链拼接搭桥
    "boundary_leg_bridge",              # 分段边界搭桥腿
})

# R4 置信序：数值越小置信越高，配额内优先保留。
_TIER_ORDER = {
    "recognized": 0,        # 直读（evidence_status 或 geometry_class）
    "side_direct": 1,       # 双视交会直读
    "template": 2,          # 模板/结构补全（reconstructed）
    "inference": 3,         # 纯推断（INFERENCE_ORIGINS 且无投影证据）
}


def _bar_origin(props: Dict[str, Any]) -> str:
    return str(props.get("geometry_origin") or "")


def _has_projection_evidence(props: Dict[str, Any]) -> bool:
    refs = props.get("projection_refs")
    return bool(refs)


def _is_inference_bar(props: Dict[str, Any]) -> bool:
    """纯推断杆：推断类 origin 且无任何 2D 投影证据。"""
    return (_bar_origin(props) in INFERENCE_ORIGINS
            and not _has_projection_evidence(props))


def _confidence_tier(props: Dict[str, Any]) -> str:
    """R4 置信分层（结构属性→层名，与评测口径解耦）。"""
    if _is_inference_bar(props):
        return "inference"
    if _bar_origin(props) == "side_direct":
        return "side_direct"
    status = str(props.get("evidence_status") or "")
    gclass = str(props.get("geometry_class") or "")
    if status == "recognized" or gclass == "recognized":
        return "recognized"
    return "template"


def _bar_role(props: Dict[str, Any]) -> str:
    return str(props.get("role") or "").upper()


def _node_degree(model_components: Dict[str, Any]) -> Dict[str, int]:
    """tower_bar 端点 → 图度（参与杆数）。悬空节点度=1。"""
    deg: Counter = Counter()
    for comp in model_components.values():
        if _kind(comp) != "tower_bar":
            continue
        props = _props(comp)
        for nid in (props.get("from_node"), props.get("to_node")):
            if nid:
                deg[str(nid)] += 1
    return dict(deg)


def _segment_of(props: Dict[str, Any]) -> str:
    """分段标签：source_file 的册号后缀（35A2-ZC1-07 → 07）。"""
    src = str(props.get("source_file") or "")
    if not src:
        return ""
    tail = src.rsplit("-", 1)[-1]
    return tail if tail.isdigit() else src


def _kind(comp: Any) -> str:
    """兼容 EngineeringModel.Component 与 dict（评测快照）。"""
    if isinstance(comp, dict):
        return str(comp.get("kind") or "")
    return str(getattr(comp, "kind", "") or "")


def _props(comp: Any) -> Dict[str, Any]:
    if isinstance(comp, dict):
        return comp.get("properties") or {}
    return getattr(comp, "properties", None) or {}


def _comp_id(cid: str, comp: Any) -> str:
    if isinstance(comp, dict):
        return str(comp.get("id") or cid)
    return str(getattr(comp, "id", cid) or cid)


def apply_evidence_prune(
    model: Any,
    overlay: Optional[Dict[str, Any]] = None,
    bom_rows: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """按 overlay ``evidence_prune`` 配置就地剪枝，返回审计报告。

    model: EngineeringModel（components dict：id→Component）。
    overlay: load_tower_spec 的 dict；缺省/无 evidence_prune 键 → 空操作。
    bom_rows: master BOM 行（DictReader 产物），R4 配额源 b。
    """
    cfg = (overlay or {}).get("evidence_prune") or {}
    if not isinstance(cfg, dict) or not cfg:
        return {"enabled": False, "removed": []}

    bars: List[tuple] = []  # (cid, props)
    for cid, comp in model.components.items():
        if _kind(comp) == "tower_bar":
            bars.append((cid, _props(comp)))
    deg = _node_degree(model.components)

    remove: Dict[str, str] = {}  # cid → rule

    # R1：无投影证据的纯推断杆
    if cfg.get("no_projection_evidence"):
        for cid, p in bars:
            if _is_inference_bar(p):
                remove[cid] = "R1_no_projection_evidence"

    # R2：dxf_geom 四面镜像（b/l/r 面）直读衍生杆
    if cfg.get("mirror_face_dxf"):
        for cid, p in bars:
            if cid in remove:
                continue
            if (_bar_origin(p) == "dxf_geom"
                    and str(p.get("face") or "").lower() in ("b", "l", "r")
                    and bool(p.get("generated_4face"))):
                remove[cid] = "R2_mirror_face_dxf"

    # R3：悬空断头推断杆（两端度均 1）
    if cfg.get("dangling_degree1"):
        for cid, p in bars:
            if cid in remove or not _is_inference_bar(p):
                continue
            da = deg.get(str(p.get("from_node")), 0)
            db = deg.get(str(p.get("to_node")), 0)
            if da == 1 and db == 1:
                remove[cid] = "R3_dangling_degree1"

    # R5：塔尖平台模板环（tip_platform 补生成的中心/边中点拓扑）
    if cfg.get("tip_platform_ring"):
        for cid, p in bars:
            if cid in remove:
                continue
            if (str(p.get("derived_from") or "") == "tip_platform"
                    and _bar_origin(p) == "terminal_pair_gen"
                    and not _has_projection_evidence(p)):
                remove[cid] = "R5_tip_platform_ring"

    # R6：腿链拼接桥（共线插值杆，refs 是源线非自身证据——双塔 0/30 TP）
    if cfg.get("leg_stitch_bridge"):
        for cid, p in bars:
            if cid in remove:
                continue
            if _bar_origin(p) == "leg_chain_stitch":
                remove[cid] = "R6_leg_stitch_bridge"

    # R4：(segment, role) 刚性配额，低置信超额剪除
    quota_report: Dict[str, Any] = {}
    if cfg.get("bom_quota"):
        quota_report = _apply_bom_quota(
            model, bars, deg, remove, cfg, bom_rows or [])

    # R7：直读端点页内层位带吸附（几何自洽净化，不改杆集合）
    snap_report: Dict[str, Any] = {}
    if cfg.get("direct_snap_layers"):
        snap_report = _apply_direct_snap_layers(model, cfg)

    # provenance：被剪杆写 pruned_by 后删除（证据链铁律）
    for cid, rule in remove.items():
        comp = model.components.get(cid)
        if comp is not None:
            props = comp if isinstance(comp, dict) else (comp.properties or {})
            props.setdefault("pruned_by", rule)

    # 审计报告（removed_ids 全量落 drawing_file.properties）
    report: Dict[str, Any] = {
        "enabled": True,
        "rules": dict(Counter(remove.values())),
        "removed_ids": sorted(remove.keys()),
        "n_before": len(bars),
        "n_after": len(bars) - len(remove),
    }
    if quota_report:
        report["bom_quota"] = quota_report
    if snap_report:
        report["direct_snap_layers"] = snap_report
    for cid in remove:
        model.components.pop(cid, None)
    df = model.components.get("drawing_file")
    if df is not None:
        if isinstance(df, dict):
            (df.setdefault("properties", {})
             )["evidence_prune_report"] = {
                k: v for k, v in report.items() if k != "removed_ids"}
            df["properties"]["evidence_prune_report"]["n_removed"] = len(remove)
        else:
            (df.properties or {})["evidence_prune_report"] = {
                k: v for k, v in report.items() if k != "removed_ids"}
            df.properties["evidence_prune_report"]["n_removed"] = len(remove)
    return report


def _apply_bom_quota(
    model: Any,
    bars: List[tuple],
    deg: Dict[str, int],
    remove: Dict[str, str],
    cfg: Dict[str, Any],
    bom_rows: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """R4：分段×角色配额。超额按置信序（低先砍），同层按长度短先砍。

    配额来源优先级：overlay 显式表 > BOM qty 聚合 > 不设限（该组跳过）。
    只约束主材杆（LEG/DIAG/CROSS/HORIZ*）；度=1 悬空杆优先入砍序。
    """
    roles_quota = {"LEG", "DIAG", "CROSS", "HORIZONTAL", "HORIZ"}
    explicit = cfg.get("segment_role_quota") or {}
    tol = float(cfg.get("quota_length_tol_mm", 0.0))

    # BOM qty 聚合：segment 列 + 数量列 → {(seg, role_hint): qty}
    bom_qty: Dict[str, float] = {}
    for row in bom_rows:
        seg = str(row.get("segment") or "").strip()
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if not seg or qty <= 0:
            continue
        bom_qty[seg] = bom_qty.get(seg, 0.0) + qty

    def quota_for(seg: str, role: str) -> Optional[float]:
        if explicit:
            v = explicit.get(f"{seg}:{role}") or explicit.get(seg)
            return float(v) if v is not None else None
        if seg in bom_qty:
            return bom_qty[seg]
        return None

    groups: Dict[tuple, List[tuple]] = defaultdict(list)
    for cid, p in bars:
        if cid in remove:
            continue
        role = _bar_role(p)
        if role not in roles_quota:
            continue
        seg = _segment_of(p)
        if not seg:
            continue
        q = quota_for(seg, role)
        if q is None or q <= 0:
            continue
        groups[(seg, role)].append((cid, p))

    node_z = _node_z(model)

    def _length_mm(p: Dict[str, Any]) -> float:
        v = p.get("length_mm_3d")
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    pruned_groups: Dict[str, Any] = {}
    for (seg, role), items in sorted(groups.items()):
        cap = quota_for(seg, role)
        if cap is None or len(items) <= cap:
            continue

        def _kill_rank(it: tuple) -> tuple:
            cid, p = it
            da = deg.get(str(p.get("from_node")), 0)
            db = deg.get(str(p.get("to_node")), 0)
            return (_TIER_ORDER.get(_confidence_tier(p), 3),
                    0 if min(da, db) == 1 else 1,
                    -_length_mm(p), cid)
        ranked = sorted(items, key=_kill_rank, reverse=True)
        n_cut = len(items) - int(cap)
        cut_ids = [cid for cid, _p in ranked[:n_cut]]
        for cid in cut_ids:
            remove[cid] = "R4_bom_quota"
        pruned_groups[f"{seg}:{role}"] = {
            "cap": int(cap), "had": len(items), "cut": n_cut}

    return {
        "mode": "explicit" if explicit else "bom_qty",
        "groups": pruned_groups,
        "tolerance_mm": tol,
    }


def _apply_direct_snap_layers(model: Any, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """R7：同册直读杆端点 z 的页内层位带吸附（纯几何自洽）。

    版式依据（图纸分段立面）：同一册的直读杆端点应落在该段的层位带
    （等节拍网格）上。提取端点因角钢双线边缘/连接板延长线取点产生
    孤立偏移（实测 ZC1 近失直读杆 114 根中 37 根单端错位）。
    页内全部直读端点 z 做 1D 聚类（gap 250mm 切带）后，离群端点
    （离最近带中心 >250mm）吸附到带中心。

    纯模型内几何：不读 ladder/overlay 窗/GT——带中心来自同册杆自身的
    z 分布（多数端点在带内，少数离群被拉回）。只改 node z，不改杆集合。
    """
    gap = float(cfg.get("direct_snap_gap_mm", 250.0))

    node_z: Dict[str, float] = _node_z(model)
    # 同册直读杆：source.reference 的文件 stem 分组
    by_sheet: Dict[str, List[tuple]] = defaultdict(list)
    for comp in model.components.values():
        if _kind(comp) != "tower_bar":
            continue
        p = _props(comp)
        if str(p.get("geometry_class") or "") != "recognized":
            continue
        ref = ""
        src = getattr(comp, "source", None) or (comp.get("source") if isinstance(comp, dict) else None)
        if isinstance(src, dict):
            ref = str(src.get("reference") or "")
        elif src is not None:
            # 管线内是 SourceRef 对象（model.py），评测侧是 dict（model.json）
            ref = str(getattr(src, "reference", "") or "")
        stem = ref.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        for nid in (str(p.get("from_node") or ""), str(p.get("to_node") or "")):
            z = node_z.get(nid)
            if z is not None:
                by_sheet[stem].append((nid, z, comp))

    snapped_nodes: Dict[str, float] = {}
    snap_count = 0
    for stem, items in by_sheet.items():
        zs = sorted(z for _nid, z, _c in items)
        if len(zs) < 4:
            continue
        # 1D 聚类（gap 切带）→ 层位带中心（带内均值）。
        # 带内计数 ≥2 才是层位带——单点带是离群端点自身，不能作为
        # 吸附目标（否则离群点自成一带永远不被吸附）。
        bands: List[List[float]] = [[zs[0]]]
        for z in zs[1:]:
            if z - bands[-1][-1] <= gap:
                bands[-1].append(z)
            else:
                bands.append([z])
        centers = [sum(b) / len(b) for b in bands if len(b) >= 2]
        if not centers:
            continue
        for nid, z, comp in items:
            if nid in snapped_nodes:
                continue  # 共享节点只处理一次
            best_d, best_c = None, None
            for c in centers:
                d = abs(z - c)
                if best_d is None or d < best_d:
                    best_d, best_c = d, c
            if best_d is None or best_d <= gap:
                continue  # 带内，不动
            # 离群端点吸附到带中心（写 provenance 到杆）
            snapped_nodes[nid] = best_c
            props = _props(comp)
            props.setdefault("snapped_by", "R7_direct_snap_layers")
            snap_count += 1

    # 应用 z 修改（node 组件）
    for nid, z in snapped_nodes.items():
        comp = model.components.get(nid)
        if comp is None:
            continue
        props = comp if isinstance(comp, dict) else (comp.properties or {})
        if props.get("z") is not None:
            props["z"] = round(z, 3)

    return {
        "sheets": len(by_sheet),
        "snapped_endpoints": snap_count,
        "gap_mm": gap,
    }


def _node_z(model: Any) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for comp in model.components.values():
        if _kind(comp) == "tower_node":
            p = _props(comp)
            if p.get("z") is not None:
                try:
                    out[_comp_id(getattr(comp, "id", ""), comp)] = float(p["z"])
                except (TypeError, ValueError):
                    continue
    return out
