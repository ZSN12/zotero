"""铁塔 DXF 解析（Phase 1）。

从铁塔施工图 DXF 中抽取：
    * 杆件中心线 (LINE/LWPOLYLINE on bar_layers) → tower_bar
    * 节点（端点聚类）                        → tower_node
    * 杆件编号（TEXT/MTEXT 空间关联）          → bar_id
    * 视图信息（布局/图层）                   → 每个节点的 view_type / 局部坐标

原则（沿用项目哲学）：
    * 每个对象必须有 SourceRef（handle + layer + coord + confidence）
    * 读不到的编号写 UNLABELED_{handle}，confidence=0.3，绝不猜
    * 图层 / 件号 / 视图区域规范从 schema/tower_layer_map.json 加载，
      生成器与解析器共用同一份规范。
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 完整 demo 生成器（16 节点 26 杆件，立面+平面+BOM）在 tower_demo_dxf.py
from .tower_demo_dxf import make_demo_tower_dxf  # noqa: F401  (复用完整版)
from .tower_spec import (
    layer_names,
    layer_names_for_stem,
    bar_id_patterns,
    view_regions,
    cross_file_infer_side_stems,
    assembly_split_min_gap_ratio,
    min_bar_length_mm,
    cluster_eps_mm,
    region_scale_ratio,
    region_scale_xy,
    double_line_merge_config,
    exact_overlap_dedup_tolerance,
    collinear_merge_config,
    canonical_sheet_role,
    sheet_role_for_stem,
    sheet_is_spatial_mergeable,
)

from ..model import (
    Component,
    Dimension,
    DimensionOrigin,
    EngineeringModel,
    SourceRef,
    SourceType,
)

# 兜底图层映射（schema/tower_layer_map.json 会覆盖）
DEFAULT_LAYER_MAP = {
    "bar_layers": ["TRUSS", "TRUSS_MAIN", "MEMBER", "LEG", "HORIZ", "DIAG",
                   "CROSS", "HEAD", "KNEE", "HANG", "杆件"],
    "node_layers": ["NODE", "TRUSS_NODE", "节点"],
    "dim_layers": ["DIM", "标注"],
    "text_layers": ["TEXT", "文字"],
}

# 端点聚类阈值（图纸单位，默认毫米）
EPS = 50.0
# 文本与杆件中点的最近距离阈值（图纸单位）
TEXT_SNAP = 400.0

# 杆件编号兜底正则（会与 schema 的 bar_id_patterns 合并）
# B5：国网件号常见纯数字、带杠、塔型前缀等形态。
DEFAULT_BAR_ID_PATTERNS = [
    r"M\d{4}",
    r"[GSB]\d{1,4}",
    r"[A-Za-z]{0,3}\d{1,5}",
    r"\d{1,5}",
]

# P1：件号候选排除正则（匹配到则整条文字不作为件号来源）。
# 分别排除：材质 Q235/Q345/Q420、角钢截面 L40X3/L100X7、
# 螺栓标注 M16X40 / 1M16X40 / 2M16X50 等（避免把尺寸/材质/螺栓当件号贴杆）；
# 排除前导负号/加号的切角/下料加工标注（如 -40, -55, +30）。
_BAR_ID_EXCLUDE_RES = [
    re.compile(r"Q\s?(?:235|345|420)", re.IGNORECASE),          # 材质
    re.compile(r"L\s?\d{1,3}\s*[Xx×*]\s*\d+", re.IGNORECASE),  # 截面
    re.compile(r"(?:\d+)?M\s?\d{1,3}\s*[Xx×*]\s*\d+", re.IGNORECASE),  # 螺栓
    re.compile(r"^[-+]\d+$"),                                   # 加工切角/下料长度偏移 (-40, -55 等)
    # 线1 verified delivery（2026-09-03）：标高/带单位长度文字。实测 JC1-07
    # 「+4.5m」被 \d{1,5} 搜出「4」贴成件号——52 根杆挂假件号「4」、
    # 「1」「2」「3」同类（标高 +1.5m/+2m 片段），直接污染
    # r_no_duplicate_bar_id（181 组重复里的大头）与 A1 件号证据。
    re.compile(r"^[+-]?\d+(?:\.\d+)?\s*[mM]$"),                # 标高 +4.5m / 4.5m / -3.1m
]

# P2：截面型号提取正则（Phase 2 填充杆件 section）。
# 国网截面标注形态：
#   * 角钢：L40X3 / L50X4 / L100X7（可带材质前缀 Q345L63X5 / Q345L70X5）
#   * 连接板钢板：-6X101 / Q345-6X188 / -10X110 / -14X260（厚 X 宽）
# 只提取「型号本体」，材质前缀（Q345 等）一并保留用于与 master BOM 精确对账。
_SECTION_RE = re.compile(
    r"(?:(?:Q\s?(?:235|345|420))\s*)?"
    r"(?:L\s?\d{1,3}\s*[Xx×*]\s*\d{1,3}|-\s?\d{1,2}\s*[Xx×*]\s*\d{1,4})",
    re.IGNORECASE,
)


def classify_drawing_kind(stem: str) -> dict:
    """按文件名规则分流国网/外图图纸类型（B2）。

    返回：
        * kind: title_block / bom / assembly / node_detail / drawing
        * role: 规范 sheet_role 枚举值（Phase A1：elevation|plan|section|
          module_panel|node_detail|index|title）；文件名推不出空间角色时为空。
        * parse_bars: 是否进入杆件解析
        * reason: 分流依据
    """
    s = stem.lower()
    # 国网命名习惯：<塔型>-<序号>[-<分页>]，如 35a1-jc1-00-1 / 35a1-jc1-02 / 35c2-sjg1-ml
    if re.search(r"[-_]0{2}(?:[-_.]|$)", s) or "图签" in s:
        return {"kind": "title_block", "role": "title", "parse_bars": False,
                "reason": "文件名 -00-* 判定为图签页"}
    if s.endswith("-ml") or s.endswith("_ml") or s == "ml" or "-ml-" in s or "-ml." in s:
        return {"kind": "bom", "role": "index", "parse_bars": False,
                "reason": "文件名 *-ML 判定为材料明细表"}
    # 02 总装、03+ 节点大样都属于可解析的杆件图
    if re.search(r"[-_]0?2(?:[-_.]|$)", s):
        return {"kind": "assembly", "role": "module_panel", "parse_bars": True,
                "reason": "文件名 -02 判定为总装图"}
    if re.search(r"[-_]0?[3-9]\d*(?:[-_.]|$)", s):
        return {"kind": "node_detail", "role": "node_detail", "parse_bars": True,
                "reason": "文件名 03+ 判定为节点/分段图"}
    if s.startswith("00") or s.startswith("02"):
        return {"kind": "assembly" if s.startswith("02") else "title_block",
                "role": "module_panel" if s.startswith("02") else "title",
                "parse_bars": s.startswith("02"),
                "reason": "文件名前导序号判定"}
    return {"kind": "drawing", "role": "node_detail", "parse_bars": True,
            "reason": "默认按杆件图解析（无视图声明时按节点大样处理）"}


def resolve_drawing_kind(stem: str, overlay: Optional[str | Path | dict] = None) -> dict:
    """按文件名分流，并允许 overlay view_regions 覆盖 BOM/图签等跳过规则（M3）。

    Phase A1：返回里带规范 sheet_role；overlay 声明了带 axes 的正交视图时，
    role 以 overlay 为准（elevation/plan/section），不再猜「总装/大样」。
    """
    kind = classify_drawing_kind(stem)
    if kind["parse_bars"]:
        role = sheet_role_for_stem(stem, overlay=overlay) if overlay else None
        if role:
            kind["role"] = role
        return kind
    for region in view_regions(stem, overlay=overlay):
        axes = list(region.get("axes") or [])
        if not axes:
            continue
        vk = region.get("kind", "drawing")
        role = sheet_role_for_stem(stem, overlay=overlay) or canonical_sheet_role(vk)
        return {
            "kind": vk if vk in ("plan", "front", "side", "section", "elevation", "drawing") else "drawing",
            "role": role,
            "parse_bars": True,
            "reason": f"overlay view_regions[{vk}] 覆盖 {kind['reason']}",
        }
    return kind


def _layer_hit(layer: str, names: List[str]) -> bool:
    """图层名是否命中映射列表（不区分大小写/空白）。

    先做精确匹配（处理国网数字图层 0/1/2/3...），再回退到子串匹配
    （兼容自画图 TRUSS / TRUSS_MAIN 等命名）。
    纯数字图层名（如 "8"）只参与精确匹配，避免把
    "$TD_AUDIT_GENERATED_(886)" 这类审计层误判成命中 "8"。
    """
    norm = layer.strip().lower()
    cleaned = [n.strip().lower() for n in names if n and n.strip()]
    exact = {n for n in cleaned}
    if norm in exact:
        return True
    # 子串匹配只用非纯数字图层名
    fuzzy = [n for n in cleaned if not n.isdigit()]
    return any(n in norm for n in fuzzy)


def _side_extra_bar_layers_cfg(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
) -> Optional[Tuple[List[str], float, Dict]]:
    """02 侧视专项：读取 overlay 的 side_extra_bar_layers 配置。

    返回 (补充图层列表, 最小物理长度 mm, side region 几何 dict) 或
    None（未声明/本 stem 无 side region）。region dict 需含
    x0/x1/y0/y1/origin_x/origin_y/scale_y——由 view_regions 求得，
    未声明 origin 的用 region 边界推（x0/y0 为原点兜底）。
    """
    from .tower_spec import load_tower_spec
    ov = load_tower_spec(overlay) if overlay else None
    if not isinstance(ov, dict):
        return None
    extra = ov.get("side_extra_bar_layers")
    if not isinstance(extra, dict):
        return None
    layers = [str(x).strip() for x in (extra.get("layers") or []) if str(x).strip()]
    if not layers:
        return None
    min_mm = float(extra.get("min_len_mm") or 100.0)
    stems = extra.get("stems") or None
    if stems is not None:
        stems = {str(s) for s in stems}
        if stem not in stems:
            return None
    sreg = None
    for r in view_regions(stem, overlay=overlay):
        if str(r.get("kind") or "").lower() == "side":
            sreg = r
            break
    if sreg is None:
        return None
    xs = [float(v) for v in (sreg.get("region") or [0, 0, 0, 0])[:2]] or [0.0, 0.0]
    ys = [float(v) for v in (sreg.get("region") or [0, 0, 0, 0])[2:4]] or [0.0, 0.0]
    geom = {
        "x0": min(xs), "x1": max(xs),
        "y0": min(ys), "y1": max(ys),
        "origin_x": float(sreg.get("origin", [0, 0])[0] if sreg.get("origin") else min(xs)),
        "origin_y": float(sreg.get("origin", [0, 0])[1] if sreg.get("origin") else min(ys)),
        "scale_y": float(sreg.get("scale_y") or sreg.get("scale") or 20.0),
    }
    return layers, min_mm, geom


def _flatten_modelspace_entities(msp) -> List:
    """展开模型空间实体：INSERT 递归展开为 LINE/LWPOLYLINE/TEXT 等虚拟实体。

    B3：国网图大量用块（INSERT）；块内 LINE/LWPOLYLINE/TEXT 也必须进
    tower_bar / bar_id 关联，不能只读最外层实体。
    """
    out: List = []
    for e in msp:
        if e.dxftype() == "INSERT":
            try:
                for v in e.virtual_entities():
                    out.append(v)
            except Exception:
                # 无法展开的 INSERT 原样保留，绝不静默丢弃
                out.append(e)
        else:
            out.append(e)
    return out


def _dimension_value(e) -> Tuple[Any, str]:
    """读取 DIMENSION 实体的实测尺寸（B4）。

    优先用图面文字（国网图常人为覆盖 text，如 5800），
    否则用 get_measurement() 的自动测量值。
    返回 (value, unit)。
    """
    text = None
    try:
        text = getattr(e.dxf, "text", None)
    except Exception:
        text = None
    if text is not None and str(text).strip():
        return str(text).strip(), "mm"
    try:
        return round(float(e.get_measurement()), 3), "unit"
    except Exception:
        return None, "unit"


def _dist(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


# ----------------------------------------------------------------------------
# P2.1 DIMENSION 节拍锚定（坐标链校准，2026-09-02）
# ----------------------------------------------------------------------------

def _collect_vertical_beat_dimensions(
    msp,
    region: dict,
    *,
    beat_min_mm: float = 350.0,
    beat_max_mm: float = 800.0,
    match_tol: float = 0.08,
) -> List[Tuple[float, float, float, float]]:
    """收集竖向主节拍 DIMENSION（面板高标注）。

    返回 [(y_lo, y_hi, value_mm, x_mid)]（图纸单位 y）。
    筛选：type=0 线性标注、竖向（y 跨度 > x 跨度）、数值文本落在
    [beat_min_mm, beat_max_mm]、实测跨度与文本值一致（±match_tol）——
    即「面板高度」标注（400/430/444/450 等），排除总高/横杆/杆件
    细部标注（5000/2500/95/105 等）。
    """
    out: List[Tuple[float, float, float, float]] = []
    for e in msp.query("DIMENSION"):
        try:
            if int(e.dimtype) != 0:
                continue
            text = str(getattr(e.dxf, "text", "") or "").strip()
            try:
                v = float(text)
            except (TypeError, ValueError):
                continue
            if not (beat_min_mm <= v <= beat_max_mm):
                continue
            p1 = e.dxf.defpoint
            p2 = e.dxf.defpoint2
            x1, y1 = float(p1[0]), float(p1[1])
            x2, y2 = float(p2[0]), float(p2[1])
            if abs(y2 - y1) <= abs(x2 - x1):
                continue
            scale_y = float(region.get("scale_y") or 20.0)
            span_mm = abs(y2 - y1) * scale_y
            if span_mm <= 0 or abs(span_mm - v) / v > match_tol:
                continue
            out.append((min(y1, y2), max(y1, y2), v, (x1 + x2) / 2.0))
        except Exception:
            continue
    return out


def _chain_beat_dimensions(
    dims: List[Tuple[float, float, float, float]],
    *,
    link_tol_u: float = 1.5,
) -> List[Tuple[float, float, float, float]]:
    """把散的面板高 DIMENSION 链成「底→顶」连续节拍序列。

    链规则：按 y 升序（图纸 y 小=顶部），从最底（最小 y_lo？——注意
    CAD y 向下：y 绝对值大=图纸下方=塔底）……实际以边界共享为链：
    下一节拍的 y_lo ≈ 上一节拍的 y_hi（±link_tol_u）。从最底节拍
    （y 最小，即图纸最下）开始逐级向上。返回链序（底→顶）。
    """
    if not dims:
        return []
    # CAD y 向下：图纸下方 y 值更小（更负）。塔底 = y 最小。
    # 链方向：从塔底（y 最小）向上（y 增大）。
    ordered = sorted(dims, key=lambda d: d[0])  # 按 y_lo 升序（底→顶）
    chain = [ordered[0]]
    used = {0}
    cur_edge = ordered[0][1]  # 该节拍的上边界 y_hi
    while True:
        nxt = None
        for i, (lo, hi, _v, _x) in enumerate(ordered):
            if i in used:
                continue
            if abs(lo - cur_edge) < link_tol_u:
                nxt = i
                break
        if nxt is None:
            break
        chain.append(ordered[nxt])
        used.add(nxt)
        cur_edge = ordered[nxt][1]
    return chain


def dimension_beat_anchors(
    msp,
    region: dict,
    z_base_mm: float,
    *,
    beat_min_mm: float = 350.0,
    beat_max_mm: float = 800.0,
    mode: str = "beats",
    z_span_mm: Optional[Tuple[float, float]] = None,
) -> Optional[Dict[str, Any]]:
    """P2.1：DIMENSION 主节拍链 → view_y 域锚点（坐标链证据标定）。

    返回 {"vy": [...], "z": [...], "y_draw": [...], "n_beats": n, "z_top": z}
    （vy = (y_draw − region.origin_y) × scale_y，与 tower_dxf 节点 view_y
    同域，供 tower_views 分段线性映射直接使用）；无法构建链（<3 节拍）
    返回 None（调用方回退分位数归一化）。

    依据：06 册实测——节点 view_y 分位跨度 5544mm ≠ 模块高 5030mm，
    分位数线性归一化在段底/段顶各偏 ~300-550mm；DIMENSION 节拍链
    （400×4+450+400×3+430+450×2+444=5024mm）与 GT 层位吻合到
    50-115mm（14051↔14000、14447↔14500、16115↔16000）。
    离线验证：TP@500 从 0（线性）→ 19（节拍分段）。

    P2.4a（2026-09-04）mode="region_span_linear"：不信任 DIMENSION 节拍
    累加，改用「视图区域 y 跨度 ↔ z_span_mm 线性映射」两点锚链。
    背景：05 册节拍链 z 赋值系统性错误——链仅覆盖图纸内容 81%（底缺
    621mm/顶缺 662mm），节拍斜率 1.0 vs 内容真实斜率 0.929，顶端累计
    +912mm 漂移（dxf_geom 斜杆端点 cost 1302 vs 线性 176 实证）。
    两点锚链保持 beam_marker_levels_mm 反解（_y_of_z）与 leg_synth
    机制兼容（z 一切来自层位/锚点表，y 一切来自图纸），画线几何回归
    线性映射。
    """
    oy = float(region["origin"][1])
    scale_y = float(region.get("scale_y") or 20.0)
    if mode == "region_span_linear":
        if not z_span_mm:
            return None
        rx = region.get("region") or []
        if len(rx) < 4:
            return None
        y_lo, y_hi = float(rx[2]), float(rx[3])
        if y_hi - y_lo < 1e-6:
            return None
        z_lo, z_hi = float(z_span_mm[0]), float(z_span_mm[1])
        return {
            "vy": [round((y_lo - oy) * scale_y, 2),
                   round((y_hi - oy) * scale_y, 2)],
            "z": [round(z_lo, 1), round(z_hi, 1)],
            "y_draw": [round(y_lo, 2), round(y_hi, 2)],
            "n_beats": 0,
            "z_top": round(z_hi, 1),
            "source": "region_span_linear",
        }
    dims = _collect_vertical_beat_dimensions(
        msp, region, beat_min_mm=beat_min_mm, beat_max_mm=beat_max_mm)
    if len(dims) < 3:
        return None
    # 按 DIMENSION x 中点分两簇（左右视图），取链最长的一簇
    xs = sorted(d[3] for d in dims)
    x_mid_gap = 0.0
    x_split = None
    for i in range(1, len(xs)):
        gap = xs[i] - xs[i - 1]
        if gap > x_mid_gap:
            x_mid_gap = gap
            x_split = (xs[i] + xs[i - 1]) / 2.0
    if x_split is not None and x_mid_gap > 50.0:
        left = [d for d in dims if d[3] <= x_split]
        right = [d for d in dims if d[3] > x_split]
        cl = _chain_beat_dimensions(left)
        cr = _chain_beat_dimensions(right)
        chain = cl if len(cl) >= len(cr) else cr
    else:
        chain = _chain_beat_dimensions(dims)
    if len(chain) < 3:
        return None
    # 锚点（drawing y → 全局 z）：链底 → z_base，逐节拍累加
    oy = float(region["origin"][1])
    scale_y = float(region.get("scale_y") or 20.0)
    y_draws: List[float] = [chain[0][0]]
    zs: List[float] = [float(z_base_mm)]
    zc = float(z_base_mm)
    for lo, hi, v, _x in chain:
        zc += v
        y_draws.append(hi)
        zs.append(zc)
    # view_y 域（与 tower_dxf 节点 ly 同域：ly = (y − oy) × scale_y）
    vys = [round((y - oy) * scale_y, 2) for y in y_draws]
    return {
        "vy": vys,
        "z": [round(z, 1) for z in zs],
        "y_draw": [round(y, 2) for y in y_draws],
        "n_beats": len(chain),
        "z_top": round(zc, 1),
        "source": "dxf_dimension_beats",
    }


def _merge_double_line_segments(raw_segments: List[Dict], cfg: Optional[dict]) -> List[Dict]:
    """P0-1：把「同一构件画成两条近似平行线」合并为一条中心线。

    国网角钢构件常用 layer1/layer4 各画一肢（两线中点偏移约 0.5~3 图纸单位），
    不合并会造成双线碎片 + self-loop。配对条件（全部满足）：
        * 方向角差 <= max_angle_rad（含 pi 翻转）
        * 中点距离 < max_offset_units
        * 长度差 <= max_length_diff_ratio * max(len)
        * 两线长度 >= min_length_units
    贪心配对，每条线最多配一次；配对后线段取两线端点均值，layer/handle 保留。
    """
    if not cfg or not raw_segments:
        return raw_segments
    max_off = float(cfg.get("max_offset_units", 3.0))
    max_len_ratio = float(cfg.get("max_length_diff_ratio", 0.25))
    max_ang = float(cfg.get("max_angle_rad", 0.25))
    min_len = float(cfg.get("min_length_units", 3.0))

    def _ang(seg):
        dx = seg["end"][0] - seg["start"][0]
        dy = seg["end"][1] - seg["start"][1]
        return math.atan2(dy, dx)

    def _mid(seg):
        return ((seg["start"][0] + seg["end"][0]) / 2,
                (seg["start"][1] + seg["end"][1]) / 2)

    segs = list(raw_segments)
    used = [False] * len(segs)
    merged: List[Dict] = []
    for i, a in enumerate(segs):
        if used[i]:
            continue
        # P1.2：marker_synth / leg_synth 合成杆是单线终态（非双线对），
        # 跳过双线配对——同层重叠段曾在此被误配对吞掉 95/195 段且属性丢失。
        if str(a.get("layer") or "") in ("marker_synth", "leg_synth", "diag_synth"):
            merged.append(a)
            continue
        la = _dist(a["start"], a["end"])
        if la < min_len:
            merged.append(a)
            continue
        aa = _ang(a)
        best_j, best_d = None, max_off
        for j in range(i + 1, len(segs)):
            if used[j]:
                continue
            b = segs[j]
            # P1.2：synth 段不做任何双线配对（单线终态）
            if str(b.get("layer") or "") in ("marker_synth", "leg_synth", "diag_synth"):
                continue
            lb = _dist(b["start"], b["end"])
            if lb < min_len:
                continue
            if abs(la - lb) / max(la, lb) > max_len_ratio:
                continue
            da = abs(aa - _ang(b))
            if da > max_ang and abs(da - math.pi) > max_ang:
                continue
            d = _dist(_mid(a), _mid(b))
            if d < best_d:
                best_d, best_j = d, j
        if best_j is not None:
            b = segs[best_j]
            used[best_j] = True
            merged.append({
                "handle": a["handle"],
                "start": ((a["start"][0] + b["start"][0]) / 2,
                          (a["start"][1] + b["start"][1]) / 2),
                "end": ((a["end"][0] + b["end"][0]) / 2,
                        (a["end"][1] + b["end"][1]) / 2),
                "layer": a["layer"],
            })
        else:
            merged.append(a)
    return merged


def _dedup_exact_overlap_segments(
    raw_segments: List[Dict],
    tol_units: float,
) -> List[Dict]:
    """P3.3：精确重合线去重（LINE + LWPOLYLINE 重复绘制同一根杆）。

    背景（35A1-JC1-05 实测）：同一图元在 DXF 里画了两遍——一次 LINE、
    一次 LWPOLYLINE，端点坐标差 <1 图纸单位（d≈0.00~0.5），提取器各提
    一根 = 完全重合的双杆。与 double_line_merge 的「角钢两肢中心线合并」
    不同：本规则只删**端点近似完全重合**的复制线，不碰任何近平行近距的
    真实构件（05 图 X 交叉对中点距离可达 <1 单位，double_line_merge 任何
    offset 参数都会误伤，实测 TP@500 211→208/194）。

    判据（全部满足才算复制对）：
        * 两端点距离之和 < 2 * tol_units（正序或反序端点对应）
        * 保留先出现者（handle 链序），删除后出现者
    """
    if not raw_segments:
        return raw_segments
    # P3-7（2026-09-04）：空间网格索引替代全量对比——旧 O(n²) 双循环在
    # 万级杆件图纸下可感知（每段与全部已保留段比端点）。重合对必然
    # 端点近距，按端点网格分桶（桶宽 = 2*tol）后只在桶邻域比对；
    # 判定逻辑与旧版逐字一致（d1/d2 判据不变，保留先出现者）。
    _cell = max(2.0 * tol_units, 1e-9)
    grid: Dict[Tuple[int, int], List[int]] = {}
    kept: List[Dict] = []

    def _bucket(p) -> Tuple[int, int]:
        return (math.floor(p[0] / _cell), math.floor(p[1] / _cell))

    for seg in raw_segments:
        b1, b2 = _bucket(seg["start"]), _bucket(seg["end"])
        dup = False
        # 候选只可能在「起点桶或终点桶 3x3 邻域」命中的已保留段里
        cand_idx: set = set()
        for (bx, by) in (b1, b2):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cand_idx.update(grid.get((bx + dx, by + dy), ()))
        for i in cand_idx:
            k = kept[i]
            d1 = _dist(seg["start"], k["start"]) + _dist(seg["end"], k["end"])
            d2 = _dist(seg["start"], k["end"]) + _dist(seg["end"], k["start"])
            if min(d1, d2) < 2.0 * tol_units:
                dup = True
                break
        if not dup:
            idx = len(kept)
            kept.append(seg)
            for b in (b1, b2):
                grid.setdefault(b, []).append(idx)
    return kept



def _merge_collinear_fragments(
    raw_segments: List[Dict],
    colinear_tol: float = 2.0,
    gap_tol: float = 8.0,
) -> List[Dict]:
    """P1 共线碎段合并：把同一物理杆件被拆成的小段拼成整根。

    真实国网图（如 35A1-JC1-02）把一根角钢画成大量短碎段（median 长度 1 单位），
    必须按「同向 + 共线 + 端点相接/相邻」合并，否则 158 根杆里 150 根是 <100mm
    的碎片（大量 self-loop），件号被贴 10 次，Precision/Recall 全失真。

    算法（贪心链式合并）：
        * 方向角差 <= ~8°（含 pi 翻转）视为同向
        * 点到对方所在直线的垂直距离 <= colinear_tol 视为共线
        * 端点沿方向投影间距 <= gap_tol 视为可拼接（相接/近邻/少量重叠）
    合并后线段取整条链的端到端 span，handles 保留为列表（可审计来源）。

    不做相交切分（那是拓扑层 P2 的事）；本函数只拼「本属同一杆」的碎段。
    """
    def _ang(s):
        dx = s["end"][0] - s["start"][0]
        dy = s["end"][1] - s["start"][1]
        return math.atan2(dy, dx)

    def _span(s):
        x1, y1 = s["start"]
        x2, y2 = s["end"]
        return math.hypot(x2 - x1, y2 - y1)

    segs = list(raw_segments)
    merged: List[Dict] = []
    used = [False] * len(segs)
    ang_tol = math.radians(8.0)

    def _region_key(s) -> object:
        # region 身份：bbox + origin + kind 三元组；None 表示未分区（旧路径兼容）。
        r = s.get("region")
        if not isinstance(r, dict):
            return None
        return (
            tuple(r.get("region") or []),
            tuple(r.get("origin") or []),
            r.get("kind"),
        )

    # P1.2：marker_synth 合成横杆是「相邻分段」终态（leg↔inner↔center
    # 断点对），共线合并会把同层相邻段熔成一根通长杆（gap 0 <
    # gap_tol 30）——GT 横杆拓扑是分段式（[0,891]+[891,1782]），通长
    # 杆端点对不上分段 GT。预标记 used 使 synth 段既不做链种子、也不
    # 被后续链吸收（链内吸收会丢分段属性并改变端点）。
    _synth_is = [k for k, s in enumerate(segs)
                 if str(s.get("layer") or "") in ("marker_synth", "leg_synth", "diag_synth")]
    for k in _synth_is:
        used[k] = True
    merged.extend(segs[k] for k in _synth_is)

    # P3-6：折叠链碎段回炉池（见下方 _fold 检出注释）
    _retry_pool: List[Dict] = []

    for i in range(len(segs)):
        if used[i]:
            continue
        # 以 i 为起点做链式延伸
        chain = [segs[i]]
        used[i] = True
        seed_region = _region_key(segs[i])
        grew = True
        while grew:
            grew = False
            base = chain[-1]
            ba = _ang(base)
            # 用整条链的方向（首尾连线）做主轴，避免逐段漂移
            ax = base["end"][0] - base["start"][0]
            ay = base["end"][1] - base["start"][1]
            bl = math.hypot(ax, ay)
            if bl <= 0:
                break
            ux, uy = ax / bl, ay / bl  # 主轴单位向量
            best_j, best_dist = None, gap_tol
            for j in range(len(segs)):
                if used[j]:
                    continue
                cand = segs[j]
                # 阶段3 修复（C1）：共线链不得跨 region 延伸。国网 side 视图的塔身
                # 与材料表同属一个 view_type，但空间分离；若不约束 region，链会从
                # 塔身段「走」进材料表线簇（N154 案例：走 60u 进材料表，泄漏原始
                # 图纸 x=34701 污染半宽）。仅当两端都带 region 时约束，None 回退旧行为。
                if seed_region is not None and _region_key(cand) != seed_region:
                    continue
                da = abs(_ang(cand) - ba)
                if da > ang_tol and abs(da - math.pi) > ang_tol:
                    continue
                # 候选起点到主轴直线的垂直距离
                cx, cy = cand["start"]
                perp = abs((cx - base["start"][0]) * uy - (cy - base["start"][1]) * ux)
                if perp > colinear_tol:
                    continue
                # 候选沿主轴投影，取离当前端点最近的一端
                proj_cur = (base["end"][0] - base["start"][0]) * ux + (base["end"][1] - base["start"][1]) * uy
                proj_c_start = (cand["start"][0] - base["start"][0]) * ux + (cand["start"][1] - base["start"][1]) * uy
                proj_c_end = (cand["end"][0] - base["start"][0]) * ux + (cand["end"][1] - base["start"][1]) * uy
                gap = min(abs(proj_c_start - proj_cur), abs(proj_c_end - proj_cur))
                if gap < best_dist:
                    best_dist, best_j = gap, j
            if best_j is not None:
                chain.append(segs[best_j])
                used[best_j] = True
                grew = True

        if len(chain) == 1:
            merged.append(chain[0])
            continue
        # 整条链的 span：所有端点在主轴方向上的投影极值
        pts = [p for s in chain for p in (s["start"], s["end"])]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        # 主轴方向投影极值
        origin = chain[0]["start"]
        ux, uy = None, None
        ax = chain[-1]["end"][0] - origin[0]
        ay = chain[-1]["end"][1] - origin[1]
        if math.hypot(ax, ay) > 0:
            ux, uy = ax / math.hypot(ax, ay), ay / math.hypot(ax, ay)
        if ux is None:
            merged.append(chain[0])
            continue
        projs = [(p[0] - origin[0]) * ux + (p[1] - origin[1]) * uy for p in pts]
        t0, t1 = min(projs), max(projs)
        start = (origin[0] + ux * t0, origin[1] + uy * t0)
        # P3-6（2026-09-04）：折叠链检出 → 严格重拼。
        # 贪心链吸收只校验候选与「当前链尾轴」的关系，X 交叉斜材族
        # 的分支可在交点处贴轴入链，随后把链端点拖向两侧——链 span
        # 塌缩（02 册 side 实测 14~18 段折叠链 span 61/51/36u，吃掉
        # 6 根 860mm 斜杆画线）。链终态自校验：任一碎段端点到最终
        # span 线垂距超 colinear_tol*2 → 判折叠。折叠链的碎段回炉
        # 池，用「两端都在轴上」的严格共线规则重拼——真共线碎段仍
        # 焊回整线，X 交叉分支各自归位。只作用于 side（损伤区），
        # front 链行为不变（全局版实测 front Hungarian 重排打穿
        # dual-recon 99.5% 红线）。
        _fold = False
        # 前提：调用方（tower_dxf.py 阶段2 按视图分组）保证本函数输入
        # 全部同 view_type，故查链首即可判定 side；若未来出现混合输入
        # 调用方，需改为 any(s.get("view_type")=="side" for s in chain)。
        if len(chain) > 1 and str((chain[0].get("view_type") or "")) == "side":
            for s in chain:
                if (abs((s["start"][0] - start[0]) * uy - (s["start"][1] - start[1]) * ux) > colinear_tol * 2
                        or abs((s["end"][0] - start[0]) * uy - (s["end"][1] - start[1]) * ux) > colinear_tol * 2):
                    _fold = True
                    break
        if _fold:
            # 折叠链：原链保留（其 span 意外匹配的 GT 不丢——v5 实测
            # 拆链丢 10 TP 换 7 TP 得不偿失），碎段另进回炉池，用严格
            # 共线规则重拼出真实画线作为**追加**段。两个证据形态并存，
            # Hungarian 各取所需。folded_chain 仅为审计元数据（区分
            # 折叠链 span 与正常链），无下游消费。
            _retry_pool.extend(dict(s, _retry_from=chain[0].get("handle"))
                               for s in chain)
            end = (origin[0] + ux * t1, origin[1] + uy * t1)
            merged.append({
                "handle": chain[0]["handle"],  # 保 str 兼容下游 handle 索引
                "start": start,
                "end": end,
                "layer": chain[0]["layer"],
                "fragments": len(chain),
                "fragments_handles": [s["handle"] for s in chain],
                "folded_chain": True,
            })
            for _ek in ("geometry_origin", "source_extractor", "geometry_class",
                        "evidence_status", "view_type", "scale_ratio"):
                if chain[0].get(_ek) is not None:
                    merged[-1][_ek] = chain[0][_ek]
            if chain[0].get("region") is not None:
                merged[-1]["region"] = chain[0]["region"]
            continue
        end = (origin[0] + ux * t1, origin[1] + uy * t1)
        merged.append({
            "handle": chain[0]["handle"],  # 保 str 兼容下游 handle 索引
            "start": start,
            "end": end,
            "layer": chain[0]["layer"],
            "fragments": len(chain),
            "fragments_handles": [s["handle"] for s in chain],
        })
        # P1.2 证据属性透传：合并链的 origin/extractor 等从链首继承
        # （此前全部丢弃——marker_synth 合成横杆合并后 origin 丢失，杆件
        # 创建回退 dxf_geom、se=None，pure 口径证据链断裂）。
        for _ek in ("geometry_origin", "source_extractor", "geometry_class",
                    "evidence_status", "view_type", "scale_ratio"):
            if chain[0].get(_ek) is not None:
                merged[-1][_ek] = chain[0][_ek]
        if chain[0].get("region") is not None:
            merged[-1]["region"] = chain[0]["region"]

    # P3-6：折叠链碎段严格重拼。回炉池里的碎段属于多条真实画线（X
    # 交叉族），用「候选两端都在链轴 ±colinear_tol 内」的严格规则
    # 重新链式合并：过轴即分叉的分支不再入链，真共线碎段仍焊回整线。
    # 重拼结果的属性透传与主路径一致（链首继承）。
    if _retry_pool:
        _rp = sorted(_retry_pool, key=lambda s: -_span(s))
        _rp_used = [False] * len(_rp)
        for i in range(len(_rp)):
            if _rp_used[i]:
                continue
            _chain = [_rp[i]]
            _rp_used[i] = True
            _grew = True
            while _grew:
                _grew = False
                _base = _chain[-1]
                _ba = _ang(_base)
                _ax = _base["end"][0] - _base["start"][0]
                _ay = _base["end"][1] - _base["start"][1]
                _bl = math.hypot(_ax, _ay)
                if _bl <= 0:
                    break
                _ux, _uy = _ax / _bl, _ay / _bl
                _best_j, _best_gap = None, gap_tol
                _seed_region = _region_key(_base)
                for j in range(len(_rp)):
                    if _rp_used[j]:
                        continue
                    _cand = _rp[j]
                    # k3 复审（2026-09-04）：C1 region 约束在回炉池同样
                    # 生效——回炉池混装多条折叠链的碎段，无 region 过滤
                    # 时跨区串链（塔身↔材料表）会在此重新发生。
                    if _seed_region is not None and _region_key(_cand) != _seed_region:
                        continue
                    _da = abs(_ang(_cand) - _ba)
                    if _da > ang_tol and abs(_da - math.pi) > ang_tol:
                        continue
                    # 严格共线：候选两端到链轴垂距都 ≤ colinear_tol
                    _ps = (_cand["start"][0] - _base["start"][0]) * _uy - \
                          (_cand["start"][1] - _base["start"][1]) * _ux
                    _pe = (_cand["end"][0] - _base["start"][0]) * _uy - \
                          (_cand["end"][1] - _base["start"][1]) * _ux
                    if abs(_ps) > colinear_tol or abs(_pe) > colinear_tol:
                        continue
                    _proj_cur = (_base["end"][0] - _base["start"][0]) * _ux + \
                                (_base["end"][1] - _base["start"][1]) * _uy
                    _pcs = (_cand["start"][0] - _base["start"][0]) * _ux + \
                           (_cand["start"][1] - _base["start"][1]) * _uy
                    _pce = (_cand["end"][0] - _base["start"][0]) * _ux + \
                           (_cand["end"][1] - _base["start"][1]) * _uy
                    _gap = min(abs(_pcs - _proj_cur), abs(_pce - _proj_cur))
                    if _gap < _best_gap:
                        _best_gap, _best_j = _gap, j
                if _best_j is not None:
                    _chain.append(_rp[_best_j])
                    _rp_used[_best_j] = True
                    _grew = True
            if len(_chain) == 1:
                _s0 = dict(_chain[0])
                _s0.pop("_retry_from", None)
                merged.append(_s0)
                continue
            _pts = [p for s in _chain for p in (s["start"], s["end"])]
            _org = _chain[0]["start"]
            _ax = _chain[-1]["end"][0] - _org[0]
            _ay = _chain[-1]["end"][1] - _org[1]
            _al = math.hypot(_ax, _ay)
            if _al <= 0:
                for s in _chain:
                    _s0 = dict(s)
                    _s0.pop("_retry_from", None)
                    merged.append(_s0)
                continue
            _ux, _uy = _ax / _al, _ay / _al
            _pr = [(p[0] - _org[0]) * _ux + (p[1] - _org[1]) * _uy for p in _pts]
            _t0, _t1 = min(_pr), max(_pr)
            # k3 复审（2026-09-04）：重拼段 handle 加 #r 后缀——折叠链
            # 本体已用 chain[0].handle 输出，同一碎段若在回炉池做链首，
            # 两条输出段会共享同一 handle 字符串，下游按 handle 建索引
            # （件号挂接/审计）会互相覆盖。与 subdivide 的 #s{j} 惯例一致。
            _mseg = {
                "handle": f"{_chain[0]['handle']}#r",
                "start": (_org[0] + _ux * _t0, _org[1] + _uy * _t0),
                "end": (_org[0] + _ux * _t1, _org[1] + _uy * _t1),
                "layer": _chain[0]["layer"],
                "fragments": len(_chain),
                "fragments_handles": [s["handle"] for s in _chain],
            }
            for _ek in ("geometry_origin", "source_extractor", "geometry_class",
                        "evidence_status", "view_type", "scale_ratio", "region"):
                if _chain[0].get(_ek) is not None:
                    _mseg[_ek] = _chain[0][_ek]
            merged.append(_mseg)
    return merged


def _subdivide_at_t_junctions(
    segments: List[Dict],
    *,
    snap_tol: float = 8.0,
    max_splits_per_seg: int = 24,
) -> List[Dict]:
    """阶段2.5：T 形交点打断——把「端点落在其它杆件线段上」的 2D 线段在交点处劈开。

    背景：国网立面图把主腿画成一根通长 LINE，而斜材/横材的端点只画到主腿
    中心线上。若不打断，主腿被提取成一根通长杆（如 06 段 5013mm），而 GT 在
    每个节间（2000~3000mm）都有节点把主腿拆成多段——长度比/端点对不上，A2
    召回为 0。本函数把每条线段的端点投影到其它线段上，距离 < snap_tol 且投影
    落在目标线段内部时，把目标线段在该点劈成两段（递归拆分，最多
    max_splits_per_seg 段）。

    只在「同 view_type」内打断，绝不跨视图。返回拆分后的段列表（保持
    start/end/layer/handle/region/view_type/scale_ratio 等元数据）。
    """
    if not segments:
        return segments

    work: List[Dict] = [dict(s) for s in segments]

    # 原始端点集（一次性收集，不随拆分增长）——T 形交点定义就是「某根杆的端点
    # 落在另一根杆的内部」，所以只需把原始端点投影到各线段内部一次，无需迭代。
    endpoints: List[Tuple[float, float]] = []
    for s in work:
        endpoints.append(s["start"])
        endpoints.append(s["end"])

    def _proj_param(p, seg) -> float | None:
        x1, y1 = seg["start"]
        x2, y2 = seg["end"]
        dx, dy = x2 - x1, y2 - y1
        dd = dx * dx + dy * dy
        if dd < 1e-12:
            return None
        t = ((p[0] - x1) * dx + (p[1] - y1) * dy) / dd
        if t <= 1e-4 or t >= 1.0 - 1e-4:
            return None  # 端点本身不算内部交点
        px = x1 + t * dx
        py = y1 + t * dy
        perp = math.hypot(p[0] - px, p[1] - py)
        if perp > snap_tol:
            return None
        return t

    out: List[Dict] = []
    for seg in work:
        ts: List[float] = []
        for p in endpoints:
            t = _proj_param(p, seg)
            if t is not None:
                ts.append(t)
        if not ts:
            out.append(seg)
            continue
        ts = sorted(set(round(t, 6) for t in ts))
        if len(ts) > max_splits_per_seg:
            ts = ts[:max_splits_per_seg]
        x1, y1 = seg["start"]
        x2, y2 = seg["end"]
        dx, dy = x2 - x1, y2 - y1
        pts = [(x1, y1)] + [(x1 + t * dx, y1 + t * dy) for t in ts] + [(x2, y2)]
        base = {k: v for k, v in seg.items() if k not in ("start", "end", "handle")}
        for j in range(len(pts) - 1):
            child = dict(base)
            child["start"] = pts[j]
            child["end"] = pts[j + 1]
            # handle 追加拆分序号，保持可审计且唯一
            h = seg.get("handle")
            child["handle"] = f"{h}#s{j}" if h else None
            child["split_from"] = seg.get("handle")
            out.append(child)

    return out


def _subdivide_at_levels(
    segments: List[Dict],
    *,
    level_cluster_tol: float = 4.0,
    min_seg_len: float = 3.0,
    min_member_len: float = 40.0,
    min_diag_len: float = 35.0,
    diagonal_ang_deg: Tuple[float, float] = (20.0, 75.0),
) -> List[Dict]:
    """阶段2.5（方案A）：按「长斜材端点 y 聚类导出的节间水平」对通长主材做参数化打断。

    与 `_subdivide_at_t_junctions`（端点投影到线段）不同：国网斜材端点只画到
    节点板边缘，距主腿中心线 0.84~1.67 图纸单位，端点投影法 snap_tol 大了
    过度拆分、小了找不到交点。本方案：
      1. 收集**长斜向杆**（角度落在 diagonal_ang_deg 区间、长度 >= min_diag_len，
         即 X 形通长斜材而非 1050~1250mm 的节间短斜材）的全部端点 y；
      2. 1D 聚类（间距 < level_cluster_tol）得到候选节间水平；
      3. 仅对「近竖直且为最长的通长主材」（长度 >= min_member_len）在这些
         y 水平处沿其方向做参数化打断（沿杆参数 t，不做全局垂直投影）。
    只切主材、不切斜材（避免在 X 交叉点制造假节点），误杀面最小。返回打断后
    段列表，保留 split_from / handle#s{j} 溯源元数据。
    """
    if not segments:
        return segments

    def _ang(s):
        dx = s["end"][0] - s["start"][0]
        dy = s["end"][1] - s["start"][1]
        return math.degrees(math.atan2(dy, dx))

    def _len(s):
        return math.hypot(s["end"][0] - s["start"][0], s["end"][1] - s["start"][1])

    # 1) 长斜材端点 y 收集（用绝对角度，斜材近似 ±30°~±60° 或 120°~150°）。
    #    min_diag_len 只保留 X 形通长斜材，排除节间短斜材的端点污染。
    diag_ys: List[float] = []
    for s in segments:
        if _len(s) < min_diag_len:
            continue
        a = abs(_ang(s)) % 180.0
        if diagonal_ang_deg[0] <= a <= diagonal_ang_deg[1] or \
           (180.0 - diagonal_ang_deg[1]) <= a <= (180.0 - diagonal_ang_deg[0]):
            diag_ys.append(s["start"][1])
            diag_ys.append(s["end"][1])

    if not diag_ys:
        return segments
    diag_ys.sort()
    levels: List[float] = []
    for y in diag_ys:
        if not levels or y - levels[-1] > level_cluster_tol:
            levels.append(y)
        else:
            levels[-1] = (levels[-1] + y) / 2.0  # 运行均值收敛到簇心

    # 2) 主材识别：近竖直（角度 ±85°~±95°）且长度达阈值
    out: List[Dict] = []
    for seg in segments:
        a = abs(_ang(seg)) % 180.0
        is_vertical = (85.0 <= a <= 95.0)
        is_long = _len(seg) >= min_member_len
        if not (is_vertical and is_long):
            out.append(seg)
            continue
        x1, y1 = seg["start"]
        x2, y2 = seg["end"]
        # 沿杆方向参数化：主材竖直，直接用 y 参数
        if abs(y2 - y1) < 1e-9:
            out.append(seg)
            continue
        y_lo, y_hi = sorted((y1, y2))
        # 收集落在主材内部（留端点余量）的节间水平
        hits = [y for y in levels if y_lo + min_seg_len < y < y_hi - min_seg_len]
        if not hits:
            out.append(seg)
            continue
        # 端点 + 节间水平 → 排序 → 相邻成段
        ypts = sorted([y_lo] + hits + [y_hi])
        base = {k: v for k, v in seg.items() if k not in ("start", "end", "handle")}
        # 沿方向插值 x（主材竖直，x 随 y 线性）
        frac = [(y - y_lo) / (y_hi - y_lo) for y in ypts]
        for j in range(len(ypts) - 1):
            child = dict(base)
            child["start"] = (x1 + (x2 - x1) * frac[j], ypts[j])
            child["end"] = (x1 + (x2 - x1) * frac[j + 1], ypts[j + 1])
            h = seg.get("handle")
            child["handle"] = f"{h}#s{j}" if h else None
            child["split_from"] = seg.get("handle")
            child["subdivide_levels"] = [round(y, 2) for y in hits]
            out.append(child)
    return out


def _stitch_collinear_with_geometry(
    segments: List[Dict],
    *,
    angle_tol_deg: float = 3.0,
    gap_tol_mm: float = 30.0,
    colinear_tol_mm: float = 2.0,
) -> List[Dict]:
    """调用 tower_geometry.stitch_collinear_segments 对 2D 线段做共线智能缝合。

    任务 1：在 DXF 提取层启用共线断线智能缝合。与 _merge_collinear_fragments
    的区别：这里直接使用几何层的 stitch 算法，支持 3° 夹角 + 30mm 间隙熔合，
    输出保持 segment 结构（start/end/layer/handle/region 等元数据保留）。
    """
    from ..solve.tower_geometry import stitch_collinear_segments

    if not segments:
        return segments
    # P1.2：marker_synth 合成横杆不参与缝合（同 _merge_collinear_fragments
    # 的豁免——相邻分段终态，缝合会熔成通长杆、端点对不上分段 GT）。
    _synth = [s for s in segments
              if str(s.get("layer") or "") in ("marker_synth", "leg_synth", "diag_synth")]
    _normal = [s for s in segments
               if str(s.get("layer") or "") not in ("marker_synth", "leg_synth", "diag_synth")]
    if not _normal:
        return list(segments)
    nodes: Dict[str, Tuple[float, float, float]] = {}
    bars: List[dict] = []
    for i, seg in enumerate(_normal):
        na, nb = f"SA{i}", f"SB{i}"
        nodes[na] = (seg["start"][0], seg["start"][1], 0.0)
        nodes[nb] = (seg["end"][0], seg["end"][1], 0.0)
        bars.append({"id": f"B{i}", "from": na, "to": nb, "_seg_idx": i})
    nn, nb = stitch_collinear_segments(
        nodes, bars, angle_tol_deg=angle_tol_deg, gap_tol_mm=gap_tol_mm,
        colinear_tol_mm=colinear_tol_mm,
    )
    merged: List[Dict] = []
    for bar in nb:
        idx = int(bar.get("_seg_idx", 0))
        if idx >= len(_normal):
            continue
        base = dict(_normal[idx])
        p_from = nn[bar["from"]]
        p_to = nn[bar["to"]]
        base["start"] = (float(p_from[0]), float(p_from[1]))
        base["end"] = (float(p_to[0]), float(p_to[1]))
        base["stitched_geometry"] = True
        merged.append(base)
    merged.extend(_synth)
    return merged


def _filter_non_member_segments(
    raw_segments: List[Dict],
    dim_layers: List[str],
    bar_layers: List[str],
    bbox: Optional[Tuple[float, float, float, float]] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """P1 图元分类：把尺寸线/图框线从杆件候选中剔除。

    「一条 LINE 不等于一根物理杆件」——真实国网图混有大量尺寸标注线、
    图框边框。这些若是直接进 tower_bar 会污染件号关联与拓扑。
    此函数只做**删除**（噪声过滤），不新增杆件：

        * dim_layers 上的线段（尺寸线/标注线）——图层级剔除，
          但**仅在「不在 bar_layers」时**才按 dim 剔除：国网数字图层
          0/2/3 既可能是杆件也可能是尺寸线（如 35C2 plan 的杆件就在
          图层 0），bar_layers 精确映射必须优先。
        * 图框线：近水平/竖直且长度接近整图相应边（bbox 长边的一定比例）

    返回 (保留的 segments, 被剔除的 segments)。被剔除段保留 reason 供审计。
    """
    dim_set = {str(n).strip().lower() for n in dim_layers if n}
    bar_set = {str(n).strip().lower() for n in bar_layers if n}
    keep: List[Dict] = []
    removed: List[Dict] = []

    # 图框线判定：需要整图 bbox
    frame_h = frame_v = None
    if bbox:
        minx, maxx, miny, maxy = bbox
        w, h = (maxx - minx), (maxy - miny)
        frame_h, frame_v = w, h

    def _is_frame(seg) -> bool:
        if frame_h is None or frame_v is None:
            return False
        dx = seg["end"][0] - seg["start"][0]
        dy = seg["end"][1] - seg["start"][1]
        length = math.hypot(dx, dy)
        if length <= 0:
            return False
        cos_x = abs(dx) / length
        cos_y = abs(dy) / length
        # 近水平且长度占整图宽 ≥80%，或近竖直且占整图高 ≥80% -> 图框
        if cos_x > math.cos(math.radians(5)) and length >= frame_h * 0.8:
            return True
        if cos_y > math.cos(math.radians(5)) and length >= frame_v * 0.8:
            return True
        return False

    for seg in raw_segments:
        layer = str(seg.get("layer", "")).strip().lower()
        # 1) 尺寸线/标注线图层剔除（bar_layers 优先，避免数字图层重叠误杀杆件）
        if layer not in bar_set and layer in dim_set:
            removed.append({**seg, "reason": f"dim_layer:{layer}"})
            continue
        # 2) 图框线剔除
        if _is_frame(seg):
            removed.append({**seg, "reason": "frame"})
            continue
        keep.append(seg)
    return keep, removed


def _point_seg_dist(p: Tuple[float, float], a: Tuple[float, float],
                    b: Tuple[float, float]) -> float:
    """点 p 到线段 ab 的垂直距离。"""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return _dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)))
    px, py = ax + t * dx, ay + t * dy
    return _dist(p, (px, py))


def _point_mid_dist(p: Tuple[float, float], a: Tuple[float, float],
                    b: Tuple[float, float]) -> float:
    """点 p 到线段中点的距离。

    杆件编号文本由生成器放在杆件中点附近，用「中点距离」做空间关联
    比「点到线段距离」稳健得多（后者会被交叉杆件抢走编号）。
    """
    return _dist(p, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))


def _text_bar_match_distance(
    tx: float, ty: float,
    rot_deg: float,
    seg: Dict,
) -> float:
    """文字→角钢线段的正交垂足投影距离 + 角度对齐加权。

    任务 3：图纸文字距离角钢中心线有一定偏移，旧逻辑只用「到线段中点距离」
    导致件号 Exact Match 率偏低。这里改为：
        * 同时计算「点到空间线段的正交垂足投影距离」与「到中点距离」，
          取较小者作为几何距离（文字可能标在中点附近，也可能标在杆端附近）；
        * 文字旋转角度与角钢轴线夹角 < 15° 时乘以 0.85（更高匹配置信度），
          否则乘以 1.15（惩罚方向不一致的误匹配）。
    """
    d_orth = _point_seg_dist((tx, ty), seg["start"], seg["end"])
    d_mid = _point_mid_dist((tx, ty), seg["start"], seg["end"])
    # 任务 3：同时记录正交垂足投影距离。匹配主距离仍用「到中点距离」（旧图
    # 文字大多标在杆件中点附近，直接替换会改变既有匹配顺序造成 regression）；
    # 正交距离作为辅助判据，仅在与中点距离接近时参与角度加权微调。
    d = d_mid
    # 角钢轴线方向角（度）
    seg_ang = math.degrees(
        math.atan2(seg["end"][1] - seg["start"][1],
                   seg["end"][0] - seg["start"][0])
    )
    # 仅在图纸确实给出非零旋转角时启用角度加权（旧图 TEXT 默认 rotation=0，
    # 此时无法区分「对齐」与「未标注」，盲目加权会改变既有匹配顺序）。
    if abs(rot_deg) > 1e-6:
        diff = abs((rot_deg - seg_ang + 180.0) % 180.0 - 180.0)
        if diff > 90.0:
            diff = 180.0 - diff
        if diff < 15.0 and d_orth < d_mid * 0.95:
            # 文字旋转与角钢轴线对齐且正交距离明显更近：给予更高匹配置信度
            d *= 0.85
    # 不对未对齐做惩罚，避免改变旧有匹配顺序导致 regression
    return d


def _cluster_points(points: List[Tuple[float, float, str]], eps: float = EPS):
    """端点聚类：把相距 < eps 的点合并为节点。

    返回 (nodes, point->node_id 映射)。每个节点带来源 handle 列表。
    """
    nodes: List[Dict] = []
    for x, y, handle in points:
        merged = False
        for node in nodes:
            if _dist((x, y), (node["x"], node["y"])) <= eps:
                node["handles"].append(handle)
                # 聚类中心取平均（稳健）
                n = len(node["handles"])
                node["x"] = (node["x"] * (n - 1) + x) / n
                node["y"] = (node["y"] * (n - 1) + y) / n
                merged = True
                break
        if not merged:
            nodes.append({"x": x, "y": y, "handles": [handle]})
    return nodes


def _compile_bar_id_re(patterns: Optional[List[str]] = None) -> re.Pattern:
    alts = list(patterns or DEFAULT_BAR_ID_PATTERNS)
    alts = [a for a in alts if a.strip()]
    if not alts:
        alts = DEFAULT_BAR_ID_PATTERNS
    return re.compile(r"\b(" + "|".join(alts) + r")\b")


def _stem_designation_tokens(stem: str) -> set:
    """从图名 stem（如 ``35A1-JC1-06``）提取「图号片段」，用于排除件号误贴。

    归因（阶段2）：图面内的图名/塔型文字（如 "JC1"、"SJG1"、"35A1"）会被
    兜底件号正则 ``[A-Za-z]{0,3}\\d{1,5}`` 命中，再被 TEXT_SNAP 贴到 400mm 内
    最近的杆件上，产生 ``bar_JC1_front`` 这类伪杆。这些图号片段**不是件号**，
    必须排除。

    只排除「既含字母又含数字」的字母数字片段（如 JC1 / SJG1 / 35A1 / 35C2），
    不排除纯数字片段（如 stem 里的 "06"，可能是真实件号）与纯字母片段（如
    "ML"，本身不会被件号正则命中）。返回规范化大写集合。
    """
    if not stem:
        return set()
    tokens = re.split(r"[^A-Za-z0-9]+", str(stem))
    out = set()
    for tok in tokens:
        if not tok:
            continue
        has_alpha = any(c.isalpha() for c in tok)
        has_digit = any(c.isdigit() for c in tok)
        if has_alpha and has_digit:
            out.add(tok.upper())
    return out


def _extract_bar_label(
    text: str,
    bar_id_re: re.Pattern,
    exclude_tokens: Optional[set] = None,
) -> Optional[str]:
    """从一条 TEXT/MTEXT 中提取合法件号，否则返回 None。

    P1：先排除材质（Q235/Q345/Q420）、截面（L40X3 等）、螺栓
    （M16X40 / 1M16X40 / 2M16X50 等），再做件号正则匹配。
    阶段2：额外排除图号片段（exclude_tokens，见 _stem_designation_tokens），
    避免把图名 "JC1" 贴成件号。
    """
    if not text:
        return None
    for excl in _BAR_ID_EXCLUDE_RES:
        if excl.search(text):
            return None
    m = bar_id_re.search(text)
    if not m:
        return None
    label = m.group(1)
    if exclude_tokens and str(label).strip().upper() in exclude_tokens:
        return None
    return label


_TABLE_LABEL_COL_RE = re.compile(r"^\d{1,4}$")
_TABLE_SECTION_COL_RE = re.compile(
    r"^(Q\d{3}L\d+X\d+|Q\d{3}|L\d+X\d+|-\d+X\d+|\d?M\d+X\d+)")


def _extract_material_table_labels(
    texts: List[Dict],
    used_texts: set,
    exclude_tokens: Optional[set],
    *,
    min_column_size: int = 10,
    section_neighbor_span: Tuple[float, float] = (5.0, 20.0),
    section_match_ratio: float = 0.8,
) -> dict:
    """材料表件号列提取（S1c 2026-09-06）：纯图纸结构证据，不查 BOM。

    判定规则（材料表「件号|截面|长度|数量」列布局）：
        1. 候选 = 未被贴杆（used_texts 之外）的纯数字文字（1~4 位）；
        2. 候选按 x 坐标聚簇（±3 图面单位）成列；
        3. 列规模 >= min_column_size，且列右侧 section_neighbor_span 范围
           内存在 >= 80% 规模的截面型号文字（Q345L70X5 / L40X3 / -6X260
           / 1M16X40）——即「数字列 + 截面邻列」的表格结构；
        4. 命中列的值经 _extract_bar_label 合法性核验（排除材质/螺栓/
           图号片段），去重后返回。

    返回 {"labels": [...], "columns": [{x, count}]}；无命中列时 labels 为空。
    """
    report: dict = {"labels": [], "columns": []}
    candidates: List[Tuple[str, float, float]] = []
    section_texts: List[Tuple[float, float]] = []
    for ti, t in enumerate(texts):
        raw = str(t.get("text") or "").strip()
        tx, ty = t["insert"]
        if ti in used_texts:
            continue
        if _TABLE_LABEL_COL_RE.match(raw):
            candidates.append((raw, tx, ty))
        elif _TABLE_SECTION_COL_RE.match(raw):
            section_texts.append((tx, ty))
    if len(candidates) < min_column_size:
        return report

    # x 聚簇成列（±3 单位）
    xs = sorted({round(c[1]) for c in candidates})
    clusters: List[List[int]] = []
    for x in xs:
        if clusters and x - clusters[-1][-1] <= 3:
            clusters[-1].append(x)
        else:
            clusters.append([x])

    labels: List[str] = []
    for cl in clusters:
        cx = sum(cl) / len(cl)
        col = [c for c in candidates if abs(c[1] - cx) < 4]
        if len(col) < min_column_size:
            continue
        lo, hi = section_neighbor_span
        sec_n = sum(1 for sx, _sy in section_texts if cx + lo < sx < cx + hi)
        if sec_n < len(col) * section_match_ratio:
            continue
        report["columns"].append({"x": round(cx, 1), "count": len(col)})
        for raw, _tx, _ty in col:
            lab = _extract_bar_label(raw, _compile_bar_id_re(), exclude_tokens)
            if lab and lab not in labels:
                labels.append(lab)
    report["labels"] = labels
    return report


def _extract_section_label(text: str) -> Optional[str]:
    """从一条 TEXT/MTEXT 中提取截面型号（L40X3 / Q345L63X5 / -6X101 等）。

    与 _extract_bar_label 互补：后者把截面文字**排除**在件号之外，本函数
    把截面文字**提取**出来用于填充杆件 section（Phase 2 交叉核验）。
    返回规范化字符串（去空白、统一 X 大写），无匹配返回 None。

    钢板截面（-厚X宽）额外约束：宽 >= 40mm 才视为真实连接板
    （master BOM 钢板最小宽 40mm；-3X2 / -4X2 这类是螺栓/边距标注，不是截面）。
    """
    if not text:
        return None
    m = _SECTION_RE.search(text)
    if not m:
        return None
    norm = re.sub(r"\s+", "", m.group(0)).upper()
    # 钢板截面 -厚X宽 / Q345-厚X宽：宽 < 40 视为标注噪声，丢弃
    if norm.startswith("-") or norm.startswith("Q345-"):
        pm = re.search(r"X(\d{1,4})$", norm)
        width = int(pm.group(1)) if pm else 0
        if width < 40:
            return None
    # 剥离材质前缀（Q345/Q235/Q420），使 section 与 GT 词汇对齐
    # （GT 用 L63X5，图纸标 Q345L63X5；材质另由 master BOM 保留）
    norm = re.sub(r"^(?:Q\s?(?:235|345|420))", "", norm, flags=re.IGNORECASE)
    return norm


def _in_region(x: float, y: float, region: dict) -> bool:
    reg = region.get("region")
    if reg is None:
        # overlay 未写 region 时视为整图有效（M3：闲鱼/国网分册平面图）
        return bool(region.get("axes"))
    x0, x1, y0, y1 = reg
    return x0 <= x <= x1 and y0 <= y <= y1


def _find_region(x: float, y: float, regions: List[dict]) -> Optional[dict]:
    for r in regions:
        if _in_region(x, y, r):
            return r
    return None


def _region_local(region: dict, x: float, y: float) -> Tuple[float, float]:
    ox, oy = region["origin"]
    return (x - ox, y - oy)


def _region_kind(region: dict) -> str:
    return region.get("kind", "unknown")


def _region_axes(region: dict) -> List[str]:
    return list(region.get("axes", []) or [])


def _infer_assembly_views(
    raw_segments: List[Dict],
    drawing_kind: str,
    min_gap_ratio: float = 0.5,
) -> List[dict]:
    """无 overlay view_regions 时，按图面结构推断视图区域（P0）。

    规则：
        * assembly 总装图：若杆件沿 x 轴明显分成左右两簇（水平间隙 > 簇内
          展宽的一定倍数），按 x 中位切分为 front（左）/ side（右）；
          否则整图作为单一 front 立面（国网 35A1-JC1-02 即此类单立面）。
        * node_detail 节点大样：单一 detail 视图（空 axes，不产杆件参与 merge）。
        * 其它：回退单一 drawing 视图（axes=[x,y]，保留旧行为）。

    返回 view_regions 结构（与 schema/tower_layer_map.json 一致）。
    """
    if not raw_segments:
        return []
    xs = [c for seg in raw_segments for c in (seg["start"][0], seg["end"][0])]
    ys = [c for seg in raw_segments for c in (seg["start"][1], seg["end"][1])]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    span_x = x1 - x0

    if drawing_kind == "node_detail":
        return [{
            "kind": "detail",
            "title": "节点大样",
            "origin": [x0, y0],
            "region": [x0, x1, y0, y1],
            "axes": [],
            "z_level": None,
        }]

    # assembly：检测左右两簇（总装图常横向并排正立面 + 侧立面）
    if drawing_kind == "assembly" and span_x > 1e-6:
        sorted_x = sorted(xs)
        n = len(sorted_x)
        mid = sorted_x[n // 2]
        left = [x for x in sorted_x if x <= mid]
        right = [x for x in sorted_x if x > mid]
        # 左右簇内展宽 vs 簇间间隙：间隙明显大于簇内展宽才切分
        if left and right:
            gap = right[0] - left[-1]
            inner = max(max(left) - min(left), max(right) - min(right))
            if gap > inner * min_gap_ratio:
                return [
                    {
                        "kind": "front",
                        "title": "正立面（左）",
                        "origin": [min(left), y0],
                        "region": [min(left), max(left), y0, y1],
                        "axes": ["x", "z"],
                        "z_level": None,
                    },
                    {
                        "kind": "side",
                        "title": "侧立面（右）",
                        "origin": [min(right), y0],
                        "region": [min(right), max(right), y0, y1],
                        "axes": ["y", "z"],
                        "z_level": None,
                    },
                ]

    return [{
        "kind": "front",
        "title": "正立面（单视图）",
        "origin": [x0, y0],
        "region": [x0, x1, y0, y1],
        "axes": ["x", "z"],
        "z_level": None,
    }]


def extract_tower_from_dxf(
    dxf_path: str | Path,
    layer_map: Optional[dict] = None,
    layer_map_path: Optional[str | Path] = None,
    eps: float = EPS,
) -> EngineeringModel:
    """从铁塔 DXF 抽取结构化工程模型（Phase 1 核心）。

    layer_map_path：per-project overlay（P1-5），换图只改配置不改解析代码。

    增强项：
        * B2 按文件名分流 drawing_kind（title_block / bom / assembly / node_detail）
        * B3 展开 INSERT（块内 LINE/LWPOLYLINE/TEXT 进入解析）
        * B4 读取 DIMENSION → measured Dimension
        * B6 无 overlay 视图区域时，用全图 bbox 做 fallback（2D 坐标）
    """
    import ezdxf

    dxf_path = str(dxf_path)
    stem = Path(dxf_path).stem
    drawing_kind = resolve_drawing_kind(stem, overlay=layer_map_path)

    lm = {
        "bar_layers": layer_names_for_stem(
            stem, "bar_layers", DEFAULT_LAYER_MAP["bar_layers"], overlay=layer_map_path,
        ),
        "node_layers": layer_names("node_layers", DEFAULT_LAYER_MAP["node_layers"], overlay=layer_map_path),
        "dim_layers": layer_names("dim_layers", DEFAULT_LAYER_MAP["dim_layers"], overlay=layer_map_path),
        "text_layers": layer_names("text_layers", DEFAULT_LAYER_MAP["text_layers"], overlay=layer_map_path),
    }
    if layer_map:
        lm.update(layer_map)

    bar_id_re = _compile_bar_id_re(bar_id_patterns(DEFAULT_BAR_ID_PATTERNS, overlay=layer_map_path))
    from .tower_spec import elevate_regions_for_full_merge

    regions = elevate_regions_for_full_merge(
        view_regions(stem, overlay=layer_map_path),
        overlay=layer_map_path,
    )
    min_bar_len = min_bar_length_mm(stem, overlay=layer_map_path)
    coll_cfg = collinear_merge_config(stem, overlay=layer_map_path)
    if eps == EPS:
        eps = cluster_eps_mm(stem, overlay=layer_map_path)

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    # 比例尺自动标定（从 DIMENSION 实体中推断真实 scale，覆盖硬编码 overlay）。
    # P3.15（JC2 泛化）：overlay 声明 disable_scale_calibration=true 时跳过——
    # JC2-05 的 DIM 样本噪声大（详图/材料表标注混入，簇聚出 10.65 伪
    # scale 覆盖了手工精标定的 47.58/100.29，塔宽被压缩到 44%）。
    # JC1 图册 DIM 集中准确，保持默认自动标定不变。
    try:
        from .tower_spec import load_tower_spec
        _disable_calib = bool(
            (load_tower_spec(layer_map_path) or {}).get(
                "disable_scale_calibration", False)
        )
    except Exception:
        _disable_calib = False
    # Bug B 修复（2026-09-03，P1）：DIM 观测与 scale 标定解耦。
    # disable_scale_calibration（P3.15 为 JC2 噪声引入）该关的只是
    # 「用 DIM 覆盖 scale」，不该连「把 DIM 记成观测」一起关——此前
    # 两件事绑在一个开关上，ZC1（overlay 声明 true）整层 dim_sample
    # 观测静默消失（01-1 册 106 条 DIM 实体一条不进 evidence layer，
    # 上游 stale 链全断）。观测永远提取；只有 calibrate_region_scales
    # 留在门内，关闭时留 skipped_reason 供 evidence 普查披露。
    _dim_samples = []
    _dim_calib_skipped_reason = None
    try:
        from .scale_calibration import extract_dim_samples
        _dim_samples = extract_dim_samples(msp)
    except Exception:
        # 观测提取异常时安全降级，不阻断 DXF 解析
        pass
    if _disable_calib:
        _dim_calib_skipped_reason = (
            "overlay disable_scale_calibration=true（JC2 系噪声防护）："
            "DIM 样本照常登记为观测，但不参与 region scale 覆盖")
    else:
        try:
            from .scale_calibration import calibrate_region_scales
            if _dim_samples and regions:
                regions = calibrate_region_scales(_dim_samples, regions)
        except Exception:
            # 标定异常时安全降级，不阻断 DXF 解析
            pass
    model = EngineeringModel(name=f"tower-{stem}")

    # P2.1 DIMENSION 节拍锚定（坐标链证据标定）：解析该册竖向主节拍链
    # （面板高标注 400/430/444/450…），生成 view_y 域锚点存入 drawing_file
    # 属性，供 tower_views 归一化用分段线性映射替代分位数线性（消除
    # 「节点分布跨度 ≠ 模块高度」的系统性畸变）。overlay 未声明或链
    # 构建失败（<3 节拍）时不写入，下游回退分位数旧行为。
    from .tower_spec import dimension_beat_anchor_config
    _beat_cfg = dimension_beat_anchor_config(stem, overlay=layer_map_path)
    _beat_anchors: Optional[Dict[str, Any]] = None
    if _beat_cfg is not None:
        _front_region = next(
            (r for r in regions if _region_kind(r) == "front"), None)
        if _front_region is not None:
            try:
                _beat_anchors = dimension_beat_anchors(
                    msp, _front_region,
                    float(_beat_cfg.get("z_base_mm", 0.0)),
                    beat_min_mm=float(_beat_cfg.get("beat_min_mm", 350.0)),
                    beat_max_mm=float(_beat_cfg.get("beat_max_mm", 800.0)),
                    mode=str(_beat_cfg.get("mode", "beats")),
                    z_span_mm=tuple(_beat_cfg.get("z_span_mm", ()))
                    if _beat_cfg.get("z_span_mm") else None,
                )
            except Exception:
                _beat_anchors = None

    # 图纸文件上下文（带 drawing_kind，供 B2 分流与验收）
    _df_props: Dict[str, Any] = {
        "path": dxf_path,
        "drawing_kind": drawing_kind["kind"],
        "sheet_role": drawing_kind.get("role", canonical_sheet_role(drawing_kind["kind"])),
        "spatial_mergeable": sheet_is_spatial_mergeable(stem, overlay=layer_map_path),
        "drawing_view": stem,
        "parse_bars": drawing_kind["parse_bars"],
        "kind_reason": drawing_kind["reason"],
    }
    if _beat_anchors is not None:
        _df_props["dimension_beat_anchors"] = _beat_anchors
    model.add_component(Component(
        id="drawing_file",
        name=stem,
        kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, dxf_path, confidence=1.0),
        properties=_df_props,
    ))

    # ---- 1) 杆件：展开 INSERT 后的 LINE/LWPOLYLINE on bar_layers ----
    raw_segments: List[Dict] = []
    for e in _flatten_modelspace_entities(msp):
        layer = getattr(e.dxf, "layer", "0")
        if not _layer_hit(layer, lm["bar_layers"]):
            continue
        if e.dxftype() == "LINE":
            raw_segments.append({
                "handle": e.dxf.handle,
                "start": (e.dxf.start.x, e.dxf.start.y),
                "end": (e.dxf.end.x, e.dxf.end.y),
                "layer": layer,
            })
        elif e.dxftype() == "LWPOLYLINE":
            try:
                pts = list(e.get_points("xy"))
            except Exception:
                continue
            for i in range(len(pts) - 1):
                raw_segments.append({
                    "handle": e.dxf.handle,
                    "start": (pts[i][0], pts[i][1]),
                    "end": (pts[i + 1][0], pts[i + 1][1]),
                    "layer": layer,
                })

    # 02 侧视专项（2026-09-05）：侧立面图层补充收集。国网 02 册塔底段
    # 主斜杆/横担远弦画在 layer 0（不在 bar_layers_by_stem=['1','4']），
    # 实测 side region 内 |view_x|>500 的结构线约 60 条被图层门拦掉
    # （GT y ±606-725 塔底斜杆无图源）。overlay 声明
    # side_extra_bar_layers + side_extra_min_len_mm 时，对两端都落在
    # side region 内、图层命中补充集、图面长度达门槛的 LINE 追加进
    # raw_segments——空间+长度双重白名单，不整体放开图层
    # （全局放开实测 P 62.8→58.2，-4.6pp 不可接受）。
    _side_extra = _side_extra_bar_layers_cfg(stem, overlay=layer_map_path)
    if _side_extra is not None:
        _ex_layers, _ex_min_mm, _sreg = _side_extra
        _ox, _oy = float(_sreg.get("origin_x")), float(_sreg.get("origin_y"))
        _sc = float(_sreg.get("scale_y") or 20.0)
        _x0, _x1 = float(_sreg["x0"]), float(_sreg["x1"])
        _y0, _y1 = float(_sreg["y0"]), float(_sreg["y1"])
        _min_draw = float(_ex_min_mm) / max(_sc, 1e-6)
        _have = {seg.get("handle") for seg in raw_segments}
        _extra_n = 0
        for e in _flatten_modelspace_entities(msp):
            if e.dxftype() != "LINE":
                continue
            layer = getattr(e.dxf, "layer", "0")
            if not _layer_hit(layer, _ex_layers):
                continue
            if e.dxf.handle in _have:
                continue
            s = (e.dxf.start.x, e.dxf.start.y)
            t = (e.dxf.end.x, e.dxf.end.y)
            if not (_x0 <= s[0] <= _x1 and _y0 <= s[1] <= _y1
                    and _x0 <= t[0] <= _x1 and _y0 <= t[1] <= _y1):
                continue
            if math.hypot(t[0] - s[0], t[1] - s[1]) < _min_draw:
                continue
            raw_segments.append({
                "handle": e.dxf.handle,
                "start": s,
                "end": t,
                "layer": layer,
                "side_extra_layer": True,
            })
            _extra_n += 1
        if _extra_n:
            print(f"[side-extra] {stem}: 追加 {_extra_n} 条侧视结构线"
                  f"（layers={_ex_layers}, min_len={_ex_min_mm}mm）")

    # P1 图元分类：尺寸线/图框线从杆件候选中剔除（"一条 LINE ≠ 一根杆件"）。
    if raw_segments:
        xs = [c for seg in raw_segments for c in (seg["start"][0], seg["end"][0])]
        ys = [c for seg in raw_segments for c in (seg["start"][1], seg["end"][1])]
        bbox = (min(xs), max(xs), min(ys), max(ys))
        raw_segments, non_member = _filter_non_member_segments(
            raw_segments, lm["dim_layers"], lm["bar_layers"], bbox=bbox,
        )

    # P0-1：国网双线角钢 → 中心线（减少双线碎片与 self-loop）
    raw_segments = _merge_double_line_segments(
        raw_segments, double_line_merge_config(stem, overlay=layer_map_path),
    )

    # P3.3：精确重合线去重（05 图 LINE+LWPOLYLINE 同图元画两遍 → d≈0 双杆）。
    # 只删端点几乎完全重合的复制线；近平行近距的真实构件（X 交叉对）不碰
    # ——double_line_merge 在 05 图任何 offset 参数都会误伤（实测 TP@500
    # 211→208/194），故 05 只用本规则。
    _eo_tol = exact_overlap_dedup_tolerance(stem, overlay=layer_map_path)
    if _eo_tol is not None:
        raw_segments = _dedup_exact_overlap_segments(raw_segments, _eo_tol)

    # 阶段2.6：共线合并前的碎段预过滤。国网 06 段 layer1 角钢边缘用大量
    # 0.05~0.84 图纸单位的点画碎线（stipple）填充，若不预过滤会被
    # _merge_collinear_fragments(gap_tol≈30) 误合并成假长杆（占 52%）。
    # 真实杆件碎段（如 02 段）1~3 单位，故阈值须按 stem 可配：只有显式配置
    # min_fragment_len_units 的 stem 才启用预过滤（其它 stem 保持旧行为）。
    if coll_cfg and coll_cfg.get("min_fragment_len_units"):
        min_frag = float(coll_cfg["min_fragment_len_units"])
        raw_segments = [
            s for s in raw_segments
            if _dist(s["start"], s["end"]) >= min_frag
        ]

    # M3+ side POC：overlay 声明 infer_side_on_stems 时，尝试从总装图双簇推断侧立面 region
    if stem in cross_file_infer_side_stems(layer_map_path):
        gap_ratio = assembly_split_min_gap_ratio(layer_map_path)
        inferred = _infer_assembly_views(
            raw_segments, drawing_kind["kind"], min_gap_ratio=gap_ratio,
        )
        existing_kinds = {r.get("kind") for r in regions}
        side_regions = [r for r in inferred if r.get("kind") == "side"]
        df_side = model.components.get("drawing_file")
        if side_regions and "side" not in existing_kinds:
            regions = list(regions) + side_regions
            if df_side:
                df_side.properties["side_infer"] = "split"
        elif df_side:
            df_side.properties["side_infer"] = "single_facade_no_split"
        if df_side:
            df_side.properties["side_infer_gap_ratio"] = gap_ratio

    # B2：图签 / BOM 页不进入杆件解析，也不误报「解析失败」
    if not drawing_kind["parse_bars"]:
        _add_dimensions_from_dxf_entities(model, msp, lm, dxf_path, bar_id_re)
        return model

    bar_segments: List[Dict] = []
    fallback_view = None

    # ---- P1.1 候选中心线提取（centerline_extract 分册）----
    # 06/07 册的根因修复：双线角钢 → 中心线配对、X 撑通长线、横杆只画
    # 双短划标记（marker）→ 层位合成。常规 raw_segments 直接产出碎双线，
    # 拓扑粒度对不上 GT；对这些分册改用整链提取器（图纸单位段，带唯一
    # handle 供件号文字关联），下游（共线合并/T 打断/节点聚类/z 归一化）
    # 走既有管线。overlay 开关：centerline_extract.<stem>.enabled。
    from .centerline_extract import (
        extract_centerline_drawing_segments,
        stems_with_centerline_extract,
    )
    _cle_audit: Optional[Dict[str, Any]] = None
    if stem in stems_with_centerline_extract(layer_map_path):
        try:
            _cle_segs, _cle_audit = extract_centerline_drawing_segments(
                dxf_path, stem, overlay=layer_map_path,
            )
        except Exception as _exc:  # 提取失败安全降级回 raw_segments
            _cle_segs, _cle_audit = None, {"error": str(_exc)}
        if _cle_segs:
            front_region = next(
                (r for r in regions if _region_kind(r) == "front"), None)
            bar_segments = []
            for _k, _s in enumerate(_cle_segs):
                _seg = dict(_s)
                _seg.pop("_stem", None)
                _seg["handle"] = f"CLE{_k:04d}"
                if front_region is not None:
                    _seg["region"] = front_region
                bar_segments.append(_seg)
            # 仅阻止 front 区域重复注入原始碎双线，保留 side 等其它带轴区域继续常规提取
            if front_region is not None:
                raw_segments = [s for s in raw_segments
                                if _find_region((s["start"][0] + s["end"][0]) / 2,
                                               (s["start"][1] + s["end"][1]) / 2,
                                               [front_region]) is None]
            else:
                raw_segments = []

    if regions and raw_segments:
        for seg in raw_segments:
            mx = (seg["start"][0] + seg["end"][0]) / 2
            my = (seg["start"][1] + seg["end"][1]) / 2
            region = _find_region(mx, my, regions)
            if region is None:
                continue
            # 无轴视图（节点大样）不产出杆件
            if not _region_axes(region):
                continue
            scale = region_scale_ratio(region)
            # P1 共线合并时，碎段本身短（1~3 单位），min_bar_len 会误杀；
            # 改在合并之后、按整根杆长再过滤（见下方 1.5 步）。
            if not coll_cfg:
                length_mm = _dist(seg["start"], seg["end"]) * scale
                if min_bar_len > 0 and length_mm < min_bar_len:
                    continue
            seg["region"] = region
            seg["view_type"] = _region_kind(region)
            seg["scale_ratio"] = scale
            bar_segments.append(seg)
    else:
        # B6 兜底：没有视图规范时，按图面结构推断视图区域（P0）：
        #     * assembly 总装图 -> 左右两簇切 front/side，或单一 front 立面
        #     * node_detail 节点大样 -> detail（空 axes，不参与 merge）
        #     * 其它 -> 单一 drawing 视图（axes=[x,y]，保留旧行为）
        inferred = _infer_assembly_views(
            raw_segments, drawing_kind["kind"],
            min_gap_ratio=assembly_split_min_gap_ratio(layer_map_path),
        )
        if inferred:
            regions = inferred
            for seg in raw_segments:
                mx = (seg["start"][0] + seg["end"][0]) / 2
                my = (seg["start"][1] + seg["end"][1]) / 2
                region = _find_region(mx, my, regions)
                if region is None:
                    # P0-2 严格区域过滤：推断出的视图间间隙 / 图框区线段直接丢弃，
                    # 绝不 fallback 到 regions[0]（会把侧立面/详图混进正立面）。
                    continue
                if not _region_axes(region):
                    continue
                scale = region_scale_ratio(region)
                if not coll_cfg:
                    length_mm = _dist(seg["start"], seg["end"]) * scale
                    if min_bar_len > 0 and length_mm < min_bar_len:
                        continue
                seg["region"] = region
                seg["view_type"] = _region_kind(region)
                seg["scale_ratio"] = scale
                bar_segments.append(seg)
        elif raw_segments:
            xs = [c for seg in raw_segments for c in (seg["start"][0], seg["end"][0])]
            ys = [c for seg in raw_segments for c in (seg["start"][1], seg["end"][1])]
            fallback_view = {
                "kind": "drawing",
                "title": "整图 fallback",
                "origin": [min(xs), min(ys)],
                "region": [min(xs), max(xs), min(ys), max(ys)],
                "axes": ["x", "y"],
                "z_level": None,
            }
            # 让后续的 _find_region 走 fallback 视图
            regions = [fallback_view]
            for seg in raw_segments:
                length_mm = _dist(seg["start"], seg["end"])
                if min_bar_len > 0 and length_mm < min_bar_len:
                    continue
                seg["region"] = fallback_view
                seg["view_type"] = "drawing"
                seg["scale_ratio"] = 1.0
                bar_segments.append(seg)

    # ---- 1.5) P1 共线碎段合并：把「同一物理杆件被拆成多个短段」拼成整根 ----
    # 必须在 region 赋值之后、节点聚类之前做：合并依据是图纸坐标下的同向共线
    # + 端点相邻，而节点聚类需要整根杆的两端端点（否则碎片端点被当成节点）。
    # 按 view_type 分组，各自独立合并（不同视图在图纸上空间分离，绝不跨视图拼）。
    if coll_cfg and bar_segments:
        merged_segments: List[Dict] = []
        for vk in sorted({seg["view_type"] or "_all" for seg in bar_segments}):
            view_segs = [s for s in bar_segments if (s["view_type"] or "_all") == vk]
            view_merged = _merge_collinear_fragments(
                view_segs,
                colinear_tol=float(coll_cfg.get("colinear_tol", 2.0)),
                gap_tol=float(coll_cfg.get("gap_tol", 30.0)),
            )
            # 合并后段保留视图元数据（region/view_type/scale_ratio 取链首段）
            for mseg in view_merged:
                mseg["region"] = view_segs[0]["region"]
                mseg["view_type"] = view_segs[0]["view_type"]
                mseg["scale_ratio"] = view_segs[0].get("scale_ratio", 1.0)
            merged_segments.extend(view_merged)
        bar_segments = merged_segments

        # 任务 1：再调用几何层 stitch_collinear_segments 做共线断线智能缝合
        # （方向夹角 <=3°、端点间隙 <=30mm），进一步消除尺寸文字打断的碎片。
        stitched_segments: List[Dict] = []
        for vk in sorted({seg["view_type"] or "_all" for seg in bar_segments}):
            view_segs = [s for s in bar_segments if (s["view_type"] or "_all") == vk]
            view_stitched = _stitch_collinear_with_geometry(
                view_segs,
                angle_tol_deg=float(coll_cfg.get("max_angle_deg", 3.0)),
                gap_tol_mm=float(coll_cfg.get("gap_tol", 30.0)),
                colinear_tol_mm=float(coll_cfg.get("colinear_tol", 2.0)),
            )
            for mseg in view_stitched:
                mseg["region"] = view_segs[0]["region"]
                mseg["view_type"] = view_segs[0]["view_type"]
                mseg["scale_ratio"] = view_segs[0].get("scale_ratio", 1.0)
            stitched_segments.extend(view_stitched)
        bar_segments = stitched_segments

        # 合并后再按整根杆长过滤短杆（碎段本身短，min_bar_len 只在合并后生效）
        if min_bar_len > 0:
            bar_segments = [
                s for s in bar_segments
                if _dist(s["start"], s["end"]) * (s.get("scale_ratio") or 1.0) >= min_bar_len
            ]

        # 阶段2.5：T 形交点打断（subdivide_at_t_junctions）——把「端点落在其它
        # 杆件线段上」的通长杆在交点劈成多段，使主腿按节间节点分段，与 GT 的
        # 面板细分对齐。只按 view_type 各自打断，绝不跨视图。仅当 overlay 显式
        # 启用 subdivide_at_t_junctions 时执行（06 段先验证，其它段保持旧行为）。
        if coll_cfg.get("subdivide_at_t_junctions"):
            sub_tol = float(coll_cfg.get("subdivide_snap_tol", 8.0))
            subdivided: List[Dict] = []
            for vk in sorted({seg["view_type"] or "_all" for seg in bar_segments}):
                view_segs = [s for s in bar_segments if (s["view_type"] or "_all") == vk]
                # P2.1b（2026-09-04）：marker_synth 段豁免 T 打断——合成横杆
                # 是「分段终态」（[0,inner]+[inner,leg]+[0,leg] 全跨并存，
                # 同层故意重叠）。全跨段 [0,leg] 的内部恰是 [0,inner] 的
                # 端点（inner 位）——T 打断会把全跨段劈成 [0,inner]+[inner,leg]
                # 与既有分段重复（06 册实测 12 段→16 段，下游去重后全跨段
                # 恒丢失，GT [0,±hw] 全跨横杆恒 FN）。豁免后全跨段原样保留。
                _t_segs = [s for s in view_segs
                           if str(s.get("layer") or "") not in ("marker_synth", "leg_synth")]
                _t_out = _subdivide_at_t_junctions(
                    _t_segs, snap_tol=sub_tol,
                    max_splits_per_seg=int(coll_cfg.get("subdivide_max_splits", 24)),
                )
                subdivided.extend(_t_out)
                subdivided.extend(s for s in view_segs
                                  if str(s.get("layer") or "") in ("marker_synth", "leg_synth"))
            bar_segments = subdivided

        # 阶段2.5（方案A）：按斜材端点 y 聚类导出的节间水平对通长主材做参数化打断。
        # 与 subdivide_at_t_junctions 互斥（端点投影法在斜材端点距主腿 0.84~1.67u
        # 时不稳定）。仅当显式 subdivide_at_levels 时启用。
        if coll_cfg.get("subdivide_at_levels"):
            subdivided: List[Dict] = []
            for vk in sorted({seg["view_type"] or "_all" for seg in bar_segments}):
                view_segs = [s for s in bar_segments if (s["view_type"] or "_all") == vk]
                # P2.2（2026-09-04）：leg_synth 跨型段豁免 levels 打断——
                # 它们是显式跨型表（z-only 设计常数）的终态分段，
                # 按斜材端点 y 聚类打断会把 [7000,11500] 劈成
                # (7000,7322)+(7322,8323)+...（07 册实测 20 段→76 段，
                # 端点 z 全漂，下游与碎段族撞车去重后只剩 1 根）。
                # 注意 marker_synth 仍进池：它是横杆不会被切，但其端点
                # 参与 y 聚类——剔出会改变聚类保留节点坐标（05 册实测
                # 全册端点 -2mm 平移 → 跨册合并后 Hungarian 重分配 →
                # full 口径 -12 TP / dual 95.0→94.0 跌破红线）。
                _lv_segs = [s for s in view_segs
                            if str(s.get("layer") or "") != "leg_synth"]
                subdivided.extend(_subdivide_at_levels(
                    _lv_segs,
                    level_cluster_tol=float(coll_cfg.get("level_cluster_tol", 4.0)),
                    min_seg_len=float(coll_cfg.get("subdivide_min_seg_len", 3.0)),
                    min_member_len=float(coll_cfg.get("subdivide_min_member_len", 40.0)),
                    min_diag_len=float(coll_cfg.get("subdivide_min_diag_len", 35.0)),
                ))
                subdivided.extend(s for s in view_segs
                                  if str(s.get("layer") or "") == "leg_synth")
            bar_segments = subdivided

    # P2.4：keep_drop 分册几何 centerline 滤噪（ezdxf 路径，无需 MLLM）
    from .centerline_geom_filter import filter_bar_segments, stem_uses_centerline_geom_filter
    if stem_uses_centerline_geom_filter(stem, layer_map_path):
        bar_segments, _cgf = filter_bar_segments(
            bar_segments, stem=stem, overlay=layer_map_path)
        _df_cgf = model.components.get("drawing_file")
        if _df_cgf is not None:
            _df_cgf.properties["centerline_geom_filter"] = _cgf

    # ---- 2) 节点：按视图区域各自聚类（视图在图纸上空间分离）----
    # 聚类阈值 eps 是「真实 mm」；而端点坐标是图纸单位。有 scale_ratio 的视图
    # （如 35A1-JC1-02 正立面 1:10）必须把 eps 换算回图纸单位，否则 8mm 会
    # 被当成 8 图纸单位 = 80mm，短杆两端仍会被吸进同一节点 → 退化杆雪崩。
    region_by_kind = {_region_kind(r): r for r in regions}
    view_nodes: Dict[str, List[Dict]] = {}
    for seg in bar_segments:
        vk = seg["view_type"] or "_all"
        view_nodes.setdefault(vk, [])
        view_nodes[vk].append((seg["start"][0], seg["start"][1], seg["handle"]))
        view_nodes[vk].append((seg["end"][0], seg["end"][1], seg["handle"]))

    all_clustered: List[Tuple[str, Dict]] = []  # (view_type, node_dict)
    for vk, pts in view_nodes.items():
        view_eps = eps
        region = region_by_kind.get(vk)
        if region is not None:
            # 系统重构：用 min(scale_x, scale_y) 而非几何平均 scale_ratio，
            # 避免非均匀缩放（如 02 图 scale_x=50.2, scale_y=85.1）导致
            # 图纸空间聚类阈值被错误缩小，端点无法合并、杆件碎片化。
            sx_r, sy_r = region_scale_xy(region)
            min_scale = min(sx_r, sy_r) if sx_r and sy_r else region_scale_ratio(region)
            if min_scale and min_scale > 0:
                view_eps = eps / min_scale
        for node in _cluster_points(pts, eps=view_eps):
            all_clustered.append((vk, node))

    # 全局连续编号（保持旧版 node_Nxx 命名习惯）
    node_components: Dict[Tuple[str, int], Component] = {}
    view_counters: Dict[str, int] = {}
    global_i = 0
    for vk, node in all_clustered:
        global_i += 1
        nid = f"N{global_i:02d}"
        view_idx = view_counters.get(vk, 0)
        view_counters[vk] = view_idx + 1

        region = region_by_kind.get(vk)
        lx = ly = None
        z_level = None
        scale = 1.0
        if region is not None:
            scale_x, scale_y = region_scale_xy(region)
            lx, ly = _region_local(region, node["x"], node["y"])
            lx *= scale_x
            ly *= scale_y
            # CAD 通常 Y 向下；立面图的 view_y 映射到 Z 时需要翻转到「向上为正」。
            # 用 region.z_flip 显式声明（默认不翻，保持自画图/110kV 兼容）。
            # P3.15（JC2 泛化）：z_axis_up=true 表示图纸 Y 轴向上（国网
            # 35A2-JC2 立面塔底 y 小、塔顶 y 大）——view_y 已天然向上为正，
            # 既不做 ly=-ly，归一化层也用正向映射。与 z_flip 互斥。
            if region.get("z_flip") and not region.get("z_axis_up"):
                ly = -ly
            z_level = region.get("z_level")
        cid = f"node_{nid}"
        comp = Component(
            id=cid,
            name=f"节点 {nid}",
            kind="tower_node",
            source=SourceRef(
                SourceType.DRAWING, dxf_path,
                detail=f"端点聚类, view={vk}, handles={','.join(str(h) for h in node['handles'] if h is not None)}",
                confidence=0.9,
            ),
            properties={
                "node_id": nid,
                "x": round(node["x"], 2),
                "y": round(node["y"], 2),
                "z": None,  # Z 由跨视图合并（Phase 2）给出
                "solve_status": "partial",
                "view_type": vk,
                "view_x": round(lx, 2) if lx is not None else None,
                "view_y": round(ly, 2) if ly is not None else None,
                "z_level": z_level,
                "drawing_view": stem,
                "axis_origin": {
                    "x": "measured" if region is None else "measured",
                    "y": "measured" if region is None else "measured",
                    "z": "placeholder",
                },
            },
        )
        model.add_component(comp)
        node_components[(vk, view_idx)] = comp

    # 每视图的节点列表，供杆件端点就近绑定
    view_node_lists: Dict[str, List[Tuple[Component, Tuple[float, float]]]] = {}
    for (vk, _idx), comp in node_components.items():
        view_node_lists.setdefault(vk, []).append((comp, (comp.properties["x"], comp.properties["y"])))

    # ---- 3) 杆件编号：TEXT/MTEXT（含块内文本）空间关联 ----
    texts: List[Dict] = []
    for e in _flatten_modelspace_entities(msp):
        layer = getattr(e.dxf, "layer", "0")
        if not _layer_hit(layer, lm["text_layers"]):
            continue
        h = float(getattr(e.dxf, "height", 0.0) or 0.0)
        # 排除 2.5mm 及以下尺寸/下料长度文字（纯件号标注字高通常为 2.8~3.5mm）
        # 如果 layer 是 '0' 且字高明显为小尺寸标注 (h < 2.8)，跳过以避免将长度/切角值误贴为件号
        if layer == "0" and 0 < h < 2.75:
            continue
        if e.dxftype() == "TEXT":
            texts.append({
                "text": e.dxf.text,
                "insert": (e.dxf.insert.x, e.dxf.insert.y),
                "handle": e.dxf.handle,
                "rotation": float(getattr(e.dxf, "rotation", 0.0) or 0.0),
            })
        elif e.dxftype() == "MTEXT":
            texts.append({
                "text": e.text,
                "insert": (e.dxf.insert.x, e.dxf.insert.y),
                "handle": e.dxf.handle,
                "rotation": float(getattr(e.dxf, "rotation", 0.0) or 0.0),
            })

    # ---- 4) 杆件编号关联：bar -> 同视图内最近合法件号文字，一对一贪心 ----
    # P0：旧逻辑是 text -> 最近 bar，774 个文字只覆盖 ~318 根杆，方向反了。
    # 改为每根杆找最近合法文字；再按 (距离, 文字, 杆) 升序做一对一贪心，
    # 每个文字只贴一根杆、每根杆只收一个文字，避免多文字抢同一杆，
    # 也避免一改方向就 100% 瞎贴。允许同一件号出现在多个文字位置
    # （国网图同一编号可标多根杆件），重复件号由 r_no_duplicate_bar_id 报出。
    text_labels: List[Optional[str]] = []
    designation_tokens = _stem_designation_tokens(stem)
    for t in texts:
        text_labels.append(_extract_bar_label(t["text"], bar_id_re, designation_tokens))

    segs_by_view: Dict[str, List[int]] = {}
    for idx, seg in enumerate(bar_segments):
        segs_by_view.setdefault(seg["view_type"] or "_all", []).append(idx)

    # 预计算每个文字所属视图（与旧逻辑一致：无 region 时回退 _all）
    text_view: Dict[int, Optional[str]] = {}
    for ti, t in enumerate(texts):
        tx, ty = t["insert"]
        region = _find_region(tx, ty, regions) if regions else None
        text_view[ti] = _region_kind(region) if region else None

    # (距离, 杆段序号, 文字序号, 件号)：先收集候选对，再全局按距离升序贪心
    # 无视图规范 / fallback 视图时，文本可能落在杆段 bbox 之外；
    # 此时不回退到空集合，而是全图兜底配对（单视图图纸）。
    all_seg_indices = list(range(len(bar_segments)))
    pairs: List[Tuple[float, int, int, str]] = []
    for ti, label in enumerate(text_labels):
        if label is None:
            continue
        view = text_view[ti]
        cands = segs_by_view.get(view) or segs_by_view.get("_all") or []
        if not cands:
            cands = all_seg_indices
        tx, ty = texts[ti]["insert"]
        rot_deg = float(texts[ti].get("rotation") or 0.0)
        for si in cands:
            seg = bar_segments[si]
            d = _text_bar_match_distance(tx, ty, rot_deg, seg)
            if d < TEXT_SNAP:
                pairs.append((d, si, ti, label))
    pairs.sort(key=lambda x: x[0])

    seg_label: Dict[int, str] = {}
    seg_label_dist: Dict[int, float] = {}
    seg_label_text_idx: Dict[int, int] = {}
    used_texts: set = set()
    for d, si, ti, label in pairs:
        if si in seg_label or ti in used_texts:
            continue
        seg_label[si] = label
        seg_label_dist[si] = d
        seg_label_text_idx[si] = ti
        used_texts.add(ti)

    # 同一 handle 可能对应多条线段（LWPOLYLINE / 重复 INSERT），取距离最近的
    # 文字作为该 handle 的件号，所有同 handle 线段共用（保持旧版语义）。
    handle_best: Dict[str, Tuple[float, str]] = {}
    handle_best_text: Dict[str, int] = {}
    for si, label in seg_label.items():
        h = bar_segments[si]["handle"]
        d = seg_label_dist[si]
        if h not in handle_best or d < handle_best[h][0]:
            handle_best[h] = (d, label)
            handle_best_text[h] = seg_label_text_idx.get(si)
    handle_to_label: Dict[str, str] = {h: v[1] for h, v in handle_best.items()}
    handle_label_dist: Dict[str, float] = {h: v[0] for h, v in handle_best.items()}

    # ---- 4.4) 件号证据（bar_id_evidence）：记录「这根杆的件号来自哪条文字」----
    # 自包含、可追溯：sheet / 文字实体 / 原文 / 方法 / 距离 / 置信度。
    # distance_unit=drawing：与 label_distance 同口径（图面单位，随视图比例换算前）。
    handle_label_evidence: Dict[str, dict] = {}
    for h, (d, _label) in handle_best.items():
        ti = handle_best_text.get(h)
        if ti is None:
            continue
        t = texts[ti]
        handle_label_evidence[h] = {
            "sheet_id": stem,
            "label_component_id": f"text_{t.get('handle', ti)}",
            "text": str(t.get("text") or ""),
            "association_method": "nearest_text_same_view_greedy",
            "distance": round(float(d), 2),
            "distance_unit": "drawing",
            "confidence": 0.85,
        }

    # ---- 4.5) 截面型号关联（Phase 2）：截面文字 → 最近杆段，一对一贪心 ----
    # 与件号关联共用同一套 texts / 距离 / 贪心机制，但提取的是截面型号
    # （L40X3 / Q345L63X5 / -6X101），用于填充杆件 section（原硬编码 None）。
    section_labels: List[Optional[str]] = [
        _extract_section_label(t["text"]) for t in texts
    ]
    sec_pairs: List[Tuple[float, int, int, str]] = []
    for ti, label in enumerate(section_labels):
        if label is None:
            continue
        view = text_view[ti]
        cands = segs_by_view.get(view) or segs_by_view.get("_all") or []
        if not cands:
            cands = all_seg_indices
        tx, ty = texts[ti]["insert"]
        rot_deg = float(texts[ti].get("rotation") or 0.0)
        for si in cands:
            seg = bar_segments[si]
            d = _text_bar_match_distance(tx, ty, rot_deg, seg)
            if d < TEXT_SNAP:
                sec_pairs.append((d, si, ti, label))
    sec_pairs.sort(key=lambda x: x[0])
    seg_section: Dict[int, str] = {}
    used_sec_texts: set = set()
    for d, si, ti, label in sec_pairs:
        if si in seg_section or ti in used_sec_texts:
            continue
        seg_section[si] = label
        used_sec_texts.add(ti)
    # 同一 handle 多条线段取最近截面文字（与件号 handle_best 同语义）
    handle_section: Dict[str, Tuple[float, str]] = {}
    for si, label in seg_section.items():
        h = bar_segments[si]["handle"]
        d = seg_label_dist.get(si, float("inf"))
        # 截面文字距离单独记录（seg_label_dist 是件号距离，不可混用）；
        # 这里用 sec_pairs 中对应距离近似即可，直接用贪心序（已按距离升序）。
        if h not in handle_section or d < handle_section[h][0]:
            handle_section[h] = (d, label)
    handle_to_section: Dict[str, str] = {h: v[1] for h, v in handle_section.items()}

    # ---- 5) 杆件 → tower_bar 组件 ----
    # 先按「(view_type, bar_id)」收集杆段，供重复件号消歧报告使用。
    dup_segments: Dict[Tuple[str, str], List[Tuple[int, float]]] = {}
    for si, label in seg_label.items():
        vk = bar_segments[si]["view_type"]
        dup_segments.setdefault((vk, label), []).append((si, seg_label_dist[si]))

    for i, seg in enumerate(bar_segments, start=1):
        vk = seg["view_type"]
        handle = seg["handle"]
        bar_id = handle_to_label.get(handle, f"UNLABELED_{handle}")
        conf = 0.85 if handle in handle_to_label else 0.3

        scale = float(seg.get("scale_ratio", region_scale_ratio(seg.get("region")) or 1.0))
        length = _dist(seg["start"], seg["end"]) * scale
        # 处理同名编号（如多个视图同名杆件）：用序号后缀避免组件 ID 冲突
        cid = f"bar_{bar_id}_{vk}"
        if cid in model.components:
            cid = f"bar_{bar_id}_{vk}_{i}"
        from_nid = _find_node(seg["start"], view_node_lists.get(vk, []))
        to_nid = _find_node(seg["end"], view_node_lists.get(vk, []))

        # 重复件号消歧：同一视图内「一號多杆」时，标出距离最近的 primary，
        # 其余仍保留原 bar_id 并打 dup 标记，交由 r_no_duplicate_bar_id 判定，
        # 不删除规则、不悄悄改号。
        dup_members = dup_segments.get((vk, bar_id))
        is_dup = bool(dup_members and len(dup_members) > 1)
        is_primary = False
        if is_dup:
            dup_members_sorted = sorted(dup_members, key=lambda x: x[1])
            is_primary = (dup_members_sorted[0][0] == (i - 1))

        properties = {
            "bar_id": bar_id,
            "view_type": vk,
            "length_mm": round(length, 2),
            "section": handle_to_section.get(handle),  # Phase 2：截面文字空间关联
            "from_node": f"node_{from_nid}",
            "to_node": f"node_{to_nid}",
            "layer": seg["layer"],
            # 阶段2.6：ezdxf 直接识别出的杆件显式标记 recognized（非 derived/mirrored），
            # 否则单段 2D 评测时 bars_from_model_2d(mode=recognition) 因缺
            # geometry_class 而 fail-closed 全部排除。
            "geometry_class": str(seg.get("geometry_class") or "recognized"),
            "drawing_view": stem,
            "source_file": stem,
            "geometry_origin": str(seg.get("geometry_origin") or "dxf_geom"),
            # 阶段 4.1/4.2：每根 recognized 杆件必须携带自包含证据引用，
            # 不依赖被后续四面展开删除的二维组件 ID。字段稳定、可解析：
            #   sheet_id / view_id / view_type / source_component_id /
            #   source_reference / geometry_origin / confidence
            # source_component_id 用 DXF 实体 handle（稳定、展开后不变化），
            # 而非模型组件 ID（展开会重命名 4f_...，导致悬空）。
            "projection_refs": [{
                "sheet_id": stem,
                "view_id": f"{stem}__{vk}",
                "view_type": vk,
                # handle 缺失时用 sheet:// 稳定 URI（外部自包含引用），
                # 绝不写 str(None)="None"——否则 validate_references 会把它
                # 误判为悬空的组件内引用（正确性修复）。
                "source_component_id": str(handle) if handle is not None
                                       else f"sheet://{stem}#{vk}",
                "source_reference": dxf_path,
                "region_id": seg.get("region"),
                "geometry_origin": str(seg.get("geometry_origin") or "dxf_geom"),
                "confidence": conf,
                "provider": "ezdxf",
                "model": None,
                "prompt_sha": None,
                "call_id": None,
            }],
        }
        if seg.get("source_extractor"):
            properties["source_extractor"] = seg["source_extractor"]
        if seg.get("evidence_status"):
            properties["evidence_status"] = seg["evidence_status"]
        if handle in handle_label_dist:
            properties["label_distance"] = round(handle_label_dist[handle], 2)
        # 阶段4.4：有件号的杆必须带 bar_id_evidence（件号从哪条文字来）
        _ev = handle_label_evidence.get(handle)
        if _ev is not None:
            properties["bar_id_evidence"] = [dict(_ev)]
        if is_dup:
            properties["bar_id_dup"] = True
            properties["bar_id_primary"] = is_primary

        model.add_component(Component(
            id=cid,
            name=f"杆件 {bar_id}（{vk}）",
            kind="tower_bar",
            source=SourceRef(
                SourceType.DRAWING, dxf_path,
                detail=f"handle={seg['handle']}, layer={seg['layer']}, view={vk}",
                confidence=conf,
            ),
            properties=properties,
        ))

    # ---- 6) DIMENSION → measured Dimension（B4）----
    _add_dimensions_from_dxf_entities(model, msp, lm, dxf_path, bar_id_re)

    # ---- 6b) 材料表件号列提取（S1c 2026-09-06）----
    # 国网总装图的材料表（图纸右侧表格）里每行都有件号文字，但离塔身杆件
    # 远超 TEXT_SNAP，贴不上任何杆——这些文字此前随空间关联静默丢弃。
    # 纯图纸结构证据判定（不查 BOM）：数字文字按 x 聚簇成列，列右侧
    # 8~20 图面单位内存在同规模的截面型号列（Q345L70X5 / L40X3 /
    # -6X260），即材料表「件号|截面|长度|数量」的列布局。命中列的
    # 未关联件号收进 orphan_label_ids 登记簿（几何不动、不贴杆），
    # 与短斜材过滤/边界缝合的登记簿同语义：几何清噪，A1 证据不丢。
    # 实测（35A1-JC1 纯矢量）：02 册 101-104/127-149、04 册 303/335/
    # 337/672/871/953、05 册 402 等件号仅在材料表出现（立面上无标注）。
    table_label_report = _extract_material_table_labels(
        texts, used_texts, designation_tokens)
    df = model.components["drawing_file"]
    if table_label_report["labels"]:
        existing_orphans = [
            str(x) for x in (df.properties.get("orphan_label_ids") or [])]
        for _lab in table_label_report["labels"]:
            if _lab not in existing_orphans:
                existing_orphans.append(_lab)
        df.properties["orphan_label_ids"] = existing_orphans
        df.properties["material_table_labels"] = table_label_report

    # 解析率统计（不改变对象语义，写入 drawing_file 供报告/CLI 使用）
    bars = [c for c in model.components.values() if c.kind == "tower_bar"]
    labeled = [c for c in bars if not str(c.properties.get("bar_id", "")).startswith("UNLABELED")]
    df.properties["bar_count"] = len(bars)
    df.properties["labeled_count"] = len(labeled)
    df.properties["association_rate"] = round(len(labeled) / len(bars), 4) if bars else 0.0
    if min_bar_len > 0:
        df.properties["min_bar_length_mm"] = min_bar_len
    df.properties["degenerate_bar_count"] = sum(
        1 for c in bars
        if c.properties.get("from_node") == c.properties.get("to_node")
    )

    # P1 重复件号报告：「一號多杆」供人工复核，不删规则、不凑 passed。
    # 同视图内按 (view_type, bar_id) 聚合，距离最近的杆标 bar_id_primary=true。
    by_view_label: Dict[Tuple[str, str], List[dict]] = {}
    for c in bars:
        bid = str(c.properties.get("bar_id", ""))
        if bid.startswith("UNLABELED"):
            continue
        by_view_label.setdefault((c.properties.get("view_type", ""), bid), []).append({
            "id": c.id,
            "label_distance": c.properties.get("label_distance"),
            "primary": bool(c.properties.get("bar_id_primary")),
        })

    duplicate_groups = []
    for (vk, bid), members in sorted(by_view_label.items()):
        if len(members) > 1:
            members_sorted = sorted(members, key=lambda m: (m["label_distance"] is None, m["label_distance"]))
            duplicate_groups.append({
                "bar_id": bid,
                "view_type": vk,
                "count": len(members),
                "primary": members_sorted[0]["id"],
                "bar_ids": [m["id"] for m in members_sorted],
            })
    df.properties["duplicate_bar_id_groups"] = len(duplicate_groups)
    df.properties["duplicate_bar_id_detail"] = duplicate_groups[:200]

    # P0-2：记录视图模式，明确区分「单立面 2D-only」与「多视图可合 3D」。
    # 国网 02 总装图只有 front 一个立面（无 side/plan/section），无法解出真 3D；
    # 此时 view_mode=single_facade，只能做 2D + 件号率，3D 需立面/平面分文件
    # （多 DWG 各自带 view_regions）走 merge_view_coordinates。
    # 有 front+side(+section) 或多 plan 时 view_mode=multi_view，可参与 3D 合并。
    view_kinds = {seg["view_type"] for seg in bar_segments
                  if seg.get("view_type") in ("front", "side", "section", "plan", "elevation")}
    merge_capable = bool(view_kinds & {"front", "side", "section", "elevation"}) and (
        ("side" in view_kinds) or ("section" in view_kinds)
    )
    if not view_kinds:
        view_mode = "no_view"
    elif merge_capable:
        view_mode = "multi_view"
    else:
        view_mode = "single_facade"
    df.properties["view_mode"] = view_mode
    df.properties["view_kinds"] = sorted(view_kinds)

    # M3：节点大样（03+ 图纸）→ 节点板 + 螺栓群（Gap 2 主链接入）
    # 注意：仅当 overlay 声明了 kind="detail" 区域才提取。04-07 虽按文件名
    # 判为 node_detail，但 overlay 已声明为 front 立面（无 detail 区域），
    # 不得把 front 区域（含材料表）当大样提取（2026-08-31 假 bolt_group 事故：
    # BOM 螺栓条目被当孔位标注，产生 113 个必然失败的假验算规则）。
    if drawing_kind["kind"] == "node_detail":
        detail_regions = [r for r in regions if r.get("kind") == "detail"]
        if detail_regions:
            from .tower_detail import extract_detail_connections

            extract_detail_connections(
                model, msp, detail_regions, stem, dxf_path, overlay=layer_map_path,
            )

    # ---- 7) 证据层（P0 架构对齐，2026-09-03 审计）----
    # 件号文字关联提升为 observation 组件（稳定 ID + confidence），
    # 杆件 DAG 登记到其证据观测（改标注文字 → 杆 stale）。
    # 纯增量：新组件 kind=observation，不触碰既有杆/节点计数与评测。
    try:
        from .tower_spec import load_tower_spec as _lts_ev
        _ev_enabled = bool(
            (load_tower_spec(layer_map_path) or {}).get("evidence_layer", True)
        ) if _lts_ev else True
    except Exception:
        _ev_enabled = True
    if _ev_enabled:
        from .evidence_layer import (
            register_label_observations,
            register_dim_observations,
            depend_on_observations,
            label_observation_id,
            observation_census,
        )
        register_label_observations(
            model, stem, dxf_path, handle_label_evidence)
        register_dim_observations(
            model, stem, dxf_path, _dim_samples, context="scale_calibration")
        # 杆 → 证据观测：观测 ID 按文字实体（label_component_id）
        # 构造，杆的 bar_id_evidence 引用同一文字 → 直接登记。
        for _bc in model.components.values():
            if _bc.kind != "tower_bar":
                continue
            for _e in (_bc.properties.get("bar_id_evidence") or []):
                _oid = label_observation_id(
                    stem, _e.get("label_component_id"))
                if _oid in model.components:
                    depend_on_observations(model, _bc.id, [_oid])
        # P1-1（2026-09-03 审计）：DIM 观测 → 标定结果的 DAG 入边。
        # drawing_file 组件承载 scale_calibration（regions 标定）与
        # dimension_beat_anchors（节拍锚定）——两者都由 DIM 样本推导，
        # 改任一标注应传播 stale 到标定结果。观测 ID 形如
        # obs_{stem}_dim_{handle}，与 register_dim_observations 一致。
        _dim_obs_ids = [
            cid for cid in model.components
            if cid.startswith(f"obs_{stem}_dim_")
            and (model.components[cid].properties.get("observation_kind")
                 == "dim_sample")
        ]
        if _dim_obs_ids:
            depend_on_observations(
                model, "drawing_file", _dim_obs_ids)
        _df_ev = model.components.get("drawing_file")
        if _df_ev is not None:
            # P0-2（2026-09-03 审计）：merge 而非整体赋值——tower_symmetry
            # 稍后也要写 evidence_layer.hypotheses，赋值会互相覆盖，
            # 最终 model.json 只剩最后一个写者的键。
            _df_ev.properties.setdefault("evidence_layer", {}).update(
                {"observations": observation_census(model)}
            )
            # Bug B（2026-09-03）：scale 标定被 overlay 关闭时留痕——
            # 观测照常登记，但「DIM 不参与 scale 覆盖」这个事实必须在
            # 普查里可见，不许静默消失。
            if _dim_calib_skipped_reason:
                _df_ev.properties.setdefault("evidence_layer", {}).update(
                    {"dim_scale_calibration_skipped_reason":
                     _dim_calib_skipped_reason}
                )

    return model


def _add_dimensions_from_dxf_entities(
    model: EngineeringModel,
    msp,
    lm: dict,
    dxf_path: str,
    bar_id_re=None,
) -> None:
    """把 DIMENSION 实体写入模型 dimensions（B4）。

    value 优先取图面文字（国网图常人为覆盖），否则取自动测量值；
    origin=measured，source 保留 handle + layer，绝不编造。
    """
    dim_count = 0
    for e in msp.query("DIMENSION"):
        layer = getattr(e.dxf, "layer", "0")
        if not _layer_hit(layer, lm["dim_layers"] + lm["bar_layers"]):
            continue
        value, unit = _dimension_value(e)
        handle = getattr(e.dxf, "handle", str(dim_count))
        did = f"dim_dxf_{handle}"
        if did in model.dimensions:
            did = f"dim_dxf_{handle}_{dim_count}"
        model.add_dimension(Dimension(
            id=did,
            name=f"DIMENSION #{handle}",
            value=value,
            unit=unit,
            origin=DimensionOrigin.MEASURED,
            source=SourceRef(SourceType.DRAWING, dxf_path,
                             detail=f"DIMENSION handle={handle}, layer={layer}",
                             confidence=0.9),
        ))
        dim_count += 1
def _find_node(point: Tuple[float, float], nodes: List[Tuple[Component, Tuple[float, float]]]) -> str:
    """找点最近的聚类节点 ID（限定同视图节点列表）。"""
    best, best_d = "N00", float("inf")
    for comp, (x, y) in nodes:
        d = _dist(point, (x, y))
        if d < best_d:
            best_d = d
            best = comp.properties["node_id"]
    return best


def layer_usage_report(dxf_path: str | Path, layer_map_path: Optional[str | Path] = None) -> dict:
    """报告 DXF 图层使用情况（P1-4 外部试点解析率报告用）。

    返回：
        * total_entities: 模型空间实体总数
        * recognized_bar_layers / recognized_text_layers
        * unidentified_layers: 有实体但不属于任何已知图层组的图层名列表
        * entity_count_by_layer: 每个图层实体数（按图层名排序）
    绝不编造通过；未知图层原样列出，供换 overlay 配置参考。
    """
    import ezdxf

    dxf_path = str(dxf_path)
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    known = {
        "bar_layers": set(layer_names("bar_layers", DEFAULT_LAYER_MAP["bar_layers"], overlay=layer_map_path)),
        "node_layers": set(layer_names("node_layers", DEFAULT_LAYER_MAP["node_layers"], overlay=layer_map_path)),
        "dim_layers": set(layer_names("dim_layers", DEFAULT_LAYER_MAP["dim_layers"], overlay=layer_map_path)),
        "text_layers": set(layer_names("text_layers", DEFAULT_LAYER_MAP["text_layers"], overlay=layer_map_path)),
    }
    norm_known = set()
    for names in known.values():
        for n in names:
            norm_known.add(n.strip().lower())

    entity_count_by_layer: Dict[str, int] = {}
    for e in msp:
        layer = getattr(e.dxf, "layer", "0")
        entity_count_by_layer[layer] = entity_count_by_layer.get(layer, 0) + 1

    unidentified = sorted(
        layer for layer in entity_count_by_layer
        if layer.strip().lower() not in norm_known and layer != "0"
    )
    recognized_bar = sorted(
        layer for layer in entity_count_by_layer
        if _layer_hit(layer, sorted(known["bar_layers"]))
    )
    recognized_text = sorted(
        layer for layer in entity_count_by_layer
        if _layer_hit(layer, sorted(known["text_layers"]))
    )
    return {
        "total_entities": sum(entity_count_by_layer.values()),
        "recognized_bar_layers": recognized_bar,
        "recognized_text_layers": recognized_text,
        "unidentified_layers": unidentified,
        "entity_count_by_layer": dict(sorted(entity_count_by_layer.items())),
    }
