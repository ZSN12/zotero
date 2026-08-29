"""图纸件号（数字 105/108/...）↔ 计算模型件号（PM_XXXX）的映射。

打通「GT 拓扑（PM_XXXX，100% 召回）」与「图纸/BOM（数字件号）」之间的追溯链。
锚点：section（截面型号）+ length（下料长度 ≈ 几何长度，容差 60mm 吸收端部余量）。
一对多：一个 BOM 下料件号对应多根 GT 对称结构杆（同规格不同位置）。

注意：BOM 的 length_mm 是「下料长度」，GT 是「节点间几何长度」，两者相差
端部加工余量（实测 ±50mm 内）。主腿合并件（如 101-104 L70X5 5348mm 是塔头
多段短杆下料合并）无法从 GT 几何直接推导，标记为 unassigned 待人工补全。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


def _normalize_section(s: str) -> str:
    """剥离材质前缀 Q235/Q345/Q420，统一大写去空白。"""
    import re
    s = re.sub(r"\s+", "", s or "").upper()
    s = re.sub(r"^(?:Q\s?(?:235|345|420))", "", s)
    return s


def _gt_bars_by_sec_len(gt: Dict[str, Any]) -> Dict[Tuple[str, int], List[str]]:
    """GT 杆件按 (section, 几何长度) 分组，返回 {(sec, len): [bar_id...]}。"""
    nodes = gt.get("nodes", {})
    by_key: Dict[Tuple[str, int], List[str]] = defaultdict(list)
    for b in gt.get("bars", []):
        f = nodes.get(b.get("from"))
        t = nodes.get(b.get("to"))
        if f is None or t is None:
            continue
        L = math.sqrt(sum((t[i] - f[i]) ** 2 for i in range(3)))
        sec = _normalize_section(b.get("section", ""))
        by_key[(sec, round(L))].append(b.get("id", ""))
    return by_key


def _gt_leg_lines(gt: Dict[str, Any], section: str) -> Dict[str, List[str]]:
    """按象限分组 GT 主腿线（近垂直杆），返回 {quadrant: [bar_id...]}。

    主腿（近垂直 dz/L>0.85）按 (x符号, y符号) 分 4 象限，对应 4 条主腿线。
    用于映射 BOM 的 4 个主腿下料件号（如 101-104）。
    """
    nodes = gt.get("nodes", {})
    legs: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
    for b in gt.get("bars", []):
        if _normalize_section(b.get("section", "")) != section:
            continue
        f = nodes.get(b.get("from")); t = nodes.get(b.get("to"))
        if f is None or t is None:
            continue
        L = math.sqrt(sum((t[i] - f[i]) ** 2 for i in range(3)))
        dz = abs(t[2] - f[2]) / L if L > 0 else 0.0
        if dz <= 0.85:
            continue
        qx = "R" if f[0] > 0 else "L"
        qy = "F" if f[1] > 0 else "B"
        legs[f"{qx}{qy}"].append(((f[2] + t[2]) / 2, b.get("id", "")))
    out: Dict[str, List[str]] = {}
    for quad, segs in legs.items():
        segs.sort()
        out[quad] = [sid for _, sid in segs]
    return out


def build_bar_id_mapping(
    gt: Dict[str, Any],
    master_bom_rows: List[Dict[str, str]],
    *,
    length_tol_mm: float = 60.0,
) -> Dict[str, Any]:
    """建立 BOM 数字件号 → GT PM_XXXX 杆集合的一对多映射。

    参数：
        gt: GT JSON（含 nodes + bars，bar.id 为 PM_XXXX）
        master_bom_rows: master BOM 行列表（含 bar_id/section/length_mm/qty）
        length_tol_mm: 下料长度 vs 几何长度的最大允许偏差（默认 60mm）

    返回：
        {
          "mapping": {bom_bar_id: {"section", "bom_len", "gt_len", "gt_ids", "diff"}},
          "assigned": 已映射件号数,
          "unassigned": 未映射件号列表（含原因）,
          "total": 件号总数,
        }
    """
    by_key = _gt_bars_by_sec_len(gt)
    mapping: Dict[str, Any] = {}
    unassigned: List[Dict[str, Any]] = []

    # 预处理：识别「同 section 同下料长度的 qty=1 件号组」（主腿 4 件号组），
    # 统一映射到 4 条主腿线（确定性：按 BOM 顺序 1:1 分配象限）。
    leg_groups: Dict[Tuple[str, int], List[str]] = defaultdict(list)
    for row in master_bom_rows:
        bid = str(row.get("bar_id", "")).strip()
        if not bid:
            continue
        sec = _normalize_section(row.get("section", ""))
        if not sec.startswith("L"):
            continue
        try:
            bom_len = int(float(row.get("length_mm", 0) or 0))
        except (ValueError, TypeError):
            bom_len = 0
        if int(row.get("qty", 1) or 1) == 1:
            leg_groups[(sec, bom_len)].append(bid)
    leg_line_assign: Dict[str, str] = {}  # bom_bar_id -> quadrant
    for (sec, bom_len), bids in leg_groups.items():
        leg_lines = _gt_leg_lines(gt, sec)
        if len(leg_lines) == 4 and len(bids) == 4:
            quads = sorted(leg_lines.keys())
            for i, bid in enumerate(sorted(bids)):
                leg_line_assign[bid] = quads[i]

    for row in master_bom_rows:
        bid = str(row.get("bar_id", "")).strip()
        if not bid:
            continue
        sec = _normalize_section(row.get("section", ""))
        try:
            bom_len = int(float(row.get("length_mm", 0) or 0))
        except (ValueError, TypeError):
            bom_len = 0

        # 非角钢（钢板 -6X207 等）不映射 GT 结构杆（GT 只含角钢）
        if not sec.startswith("L"):
            unassigned.append({
                "bar_id": bid, "section": sec, "bom_len": bom_len,
                "reason": "非角钢截面（钢板/连接板），GT 结构杆不含",
            })
            continue

        # 同 section 长度差 <= tol 的候选
        cands = [
            (abs(gl - bom_len), gl, ids)
            for (s, gl), ids in by_key.items()
            if s == sec and abs(gl - bom_len) <= length_tol_mm
        ]
        if not cands:
            # 主腿合并件特殊处理：BOM 4 个同 (section, 下料长) 的 qty=1 件号，
            # 对应 GT 的 4 条主腿线（近垂直杆按象限分组）。下料长度是整条腿的
            # 分段总和，不等于任何单杆几何长度，需按象限整线匹配。
            if bid in leg_line_assign:
                quad = leg_line_assign[bid]
                leg_lines = _gt_leg_lines(gt, sec)
                ids = sorted(leg_lines.get(quad, []))
                mapping[bid] = {
                    "section": sec,
                    "bom_len": bom_len,
                    "gt_len": None,  # 整条主腿，无单一几何长度
                    "gt_ids": ids,
                    "leg_quad": quad,
                    "diff_mm": None,
                    "qty": int(row.get("qty", 1) or 1),
                    "note": "主腿合并件：下料长度=整条主腿分段总和，按象限 1:1 映射",
                }
                continue
            unassigned.append({
                "bar_id": bid, "section": sec, "bom_len": bom_len,
                "reason": "同截面无长度接近的 GT 杆（下料合并件或工艺余量超容差）",
            })
            continue
        diff, gl, ids = min(cands, key=lambda x: x[0])
        mapping[bid] = {
            "section": sec,
            "bom_len": bom_len,
            "gt_len": gl,
            "gt_ids": sorted(ids),
            "diff_mm": diff,
            "qty": int(row.get("qty", 1) or 1),
        }

    return {
        "mapping": mapping,
        "assigned": len(mapping),
        "unassigned": unassigned,
        "total": len(mapping) + len(unassigned),
    }


def mapping_to_bar_map(mapping_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """转成与 canonical.bar_map.json 兼容的 bar_map 列表。

    每项：{"bar_id": BOM 数字件号, "component_id": "gt_bar_<PM_XXXX>", "gt_id": PM_XXXX}
    """
    out: List[Dict[str, Any]] = []
    for bid, m in mapping_result.get("mapping", {}).items():
        for gid in m["gt_ids"]:
            out.append({
                "bar_id": bid,
                "component_id": f"gt_bar_{gid}",
                "gt_id": gid,
                "section": m["section"],
            })
    return out
