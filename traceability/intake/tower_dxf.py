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
    collinear_merge_config,
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
# 螺栓标注 M16X40 / 1M16X40 / 2M16X50 等（避免把尺寸/材质/螺栓当件号贴杆）。
_BAR_ID_EXCLUDE_RES = [
    re.compile(r"Q\s?(?:235|345|420)", re.IGNORECASE),          # 材质
    re.compile(r"L\s?\d{1,3}\s*[Xx×*]\s*\d+", re.IGNORECASE),  # 截面
    re.compile(r"(?:\d+)?M\s?\d{1,3}\s*[Xx×*]\s*\d+", re.IGNORECASE),  # 螺栓
]


def classify_drawing_kind(stem: str) -> dict:
    """按文件名规则分流国网/外图图纸类型（B2）。

    返回：
        * kind: title_block / bom / assembly / node_detail / drawing
        * parse_bars: 是否进入杆件解析
        * reason: 分流依据
    """
    s = stem.lower()
    # 国网命名习惯：<塔型>-<序号>[-<分页>]，如 35a1-jc1-00-1 / 35a1-jc1-02 / 35c2-sjg1-ml
    if re.search(r"[-_]0{2}(?:[-_.]|$)", s) or "图签" in s:
        return {"kind": "title_block", "parse_bars": False,
                "reason": "文件名 -00-* 判定为图签页"}
    if s.endswith("-ml") or s.endswith("_ml") or s == "ml" or "-ml-" in s or "-ml." in s:
        return {"kind": "bom", "parse_bars": False,
                "reason": "文件名 *-ML 判定为材料明细表"}
    # 02 总装、03+ 节点大样都属于可解析的杆件图
    if re.search(r"[-_]0?2(?:[-_.]|$)", s):
        return {"kind": "assembly", "parse_bars": True,
                "reason": "文件名 -02 判定为总装图"}
    if re.search(r"[-_]0?[3-9]\d*(?:[-_.]|$)", s):
        return {"kind": "node_detail", "parse_bars": True,
                "reason": "文件名 03+ 判定为节点/分段图"}
    if s.startswith("00") or s.startswith("02"):
        return {"kind": "assembly" if s.startswith("02") else "title_block",
                "parse_bars": s.startswith("02"),
                "reason": "文件名前导序号判定"}
    return {"kind": "drawing", "parse_bars": True, "reason": "默认按杆件图解析"}


