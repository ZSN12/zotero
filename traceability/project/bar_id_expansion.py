"""P4 件号同族扩绑（2026-09-08）。

背景（docs/P0_VERIFIED_DELIVERY_ATTRIBUTION.md §2.3）：BOM 有 28 件号
qty=4~8 而模型只有 1~4 根——图面件号标注只覆盖部分实例（一个件号装在
多个位置，图纸只在一处标号），几何杆大多已重建但缺 bar_id 绑定。
r_project_bom_master 把它们报成 under_identified。

    扩绑语义（诚实披露，非直读）：

    候选单元 = front 面无号物理杆（is_physical_bar）——独立杆为单杆，
    split 链（同 root_bar_id）为整链（长度用端到端 span，与
    r_bom_length_match 的链口径一致——把链内单独一段绑到短 BOM 件号
    会让链 span 超差，实测 CLE0010#s0 链 span 915.6 vs 121 BOM 320）。

    三道闸（继承 merge_view_bars P0-1b 先例）：
      * sheet 锚：BOM 行 sheet 已知且候选 source_file 已知且不同 → 排除
        （件号 X 册的杆画在 X 册图纸）；
      * 长度窗：单元长度与 BOM 长度偏差 ≤ length_tol（默认 3%）才配对，
        全局按偏差升序贪心分配（每单元只绑一件号）；
      * 截面闸：候选杆 section 已知且与 BOM section（归一化）不符 → 排除
        （同一物理杆不可能既 L40X3 又 L50X4——304 实证长度窗撞号）。
        无 section 的候选（front 无号杆 95% 缺失）由「件号有已绑实例」
        背书——有已绑实例才有同族锚；bound=0 的件号无任何截面/位置锚，
        纯长度巧合撞号风险高，不扩；
      * 数量闸：预算按**物理根数**（V3 口径）——单元写入后其 stem 家族
        （front + 跟随镜像）呈现的几何连通分量数即该单元贡献的物理根
        （塔身四棱对称杆四面 = 4 根，同棱共线四面 = 1 根），
        bound + Σ(单元根数) ≤ BOM qty 才收。按单元数预算会漏算镜像
        根数（101 实测 1 单元写入变 4 根 > qty=1）。

    写入字段：bar_id + bar_id_source="family_expansion" +
    bar_id_original（原 UNLABELED 值留档）。b/l/r 面同 stem 镜像孪生
    随 front 一同改写（同一物理杆的展开实例，不跟就是计数漏报）；
    无 front 对应的孤儿镜像不碰（无锚不猜）。

    不碰：已绑杆（含 bar_id_dup 非 primary——那是 intake 消歧的次实例）、
    sidegen l/r 孪生（V3 计数语义自管）、纯派生几何。

    报告落 drawing_file.properties.bar_id_expansion_report，逐件号
    bound_before/expanded_units/candidates 的可审计结构。
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ..model import Component, EngineeringModel
from .module_build import (
    _bar_geometry_key, _connected_component_count, _root_stem,
    physical_bar_counts,
)


def _normalize_section(s: str) -> str:
    """截面归一化（tower_validators 同式）：L100x8/L100×8/L100*8 → l100x8。"""
    return re.sub(r"[×*xX]", "x", (s or "").strip().lower().replace(" ", ""))


def _bar_sheet(props: Dict[str, Any]) -> str:
    return str(props.get("source_file") or props.get("drawing_view") or "")


def _chain_span_mm(
    model: EngineeringModel,
    segs: List[Tuple[str, Component]],
) -> Optional[float]:
    """链端到端 span（3D 节点坐标最远两点）——与
    tower_validators._chain_span_mm 同口径。节点缺失返回 None。"""
    pts: list = []
    for _cid, bar in segs:
        for end in ("from_node", "to_node"):
            nid = bar.properties.get(end)
            node = model.components.get(str(nid)) if nid else None
            if node is None:
                continue
            p = node.properties or {}
            try:
                pts.append((float(p["x"]), float(p["y"]), float(p["z"])))
            except (TypeError, KeyError):
                continue
    if not pts:
        return None
    best = 0.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = ((pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2
                 + (pts[i][2] - pts[j][2]) ** 2) ** 0.5
            if d > best:
                best = d
    return best


def _parse_bom_sheet_qty(detail: str) -> Tuple[str, int]:
    """dim source.detail 的 'sheet=...;qty=N' → (sheet, qty)。"""
    m_sheet = re.search(r"sheet=([^;]*)", detail)
    m_qty = re.search(r"qty=(\d+)", detail)
    return (m_sheet.group(1).strip() if m_sheet else "",
            int(m_qty.group(1)) if m_qty else 0)


def expand_family_bar_id_binding(
    model: EngineeringModel,
    *,
    length_tol: float = 0.03,
    target_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """同族几何杆按 BOM qty 扩绑件号（原地修改，返回审计报告）。

    target_ids：显式指定要扩绑的件号列表（默认全部 under 候件——
    BOM 维度存在且模型计数 < qty 的件号）。不读 GT、不读评测结果，
    只消费模型组件属性 + BOM 维度（dim_bom_length_* 的 value 与
    source.detail 元数据）。
    """
    from ..eval.metrics import is_physical_bar

    # --- BOM 维度表（长度 + sheet/qty 元数据，cross_check_bom 写入） ---
    bom_len: Dict[str, float] = {}
    bom_sheet: Dict[str, str] = {}
    bom_qty: Dict[str, int] = {}
    for did, dim in model.dimensions.items():
        if not did.startswith("dim_bom_length_"):
            continue
        bid = did[len("dim_bom_length_"):]
        if dim.value is None or float(dim.value) <= 0:
            continue
        bom_len[bid] = float(dim.value)
        det = (dim.source.detail or "") if dim.source else ""
        sh, qty = _parse_bom_sheet_qty(det)
        if sh:
            bom_sheet[bid] = sh
        if qty:
            bom_qty[bid] = qty
    if not bom_len:
        return {"enabled": True, "expanded_units": 0, "detail": {}}

    # --- 已绑物理根数（V3 口径）与目标件号 ---
    bound_counts = physical_bar_counts(model)
    # 件号门槛：qty≥2 才扩绑——单件图面标注即全部，「只标一处」前提不成立
    # （101/124/125/3901 实测全是 pbase/派生生成杆撞号）。
    # 另要求已绑 ≥1 根——已绑实例是同族锚（截面/位置背书）；bound=0 的
    # 件号图面一个字都没标，纯长度巧合撞号风险高，不猜。
    targets = list(target_ids) if target_ids else [
        bid for bid, L in bom_len.items()
        if bom_qty.get(bid, 0) >= 2
        and 0 < bound_counts.get(bid, 0) < bom_qty[bid]
    ]
    report_detail: Dict[str, Dict[str, Any]] = {}
    if not targets:
        return {"enabled": True, "expanded_units": 0, "detail": {}}

    # --- BOM 截面表（截面闸用） ---
    bom_sec: Dict[str, str] = {}
    for did, dim in model.dimensions.items():
        if did.startswith("dim_bom_section_") and dim.value is not None:
            bom_sec[did[len("dim_bom_section_"):]] = str(dim.value)

    # --- 候选单元：front 面无号物理杆，独立杆 / split 链 ---
    # stem 家族分组（front 候选 + b/l/r 镜像）——预算单元物理根数用：
    # 写入 front 后整个 stem 家族的几何连通分量数 = 该单元贡献的物理根
    # （塔身四棱对称杆四面 4 分量 = 4 根；同棱共线四面 1 分量 = 1 根）。
    unlabeled_by_stem: Dict[str, List[Component]] = defaultdict(list)
    chains: Dict[str, List[Tuple[str, Component]]] = defaultdict(list)
    solo: List[Tuple[str, Component]] = []
    for cid, comp in model.components.items():
        if comp.kind != "tower_bar":
            continue
        props = comp.properties or {}
        if not is_physical_bar(props):
            continue
        bid = str(props.get("bar_id") or "")
        if bid and not bid.startswith("UNLABELED"):
            continue
        unlabeled_by_stem[_root_stem(cid)].append(comp)
        if props.get("face") != "f":
            continue
        # intake 消歧的非 primary 次实例：贴线错挂，不作为扩绑锚
        if props.get("bar_id_dup") and props.get("bar_id_primary") is False:
            continue
        root = str(props.get("root_bar_id") or "")
        (chains[root] if root else solo).append((cid, comp))

    def _stem_phys_count(front_cid: str) -> int:
        """该 stem 的无号家族写入后的物理根数（几何连通分量）。"""
        stem = _root_stem(front_cid)
        geos = [g for g in (_bar_geometry_key(model, c)
                            for c in unlabeled_by_stem.get(stem, []))
                if g is not None]
        if not geos:
            # 几何不可判（节点缺失）：按 face 计数（V3 face_fallback 同语义）
            return len({str((c.properties or {}).get("face") or "")
                        for c in unlabeled_by_stem.get(stem, [])}) or 1
        return _connected_component_count(list(geos))

    units: List[Dict[str, Any]] = []
    for cid, comp in solo:
        L = comp.properties.get("length_mm_3d") or comp.properties.get("length_mm")
        if L is None or float(L) <= 0:
            continue
        units.append({
            "cids": [cid],
            "length": float(L),
            "sheet": _bar_sheet(comp.properties or {}),
            "phys": _stem_phys_count(cid),
        })
    for root, segs in chains.items():
        if not segs:
            continue
        span = _chain_span_mm(model, segs)
        if span is None:
            span = sum(
                float(s[1].properties.get("length_mm_3d")
                      or s[1].properties.get("length_mm") or 0.0)
                for s in segs)
        if span <= 0:
            continue
        units.append({
            "cids": [c for c, _ in segs],
            "length": span,
            "sheet": _bar_sheet(segs[0][1].properties or {}),
            "phys": _stem_phys_count(segs[0][0]),
        })
    if not units:
        return {"enabled": True, "expanded_units": 0, "detail": {}}

    # --- 全局贪心：偏差升序，每单元只绑一件号，物理根数预算截断 ---
    def _section_ok(unit_sections: List[str], bid: str) -> bool:
        """截面闸：候选截面已知且与 BOM 不符 → 排除。"""
        bs = bom_sec.get(bid)
        if not bs:
            return True
        for s in unit_sections:
            if s and _normalize_section(s) != _normalize_section(bs):
                return False
        return True

    unit_sections: List[List[str]] = [
        [str((model.components.get(c).properties or {}).get("section") or "")
         for c in u["cids"] if model.components.get(c) is not None]
        for u in units
    ]
    pairs: List[Tuple[float, int, str]] = []
    for bid in targets:
        bl = bom_len.get(bid)
        sh = bom_sheet.get(bid)
        if bl is None:
            continue
        for ui, unit in enumerate(units):
            if sh and unit["sheet"] and unit["sheet"] != sh:
                continue
            if not _section_ok(unit_sections[ui], bid):
                continue
            dev = abs(unit["length"] - bl) / bl
            if dev <= length_tol:
                pairs.append((dev, ui, bid))
    pairs.sort()

    alloc: Dict[str, List[int]] = defaultdict(list)
    used_units: set = set()
    budget_used: Dict[str, int] = defaultdict(int)
    for dev, ui, bid in pairs:
        if ui in used_units:
            continue
        deficit = bom_qty.get(bid, 0) - bound_counts.get(bid, 0)
        if budget_used[bid] + units[ui]["phys"] > deficit:
            continue
        alloc[bid].append(ui)
        used_units.add(ui)
        budget_used[bid] += units[ui]["phys"]

    # --- 写回：front + b/l/r 同 stem 镜像孪生 ---
    def _mirror_siblings(front_cid: str) -> List[str]:
        stem = _root_stem(front_cid)
        if not front_cid.startswith("4f_"):
            return []
        out = []
        for suf in ("_B", "_L", "_R"):
            sib = f"4f_{stem}{suf}"
            if sib in model.components and sib != front_cid:
                out.append(sib)
        return out

    expanded_units = 0
    for bid, uis in alloc.items():
        n_exp = 0
        cids_written: List[str] = []
        for ui in uis:
            unit = units[ui]
            touched = False
            for cid in unit["cids"]:
                comp = model.components.get(cid)
                if comp is None:
                    continue
                props = comp.properties
                props.setdefault("bar_id_original", props.get("bar_id"))
                props["bar_id"] = bid
                props["bar_id_source"] = "family_expansion"
                touched = True
                cids_written.append(cid)
                for sib in _mirror_siblings(cid):
                    scomp = model.components.get(sib)
                    if scomp is None:
                        continue
                    sprops = scomp.properties or {}
                    if str(sprops.get("bar_id") or "").startswith("UNLABELED") \
                            or not sprops.get("bar_id"):
                        sprops.setdefault("bar_id_original", sprops.get("bar_id"))
                        sprops["bar_id"] = bid
                        sprops["bar_id_source"] = "family_expansion"
                        sprops["bar_id_expansion_mirror"] = True
            if touched:
                n_exp += 1
        expanded_units += n_exp
        report_detail[bid] = {
            "bom_qty": bom_qty.get(bid, 0),
            "bound_before": bound_counts.get(bid, 0),
            "expanded_units": n_exp,
            "expanded_phys": budget_used.get(bid, 0),
            "candidates_in_window": sum(
                1 for dev, ui, b in pairs if b == bid),
            "assigned_cids": cids_written[:20],
        }

    report = {
        "enabled": True,
        "length_tol": length_tol,
        "n_targets": len(targets),
        "expanded_units": expanded_units,
        "detail": report_detail,
    }
    df = model.components.get("drawing_file")
    if df is not None:
        df.properties["bar_id_expansion_report"] = report
    return report