def resolve_drawing_kind(stem: str, overlay: Optional[str | Path | dict] = None) -> dict:
    """按文件名分流，并允许 overlay view_regions 覆盖 BOM/图签等跳过规则（M3）。"""
    kind = classify_drawing_kind(stem)
    if kind["parse_bars"]:
        return kind
    for region in view_regions(stem, overlay=overlay):
        axes = list(region.get("axes") or [])
        if not axes:
            continue
        vk = region.get("kind", "drawing")
        return {
            "kind": vk if vk in ("plan", "front", "side", "section", "elevation", "drawing") else "drawing",
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

    for i in range(len(segs)):
        if used[i]:
            continue
        # 以 i 为起点做链式延伸
        chain = [segs[i]]
        used[i] = True
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
        end = (origin[0] + ux * t1, origin[1] + uy * t1)
        merged.append({
            "handle": chain[0]["handle"],  # 保 str 兼容下游 handle 索引
            "start": start,
            "end": end,
            "layer": chain[0]["layer"],
            "fragments": len(chain),
            "fragments_handles": [s["handle"] for s in chain],
        })
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


def _extract_bar_label(text: str, bar_id_re: re.Pattern) -> Optional[str]:
    """从一条 TEXT/MTEXT 中提取合法件号，否则返回 None。

    P1：先排除材质（Q235/Q345/Q420）、截面（L40X3 等）、螺栓
    （M16X40 / 1M16X40 / 2M16X50 等），再做件号正则匹配。
    """
    if not text:
        return None
    for excl in _BAR_ID_EXCLUDE_RES:
        if excl.search(text):
            return None
    m = bar_id_re.search(text)
    return m.group(1) if m else None


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
    regions = view_regions(stem, overlay=layer_map_path)
    min_bar_len = min_bar_length_mm(stem, overlay=layer_map_path)
    coll_cfg = collinear_merge_config(stem, overlay=layer_map_path)
    if eps == EPS:
        eps = cluster_eps_mm(stem, overlay=layer_map_path)

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    model = EngineeringModel(name=f"tower-{stem}")

    # 图纸文件上下文（带 drawing_kind，供 B2 分流与验收）
    model.add_component(Component(
        id="drawing_file",
        name=stem,
        kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, dxf_path, confidence=1.0),
        properties={
            "path": dxf_path,
            "drawing_kind": drawing_kind["kind"],
            "drawing_view": stem,
            "parse_bars": drawing_kind["parse_bars"],
            "kind_reason": drawing_kind["reason"],
        },
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
    if regions:
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
        # 合并后再按整根杆长过滤短杆（碎段本身短，min_bar_len 只在合并后生效）
        if min_bar_len > 0:
            bar_segments = [
                s for s in bar_segments
                if _dist(s["start"], s["end"]) * (s.get("scale_ratio") or 1.0) >= min_bar_len
            ]

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
            scale = region_scale_ratio(region)
            if scale and scale > 0:
                view_eps = eps / scale
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
            if region.get("z_flip"):
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
        if e.dxftype() == "TEXT":
            texts.append({
                "text": e.dxf.text,
                "insert": (e.dxf.insert.x, e.dxf.insert.y),
                "handle": e.dxf.handle,
            })
        elif e.dxftype() == "MTEXT":
            texts.append({
                "text": e.text,
                "insert": (e.dxf.insert.x, e.dxf.insert.y),
                "handle": e.dxf.handle,
            })

    # ---- 4) 杆件编号关联：bar -> 同视图内最近合法件号文字，一对一贪心 ----
    # P0：旧逻辑是 text -> 最近 bar，774 个文字只覆盖 ~318 根杆，方向反了。
    # 改为每根杆找最近合法文字；再按 (距离, 文字, 杆) 升序做一对一贪心，
    # 每个文字只贴一根杆、每根杆只收一个文字，避免多文字抢同一杆，
    # 也避免一改方向就 100% 瞎贴。允许同一件号出现在多个文字位置
    # （国网图同一编号可标多根杆件），重复件号由 r_no_duplicate_bar_id 报出。
    text_labels: List[Optional[str]] = []
    for t in texts:
        text_labels.append(_extract_bar_label(t["text"], bar_id_re))

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
        for si in cands:
            seg = bar_segments[si]
            d = _point_mid_dist((tx, ty), seg["start"], seg["end"])
            if d < TEXT_SNAP:
                pairs.append((d, si, ti, label))
    pairs.sort(key=lambda x: x[0])

    seg_label: Dict[int, str] = {}
    seg_label_dist: Dict[int, float] = {}
    used_texts: set = set()
    for d, si, ti, label in pairs:
        if si in seg_label or ti in used_texts:
            continue
        seg_label[si] = label
        seg_label_dist[si] = d
        used_texts.add(ti)

    # 同一 handle 可能对应多条线段（LWPOLYLINE / 重复 INSERT），取距离最近的
    # 文字作为该 handle 的件号，所有同 handle 线段共用（保持旧版语义）。
    handle_best: Dict[str, Tuple[float, str]] = {}
    for si, label in seg_label.items():
        h = bar_segments[si]["handle"]
        d = seg_label_dist[si]
        if h not in handle_best or d < handle_best[h][0]:
            handle_best[h] = (d, label)
    handle_to_label: Dict[str, str] = {h: v[1] for h, v in handle_best.items()}
    handle_label_dist: Dict[str, float] = {h: v[0] for h, v in handle_best.items()}

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
            "section": None,  # 由 BOM 交叉核验（Phase 2）填充
            "from_node": f"node_{from_nid}",
            "to_node": f"node_{to_nid}",
            "layer": seg["layer"],
            "drawing_view": stem,
        }
        if handle in handle_label_dist:
            properties["label_distance"] = round(handle_label_dist[handle], 2)
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

    # 解析率统计（不改变对象语义，写入 drawing_file 供报告/CLI 使用）
    bars = [c for c in model.components.values() if c.kind == "tower_bar"]
    labeled = [c for c in bars if not str(c.properties.get("bar_id", "")).startswith("UNLABELED")]
    df = model.components["drawing_file"]
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
    if drawing_kind["kind"] == "node_detail":
        detail_regions = [r for r in regions if r.get("kind") == "detail"] or regions
        from .tower_detail import extract_detail_connections

        extract_detail_connections(
            model, msp, detail_regions, stem, dxf_path, overlay=layer_map_path,
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
