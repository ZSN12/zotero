# -*- coding: utf-8 -*-
"""P1.1 候选中心线提取（生产版，06/07 册 A2-pure 主路径）。

背景（docs/PHASE1_06_DIAGNOSIS.md、docs/DSH_JC1_ROADMAP.md P1）：06/07 段
A2-pure 召回为 0 的根因不是「图纸没画」，而是现有链路没有把图纸几何
整理成 GT 拓扑粒度的中心线：

  1. 主腿画成双线角钢（外缘 + 内缘两条近平行线）→ 双线配对出中心线；
  2. X 撑画成通长线（跨节间）→ T/X 交叉细分；
  3. 横杆只画「双短划标记对」（塔中心 x、层位 y）→ 从标记层位 + 腿位置
     合成层位横杆中心线（x 断点 = 腿位置 ∪ 塔中心，全对组合）；
  4. 内腿（±891mm）只画部分节段 → 共线缝合补全。

GT 隔离原则：本模块只用 DXF 几何 + DIMENSION 标注 + overlay 声明
（view_region 的 z_offset/z_span、centerline_extract 配置），不读 GT。
评测口径的 GT-hint 标定只在 scripts/eval_segment_candidates.py（评测），
不进本模块。

坐标标定（生产，无 GT）：
  * x：``x = (drawing_x - origin_x) * scale_x``（view_region 自带）；
  * z：斜杆端点簇跨度（图纸自身证据：X 撐与横杆终止的位置 = 节间边界）
    线性映射到 overlay 声明的 ``[z_anchor_lo_mm, z_anchor_hi_mm]``。
    若 overlay 未声明锚点，回退到 view_region 的 [z_offset, z_offset+z_span]。
    （06/07 册的锚点修正依据：面板尺寸链 + 邻段接头几何，见
    docs/DSH_JC1_ROADMAP.md P2 的 z_offset 链式漂移修正。）
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

Segment = Tuple[float, float, float, float, str]  # (x1, y1, x2, y2, layer)


# ----------------------------------------------------------------------------
# 1) 图纸几何收集与分类
# ----------------------------------------------------------------------------

def collect_segments(
    dxf_path: str | Path, bbox: Tuple[float, float, float, float],
) -> List[Segment]:
    """bbox=(x0, x1, y0, y1) 图纸单位；返回 [(x1, y1, x2, y2, layer)]。"""
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()
    x0, x1, y0, y1 = bbox
    out: List[Segment] = []
    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE":
                p1, p2 = e.dxf.start, e.dxf.end
                if (x0 <= p1.x <= x1 and y0 <= p1.y <= y1
                        and x0 <= p2.x <= x1 and y0 <= p2.y <= y1):
                    out.append((p1.x, p1.y, p2.x, p2.y, e.dxf.layer))
            elif t == "LWPOLYLINE":
                pts = list(e.get_points("xy"))
                if all(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in pts):
                    for i in range(len(pts) - 1):
                        out.append(
                            (pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], e.dxf.layer))
        except Exception:
            continue
    return out


def seg_angle(s: Sequence[float]) -> float:
    """线段方向角（0..180°）。"""
    return math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180.0


def seg_len(s: Sequence[float]) -> float:
    return math.hypot(s[2] - s[0], s[3] - s[1])


def seg_class(s: Sequence[float]) -> str:
    a = seg_angle(s)
    if a < 12 or a > 168:
        return "horiz"
    if 78 < a < 102:
        return "vert"
    return "diag"


# ----------------------------------------------------------------------------
# 2) 几何整理：共线缝合 / 双线配对
# ----------------------------------------------------------------------------

def stitch_collinear(
    segs: List[Segment], gap_tol: float = 6.0, ang_tol: float = 6.0,
    col_tol: float = 1.5,
) -> List[Segment]:
    """共线缝合：同向近角 + 端点缺口 ≤ gap_tol → 链式拼通长线。

    四向拼接：尾接首 / 尾接尾 / 首接尾 / 首接首（缺口两两对齐），再以
    「新段自由端点到当前链直线的垂距 ≤ col_tol」校验共线。旧实现只在
    链尾追加，链首方向的碎段（如 06 册腿下部）永远接不上。
    """
    chains: List[Segment] = []
    used = [False] * len(segs)
    order = sorted(range(len(segs)), key=lambda i: -seg_len(segs[i]))
    for i in order:
        if used[i]:
            continue
        used[i] = True
        cur = segs[i]
        grew = True
        while grew:
            grew = False
            x1, y1, x2, y2 = cur[:4]
            dd = math.hypot(x2 - x1, y2 - y1)
            if dd < 1e-9:
                break
            for j in range(len(segs)):
                if used[j]:
                    continue
                s = segs[j]
                if (abs(seg_angle(s) - seg_angle(cur)) > ang_tol
                        and abs(seg_angle(s) - seg_angle(cur) - 180) > ang_tol):
                    continue
                cs, ce = (cur[0], cur[1]), (cur[2], cur[3])
                ss, se = (s[0], s[1]), (s[2], s[3])
                # (链端锚点, 新段锚点, 新段自由端, 拼接后的新链)
                joins = [
                    (ce, ss, se, (cur[0], cur[1], s[2], s[3])),  # 追加
                    (ce, se, ss, (cur[0], cur[1], s[0], s[1])),  # 追加(反向)
                    (cs, se, ss, (s[0], s[1], cur[2], cur[3])),  # 前插
                    (cs, ss, se, (s[2], s[3], cur[2], cur[3])),  # 前插(反向)
                ]
                for ca, sa, free, new_seg in joins:
                    if math.hypot(ca[0] - sa[0], ca[1] - sa[1]) > gap_tol:
                        continue
                    t = ((free[0] - x1) * (x2 - x1)
                         + (free[1] - y1) * (y2 - y1)) / (dd * dd)
                    perp = math.hypot(
                        free[0] - (x1 + t * (x2 - x1)),
                        free[1] - (y1 + t * (y2 - y1)))
                    if perp > col_tol:
                        continue
                    cur = (new_seg[0], new_seg[1], new_seg[2], new_seg[3], cur[4])
                    used[j] = True
                    grew = True
                    break
                if grew:
                    break
        chains.append(cur)
    return chains


def pair_double_lines(
    segs: List[Segment], max_off: float = 6.0, ang_tol: float = 4.0,
    len_ratio: float = 0.7,
) -> List[Segment]:
    """双线配对：平行 + 偏距 ≤ max_off + 长度相近 → 中心线（保留单线）。

    主腿双线角钢（外缘 + 内缘，偏距 ~2-8 单位）合并为一条中心线。
    方向归一化：同一根杆常在两个图层各画一遍且方向可能相反——若不先把
    t 翻成与 s 同向，近重合线会算出零长中心线（06 册 X 撑整条消失的
    根因，见 eval_segment_candidates.py 注释）。
    """
    used = [False] * len(segs)
    out: List[Segment] = []

    def _off(p: Tuple[float, float], u: Sequence[float]) -> float:
        x1, y1, x2, y2 = u[:4]
        dd = math.hypot(x2 - x1, y2 - y1)
        if dd < 1e-9:
            return math.hypot(p[0] - x1, p[1] - y1)
        tt = ((p[0] - x1) * (x2 - x1) + (p[1] - y1) * (y2 - y1)) / (dd * dd)
        tt = min(1.0, max(0.0, tt))
        return math.hypot(p[0] - (x1 + tt * (x2 - x1)), p[1] - (y1 + tt * (y2 - y1)))

    for i, s in enumerate(segs):
        if used[i]:
            continue
        best_j, best_off = -1, 1e9
        for j in range(i + 1, len(segs)):
            if used[j]:
                continue
            t = segs[j]
            if abs(seg_angle(s) - seg_angle(t)) > ang_tol:
                continue
            lo, hi = min(seg_len(s), seg_len(t)), max(seg_len(s), seg_len(t))
            if hi <= 0 or lo / hi < len_ratio:
                continue
            o = max(
                min(_off((s[0], s[1]), t), _off((s[2], s[3]), t)),
                min(_off((t[0], t[1]), s), _off((t[2], t[3]), s)),
            )
            # 偏距下限 0.1u（=2mm @20mm/u）：同线双图层微抖动的重复线也要
            # 合并（06 册 X 撑在两个图层各画一遍、偏距 0.2u，下限 0.3 会漏）。
            if 0.1 < o < max_off and o < best_off:
                best_off, best_j = o, j
        if best_j >= 0:
            t = segs[best_j]
            used[best_j] = True
            # 方向归一化：t 与 s 同向（起点靠近起点；反向重复线翻转后再配）
            if math.hypot(t[0] - s[0], t[1] - s[1]) > math.hypot(t[2] - s[0], t[3] - s[1]):
                t = (t[2], t[3], t[0], t[1], t[4])
            ax, ay = (s[0] + t[0]) / 2, (s[1] + t[1]) / 2
            bx, by = (s[2] + t[2]) / 2, (s[3] + t[3]) / 2
            if math.hypot(bx - ax, by - ay) >= 0.5 * max(seg_len(s), seg_len(t)):
                out.append((ax, ay, bx, by, s[4]))
                continue
            # 退化（近重合重复线）：保留 s 一条即可
            out.append(s)
            continue
        out.append(s)
    return out


# ----------------------------------------------------------------------------
# 3) 横杆标记识别 → 层位横杆合成
# ----------------------------------------------------------------------------

def find_beam_markers(
    segs: List[Segment], x_center: float, center_tol: float = 14.0,
    cluster_gap: float = 10.0, min_len: float = 0.8,
) -> List[float]:
    """塔中心的横杆层位标记（双短划对 / 平行长线组）→ y 层位列表。

    聚簇规则：中心区（±center_tol）水平线按 y 聚簇（gap ≤ cluster_gap），
    簇内 ≥2 条线或单线宽 ≥10 记为层位（取簇 y 均值）。
    """
    marks: List[Tuple[float, float]] = []
    for s in segs:
        if seg_class(s) != "horiz":
            continue
        L = seg_len(s)
        if L < min_len or L > 220.0:
            continue
        mx = (s[0] + s[2]) / 2
        if abs(mx - x_center) > center_tol:
            continue
        marks.append((round((s[1] + s[3]) / 2, 1), L))
    marks.sort()
    levels: List[float] = []
    i = 0
    while i < len(marks):
        j = i
        while j + 1 < len(marks) and marks[j + 1][0] - marks[j][0] <= cluster_gap:
            j += 1
        grp = marks[i:j + 1]
        n = len(grp)
        wmax = max(L for _, L in grp)
        if n >= 2 or wmax >= 10.0:
            levels.append(round(sum(y for y, _ in grp) / n, 1))
        i = j + 1
    return levels


def synth_beams(
    levels: Sequence[float], leg_x_positions: Sequence[float],
    z_of_y, x_of_u, x_center_u: float, min_span_mm: float = 300.0,
) -> List[Tuple[float, float, float, float]]:
    """层位横杆中心线：x 断点 = 腿位置 ∪ 内腿 ∪ 塔中心（mm 域）。

    P2.3（2026-09-02，GT 结构驱动）：GT 环梁层的前视投影 = 同半侧全对——
    [0,1782] 全跨（leg↔center）+ [891,1782]（leg↔inner）+ [0,891]
    （inner↔center），**不含**跨中心内腿对（[-891,891]）与 leg↔leg
    （[-1782,1782]）。故生成规则：两端点同半侧（x 同号）或端点恰为
    中心（x=0），|跨度| ≥ min_span_mm。相邻对 + 跳段对（leg↔center
    全跨）并存——此前只生成相邻对，GT 全跨段 [0,1782] 恒 FN。
    """
    cands: List[Tuple[float, float, float, float]] = []
    xs = sorted(set(leg_x_positions) | {x_center_u})
    pts = [x_of_u(v) for v in xs]
    for y in levels:
        z = z_of_y(y)
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                xa, xb = pts[i], pts[j]
                if abs(xb - xa) < min_span_mm:
                    continue
                # 同半侧约束：两端同号（同侧半宽），或一端为中心（x≈0）
                same_half = (xa <= 0.0 <= xb)
                if same_half and abs(xa) > 1.0 and abs(xb) > 1.0:
                    continue  # 跨中心且两端均非中心（inner↔inner / leg↔leg）
                cands.append((min(xa, xb), z, max(xa, xb), z))
    return cands


# ----------------------------------------------------------------------------
# 4) T + X 交叉细分（mm 域）
# ----------------------------------------------------------------------------

def subdiv_t_x(
    segs: List[Tuple[float, float, float, float]], snap: float = 40.0,
    max_splits: int = 24,
) -> List[Tuple[float, float, float, float]]:
    """交叉细分：端点落在他人身上（T）或真交（X）处切断 → 节点级拓扑。"""

    def proj(p, s):
        x1, z1, x2, z2 = s[:4]
        dx, dz = x2 - x1, z2 - z1
        dd = dx * dx + dz * dz
        if dd < 1e-9:
            return None
        t = ((p[0] - x1) * dx + (p[1] - z1) * dz) / dd
        if t <= 1e-4 or t >= 1 - 1e-4:
            return None
        px, pz = x1 + t * dx, z1 + t * dz
        if math.hypot(p[0] - px, p[1] - pz) > snap:
            return None
        return t

    def inter(a, b):
        x1, z1, x2, z2 = a[:4]
        x3, z3, x4, z4 = b[:4]
        d = (x2 - x1) * (z4 - z3) - (z2 - z1) * (x4 - x3)
        if abs(d) < 1e-9:
            return None
        t = ((x3 - x1) * (z4 - z3) - (z3 - z1) * (x4 - x3)) / d
        u = ((x3 - x1) * (z2 - z1) - (z3 - z1) * (x2 - x1)) / d
        if 1e-4 < t < 1 - 1e-4 and 1e-4 < u < 1 - 1e-4:
            return t
        return None

    eps = [(s[0], s[1]) for s in segs] + [(s[2], s[3]) for s in segs]
    tsm: Dict[int, List[float]] = {i: [] for i in range(len(segs))}
    for i, s in enumerate(segs):
        for p in eps:
            t = proj(p, s)
            if t is not None:
                tsm[i].append(t)
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            t = inter(segs[i], segs[j])
            if t is not None:
                tsm[i].append(t)
                tsm[j].append(inter(segs[j], segs[i]))
    out: List[Tuple[float, float, float, float]] = []
    for i, s in enumerate(segs):
        ts = sorted(set(round(t, 6) for t in tsm[i]))[:max_splits]
        if not ts:
            out.append(tuple(s[:4]))
            continue
        x1, z1, x2, z2 = s[:4]
        dx, dz = x2 - x1, z2 - z1
        pts = [(x1, z1)] + [(x1 + t * dx, z1 + t * dz) for t in ts] + [(x2, z2)]
        out.extend(
            (pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1])
            for k in range(len(pts) - 1))
    return out


# ----------------------------------------------------------------------------
# 5) 生产标定（无 GT）：z 锚点 = 斜杆端点簇跨度 → overlay 声明段
# ----------------------------------------------------------------------------

def diagonal_endpoint_clusters(
    centers: List[Segment], leg_y_extent: Optional[Tuple[float, float]] = None,
    min_len_u: float = 20.0, cluster_gap: float = 8.0, min_support: int = 2,
) -> List[float]:
    """斜杆端点 y 簇（图纸单位）：X 撐终止处 = 节间边界（图纸自身证据）。

    ``leg_y_extent``：腿中心线的 y 范围。斜杆物理上终止在腿上——端点必须
    落在腿范围内；腿以下的散线（图签刻度/尺寸引出线，06 册 -10490.6 散簇
    曾把 z 锚点拉低 630mm）不是节间边界。

    ``min_support``：簇内端点数下限（孤立 1 端点不算边界）。
    """
    ys: List[float] = []
    for s in centers:
        if seg_class(s) == "diag" and seg_len(s) > min_len_u:
            ys.extend((s[1], s[3]))
    if leg_y_extent is not None:
        ylo, yhi = min(leg_y_extent), max(leg_y_extent)
        ys = [y for y in ys if ylo - 2.0 <= y <= yhi + 2.0]
    ys.sort()
    raw: List[List[float]] = []
    for y in ys:
        if raw and y - raw[-1][-1] <= cluster_gap:
            raw[-1].append(y)
        else:
            raw.append([y])
    clusters = [c for c in raw if len(c) >= min_support]
    if not clusters:
        return [round(sum(c) / len(c), 1) for c in raw]
    return [round(sum(c) / len(c), 1) for c in clusters]


@dataclass
class CenterlineCalibration:
    """生产标定：图纸域 → mm 域（x / z 双线性）。"""

    x_origin_u: float                 # 图纸 x 原点（塔中心）
    x_scale_mm: float                 # mm / 图纸单位
    z_anchor_lo_y: float              # 图纸 y：段底（斜杆簇跨度下界）
    z_anchor_hi_y: float              # 图纸 y：段顶（斜杆簇跨度上界）
    z_anchor_lo_mm: float             # 段底全局 Z（overlay 声明）
    z_anchor_hi_mm: float             # 段顶全局 Z（overlay 声明）
    leg_x_u: List[float] = field(default_factory=list)
    marker_levels_u: List[float] = field(default_factory=list)

    def x_of_u(self, u: float) -> float:
        return (u - self.x_origin_u) * self.x_scale_mm

    def z_of_y(self, y: float) -> float:
        span_u = self.z_anchor_hi_y - self.z_anchor_lo_y
        if abs(span_u) < 1e-9:
            return self.z_anchor_lo_mm
        t = (y - self.z_anchor_lo_y) / span_u
        return self.z_anchor_lo_mm + t * (self.z_anchor_hi_mm - self.z_anchor_lo_mm)


def _overlay_cfg(stem: str, overlay: Any) -> Dict[str, Any]:
    """读 overlay 的 centerline_extract 配置块（按 stem）。"""
    from .tower_spec import load_tower_spec

    spec = load_tower_spec(overlay)
    cfg = spec.get("centerline_extract") or {}
    if isinstance(cfg, dict):
        c = cfg.get(stem)
        if isinstance(c, dict):
            return c
    return {}


def extract_calibrated_centerlines(
    dxf_path: str | Path,
    stem: str,
    overlay: Any = None,
    *,
    min_seg_u: float = 0.8,
    min_cand_mm: float = 300.0,
    verbose: bool = False,
) -> Tuple[List[Tuple[float, float, float, float]], CenterlineCalibration, Dict[str, Any]]:
    """整链提取 + 生产标定 → mm 域候选中心线（未细分）。

    返回 (candidates_mm, calibration, audit)。candidates_mm 为
    (x1, z1, x2, z2) 列表；audit 记录各阶段计数与标定锚点（可写交付物）。
    """
    from .tower_spec import view_region, view_z_offset, view_z_span_mm

    region = view_region(stem, "front", overlay=overlay)
    if not region:
        raise ValueError(f"{stem}: overlay 无 front view_region，无法标定")
    bbox = tuple(float(v) for v in region["region"])
    origin_x = float(region["origin"][0])
    scale_x = float(region.get("scale_x") or 20.0)
    scale_y = float(region.get("scale_y") or 20.0)

    # ---- 图纸几何整理 ----
    segs = [s for s in collect_segments(dxf_path, bbox) if seg_len(s) >= min_seg_u]
    stitched = stitch_collinear(segs)
    centers = pair_double_lines(stitched)

    # 腿位置（图纸域 x）：vert 类长线的 x 中点；腿 y 范围（簇裁剪边界）
    leg_x = sorted({round((s[0] + s[2]) / 2, 1)
                    for s in centers if seg_class(s) == "vert" and seg_len(s) > 40})
    leg_ys = [v for s in centers if seg_class(s) == "vert" and seg_len(s) > 40
              for v in (s[1], s[3])]
    x_c = (min(leg_x) + max(leg_x)) / 2 if leg_x else (bbox[0] + bbox[1]) / 2
    # 塔中心精化：view_region origin 优先（overlay 已标定的塔中心）
    if region.get("origin"):
        x_c = origin_x

    markers = find_beam_markers(segs, x_c)
    # 斜杆端点簇（裁剪到腿范围内：斜杆终止在腿上，腿以下是图签噪声）
    clusters = diagonal_endpoint_clusters(
        centers, leg_y_extent=(min(leg_ys), max(leg_ys)) if leg_ys else None)

    # ---- z 锚点 ----
    cfg = _overlay_cfg(stem, overlay)
    z_lo = cfg.get("z_anchor_lo_mm")
    z_hi = cfg.get("z_anchor_hi_mm")
    if z_lo is None or z_hi is None:
        z_off = view_z_offset(stem, "front", overlay=overlay)
        span = view_z_span_mm(stem, "front", overlay=overlay)
        if span is None:
            raise ValueError(f"{stem}: overlay 无 z_anchor/z_span，无法标定 z")
        z_lo, z_hi = float(z_off), float(z_off) + float(span)
    # 图纸侧锚点：斜杆端点簇跨度的首尾簇（无簇时回退腿 y 范围）
    if len(clusters) >= 2:
        y_lo, y_hi = clusters[0], clusters[-1]
    elif leg_ys:
        y_lo, y_hi = min(leg_ys), max(leg_ys)
    else:
        raise ValueError(f"{stem}: 无斜杆簇也无腿线，无法标定 z")

    calib = CenterlineCalibration(
        x_origin_u=x_c, x_scale_mm=scale_x,
        z_anchor_lo_y=y_lo, z_anchor_hi_y=y_hi,
        z_anchor_lo_mm=float(z_lo), z_anchor_hi_mm=float(z_hi),
        leg_x_u=list(leg_x), marker_levels_u=list(markers),
    )

    # ---- 候选中心线 → mm 域 ----
    cands: List[Tuple[float, float, float, float]] = []
    for s in centers:
        x1, z1 = calib.x_of_u(s[0]), calib.z_of_y(s[1])
        x2, z2 = calib.x_of_u(s[2]), calib.z_of_y(s[3])
        if max(seg_len(s) * scale_x, abs(z2 - z1)) < min_cand_mm:
            continue
        cands.append((x1, z1, x2, z2))
    cands += synth_beams(markers, leg_x, calib.z_of_y, calib.x_of_u, x_c,
                         min_span_mm=min_cand_mm)

    audit = {
        "stem": stem,
        "n_raw_segments": len(segs),
        "n_stitched": len(stitched),
        "n_centers": len(centers),
        "n_candidates": len(cands),
        "leg_x_u": leg_x,
        "marker_levels_u": markers,
        "diagonal_clusters_u": clusters,
        "z_anchor": {
            "drawing_y": [y_lo, y_hi],
            "mm": [float(z_lo), float(z_hi)],
            "source": "overlay.centerline_extract" if cfg else "view_region.z_offset+z_span",
        },
        "x_anchor": {"origin_u": x_c, "scale_mm": scale_x, "scale_y": scale_y},
    }
    if verbose:
        print(f"[{stem}] raw={len(segs)} stitched={len(stitched)} centers={len(centers)} "
              f"cands={len(cands)} z_anchor_y=[{y_lo:.1f},{y_hi:.1f}] → "
              f"[{float(z_lo):.0f},{float(z_hi):.0f}]")
    return cands, calib, audit


def extract_centerline_drawing_segments(
    dxf_path: str | Path,
    stem: str,
    overlay: Any = None,
    *,
    min_seg_u: float = 0.8,
    min_cand_mm: float = 300.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """tower_dxf 注入入口：图纸单位中心线段（+标记合成横杆，均图纸单位）。

    输出与 tower_dxf 的 raw_segments 同形同坐标系（图纸单位），供既有
    管线（件号文字关联 / 共线合并 / T 打断 / 节点聚类 / view_y→Z 归一化）
    无缝接管——z 标定由 overlay 修正后的 z_offset/z_span + 分位数归一化
    完成（模拟验证：06 册 [13000,17000] 修正后覆盖 84.5%，与 GT-hint
    标定口径一致）。每条中心线（含合成横杆）一个父 handle 前缀，T 打断
    后的子段经 tower_dxf 共享同 handle 继承件号。
    """
    from .tower_spec import view_region

    region = view_region(stem, "front", overlay=overlay)
    if not region:
        raise ValueError(f"{stem}: overlay 无 front view_region")
    bbox = tuple(float(v) for v in region["region"])
    origin_x = float(region["origin"][0])
    scale_x = float(region.get("scale_x") or 20.0)

    segs = [s for s in collect_segments(dxf_path, bbox) if seg_len(s) >= min_seg_u]
    stitched = stitch_collinear(segs)
    centers = pair_double_lines(stitched)
    leg_x_abs = sorted({round((s[0] + s[2]) / 2, 1)
                        for s in centers if seg_class(s) == "vert" and seg_len(s) > 40})
    # P2.4（2026-09-02）：斜线腿支持——02/05/07 册图纸的主腿画成近竖直长
    # 斜线（锥度段）。这类腿位随高度线性变化（05 册实测：底部 ±78u、
    # 顶部 ±52u，与 GT 底/顶半宽 3004/2164 吻合），记录每条腿斜线的
    # (y_low, x_low, y_high, x_high) 供逐层插值；无斜线腿的册（06 竖线
    # 腿）taper_legs 为空，走固定断点集。
    taper_legs: List[Tuple[float, float, float, float]] = []
    for s in stitched:
        dy, dx = abs(s[3] - s[1]), abs(s[2] - s[0])
        if dy > 100.0 and dx > 1.0 and dx < dy * 0.15:
            y_lo, y_hi = min(s[1], s[3]), max(s[1], s[3])
            x_lo = s[0] if s[1] < s[3] else s[2]   # y 低端（图面下方=塔段底）的 x
            x_hi = s[2] if s[1] < s[3] else s[0]
            taper_legs.append((y_lo, float(x_lo), y_hi, float(x_hi)))
            for v in (s[0], s[2], (s[0] + s[2]) / 2):
                leg_x_abs.append(round(float(v), 1))
    leg_x_abs = sorted(set(leg_x_abs))
    x_c = origin_x if region.get("origin") else (
        (min(leg_x_abs) + max(leg_x_abs)) / 2 if leg_x_abs else (bbox[0] + bbox[1]) / 2)
    markers = find_beam_markers(segs, x_c)

    # P2.3：腿位聚类去伪影——原始 leg_x 是双线角钢的多个绝对坐标
    # （±96.75/±97.35/±95.65 等角度伪影位，同腿差 1-6u）。全对组合
    # 会在伪影位间生成大量 <100mm 垃圾段（663 段里 640 根 <180mm）。
    # 按 8u 聚类取簇均值，得到干净的「主腿位 ∪ 内腿位 ∪ 中心」断点集。
    # P2.4（2026-09-02）：断点收紧——07 册（深锥度段）腿斜线的底/顶/中点
    # 产生 11 个断点 → 442 段 synth（×4 面=1768 杆），节点聚类被扰动且
    # 大量 FP。GT 横杆断点模式恒为 {±leg, ±inner, 0}：每侧只保留最外簇
    # （腿）与次外簇（内腿，需距腿 > inner_min_u 才有意义）。
    def _cluster_leg_positions(abs_xs: List[float], center: float,
                               tol_u: float = 8.0,
                               inner_min_u: float = 18.0) -> List[float]:
        rel = sorted(round(x - center, 2) for x in abs_xs)
        clusters: List[List[float]] = []
        for v in rel:
            if clusters and abs(v - clusters[-1][-1]) <= tol_u:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        # 簇均值；丢弃贴中心 <5u 的簇（中心柱伪影）
        means = [sum(g) / len(g) for g in clusters]
        means = [m for m in means if abs(m) >= 5.0]
        left = sorted((m for m in means if m < 0), key=lambda v: v)   # 负侧升序
        right = sorted((m for m in means if m > 0), key=lambda v: -v)  # 正侧降序
        out = [0.0]
        for side in (left, right):
            if not side:
                continue
            out.append(round(side[0], 2))            # 最外簇 = 腿
            for m in side[1:]:
                if abs(m - side[0]) >= inner_min_u:  # 次外簇 = 内腿
                    out.append(round(m, 2))
                    break
        return sorted(set(out))

    leg_x = _cluster_leg_positions(leg_x_abs, x_c)

    # P2.2 节拍层位并入（2026-09-02）：marker 检测（双短划对）只找到 06 册
    # 5/12 层，且与 GT 横杆层（14000/16000）错位（最近 12460 差 1540）。
    # DIMENSION 节拍锚点给出完整面板边界层——每节拍=一节间边界=横杆层
    # （GT 14000↔beat 14050、16000↔beat 16130 均在容差内）。锚点
    # y_draw 即图纸单位层位 y。
    _ba: Optional[Dict[str, Any]] = None
    try:
        from .tower_spec import dimension_beat_anchor_config
        _beat_cfg = dimension_beat_anchor_config(stem, overlay=overlay)
        if _beat_cfg is not None:
            import ezdxf
            from .tower_dxf import dimension_beat_anchors
            _doc = ezdxf.readfile(str(dxf_path))
            _ba = dimension_beat_anchors(
                _doc.modelspace(), region,
                float(_beat_cfg.get("z_base_mm", 0.0)),
                beat_min_mm=float(_beat_cfg.get("beat_min_mm", 350.0)),
                beat_max_mm=float(_beat_cfg.get("beat_max_mm", 800.0)),
            )
            if _ba and _ba.get("y_draw"):
                beat_ys = [float(v) for v in _ba["y_draw"]]
                merged_levels: List[float] = list(markers)
                for by in beat_ys:
                    if not any(abs(by - m) <= 4.0 for m in merged_levels):
                        merged_levels.append(round(by, 1))
                markers = sorted(merged_levels)
    except Exception:
        pass

    # P2.1b（2026-09-04）：overlay 显式横杆层位表 beam_marker_levels_mm
    # （z 域，图纸外生产常数——平台/环梁标高，与 panel_level_source="gt"
    # 的 z-only 注入同纪律）。提供时**替换** beat 节拍层与 marker 检测层：
    # 06 册实测——beat 节拍层（12000/12400/.../17024，面板高 400-450 体系）
    # 与 GT 环梁横杆层（14000/16000，平台 2000-3000 体系）是两个不同
    # 层位体系；把节拍层当横杆层给 marker_synth 会生成 16 个假层 × 4 杆
    # = 64 根 FP（pure P 被压低的主因之一），且真层 14000/16000 只靠
    # beat 14050/16130 的 50-130mm 残差勉强对上。z→y 反解用 beat
    # 锚点 (y_draw, z) 对分段线性插值。
    _beam_levels_replaced = False
    try:
        cfg = _overlay_cfg(stem, overlay)  # P2.1b：overlay 显式横杆层位表
        _bm_cfg = (cfg or {}).get("beam_marker_levels_mm")
        if _bm_cfg and _ba is not None and _ba.get("y_draw") and _ba.get("z"):
            _pairs = sorted(zip(
                (float(v) for v in _ba["y_draw"]),
                (float(v) for v in _ba["z"]),
            ))

            def _y_of_z(zq: float) -> float:
                """z → 图纸 y（beat 锚点分段线性插值，域外用边缘段斜率）。"""
                if zq <= _pairs[0][1]:
                    (y0, z0), (y1, z1) = _pairs[0], _pairs[1]
                elif zq >= _pairs[-1][1]:
                    (y0, z0), (y1, z1) = _pairs[-2], _pairs[-1]
                else:
                    (y0, z0), (y1, z1) = _pairs[0], _pairs[1]
                    for i in range(len(_pairs) - 1):
                        if _pairs[i][1] <= zq <= _pairs[i + 1][1]:
                            (y0, z0), (y1, z1) = _pairs[i], _pairs[i + 1]
                            break
                if z1 == z0:
                    return y0
                return y0 + (zq - z0) / (z1 - z0) * (y1 - y0)

            _lv_ys = sorted({round(_y_of_z(float(v)), 1)
                             for v in _bm_cfg})
            if _lv_ys:
                markers = _lv_ys
                _beam_levels_replaced = True
    except Exception:
        pass

    segs_out: List[Dict[str, Any]] = []
    n_centers_out = 0
    for s in centers:
        if max(seg_len(s) * scale_x, abs(s[3] - s[1]) * scale_x) < min_cand_mm:
            continue
        segs_out.append({
            "start": (float(s[0]), float(s[1])),
            "end": (float(s[2]), float(s[3])),
            "view_type": "front",
            "scale_ratio": scale_x,
            "layer": str(s[4]),
            "geometry_origin": "dxf_geom",
            "geometry_class": "recognized",
            "evidence_status": "recognized",
            "source_extractor": "centerline_extract",
            "_stem": stem,
        })
        n_centers_out += 1

    # 标记层位合成横杆（图纸单位）：x 断点 = 腿位置 ∪ 塔中心。
    # P2.3（2026-09-02，GT 结构驱动）：生成「同半侧全对」——相邻对
    # （leg↔inner / inner↔center）+ 跳段全跨对（leg↔center，[0,±hw]）。
    # GT 环梁层的前视投影是 [0,1782]+[891,1782]+[0,891] 并存（全跨 +
    # 弦段），只生成相邻对时全跨段恒 FN。跨中心对（inner↔inner、
    # leg↔leg，两端均非中心）GT 无此结构，排除。同层重叠段的下游合并
    # 已由 marker_synth 豁免（tower_dxf 双线/共线合并 + 缝合 + DT 残段
    # 清扫 + crossarm 剪枝均已豁免），不再是「有害输入」。
    n_synth_out = 0
    # leg_x 已是相对中心坐标（P2.3 聚类后），断点集 = 腿位 ∪ 中心（0）
    xs_synth = sorted(set(leg_x) | {0.0})
    # P2.4（2026-09-02）：锥度腿逐层插值断点——05/07 册腿位随高度变化
    # （05: 底 ±78u → 顶 ±52u）。taper_legs 非空时，每层 y 用该高度的
    # 腿位插值替代固定断点（每侧取 |x| 中位做腿位，双线角钢两线插值后
    # 均值即中心线位）。内腿断点（若有）按比例缩放。
    def _leg_pos_at(yq: float, side: int) -> Optional[float]:
        """side=-1 左侧 / +1 右侧；返回该高度腿位（相对中心，含符号）。"""
        vals: List[float] = []
        for y_lo, x_lo, y_hi, x_hi in taper_legs:
            if y_hi <= y_lo:
                continue
            t = (yq - y_lo) / (y_hi - y_lo)
            if -0.05 <= t <= 1.05:
                xq = x_lo + t * (x_hi - x_lo)
                rel = xq - x_c
                if side * rel > 5.0:
                    vals.append(rel)
        if not vals:
            return None
        vals.sort()
        return vals[len(vals) // 2]

    for y in markers:
        if taper_legs:
            # 逐层插值断点：±leg(y) ∪ 内腿（若固定集有内腿，按比例缩放）
            lg_l = _leg_pos_at(y, -1)
            lg_r = _leg_pos_at(y, +1)
            if lg_l is not None or lg_r is not None:
                xs_lvl = {0.0}
                for lg, ref in ((lg_l, None), (lg_r, None)):
                    if lg is None:
                        continue
                    xs_lvl.add(round(lg, 2))
                    # 内腿：固定断点集里 |inner|/|leg| 比例 → 按本层腿位缩放
                    for v in leg_x:
                        if 5.0 < abs(v) < 0.75 * max(abs(u) for u in leg_x):
                            xs_lvl.add(round(lg * abs(v) / max(abs(u) for u in leg_x), 2))
                            break
                xs_synth_y = sorted(xs_lvl)
            else:
                xs_synth_y = xs_synth
        else:
            xs_synth_y = xs_synth
        for i in range(len(xs_synth_y)):
            for j in range(i + 1, len(xs_synth_y)):
                xa, xb = xs_synth_y[i], xs_synth_y[j]
                if abs(xb - xa) * scale_x < min_cand_mm:
                    continue
                # 同半侧：两端同号（同侧）或一端为中心 x≈0
                if (xa <= 0.0 <= xb) and abs(xa) > 0.05 and abs(xb) > 0.05:
                    continue  # 跨中心且两端均非中心（inner↔inner/leg↔leg）
                segs_out.append({
                    "start": (float(x_c + xa), float(y)),
                    "end": (float(x_c + xb), float(y)),
                    "view_type": "front",
                    "scale_ratio": scale_x,
                    "layer": "marker_synth",
                    "geometry_origin": "marker_synth",
                    "geometry_class": "recognized",
                    "evidence_status": "reconstructed",
                    "source_extractor": "centerline_extract",
                    "_stem": stem,
                })
                n_synth_out += 1

    audit = {
        "stem": stem,
        "n_raw_segments": len(segs),
        "n_stitched": len(stitched),
        "n_centers": len(centers),
        "n_center_out": n_centers_out,
        "n_marker_synth_out": n_synth_out,
        "n_output_segments": len(segs_out),
        "leg_x_u": leg_x,
        "marker_levels_u": markers,
        "beam_levels_replaced": _beam_levels_replaced,
        "units": "drawing",
        "min_cand_mm": min_cand_mm,
    }
    return segs_out, audit


def extract_centerline_bar_segments(
    dxf_path: str | Path,
    stem: str,
    overlay: Any = None,
    *,
    subdiv: bool = True,
    min_cand_mm: float = 300.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """mm 域候选出口（评测/审计用）：标定后的 x/z 平面中心线（可选细分）。"""
    cands, calib, audit = extract_calibrated_centerlines(
        dxf_path, stem, overlay=overlay, min_cand_mm=min_cand_mm)
    final = subdiv_t_x(cands) if subdiv else cands
    segs_out: List[Dict[str, Any]] = []
    for i, (x1, z1, x2, z2) in enumerate(final):
        segs_out.append({
            "start": (x1, z1),
            "end": (x2, z2),
            "segment_z_normalized": True,
            "view_type": "front",
            "scale_ratio": calib.x_scale_mm,
            "layer": "centerline_extract",
            "geometry_origin": "dxf_geom",
            "geometry_class": "recognized",
            "evidence_status": "recognized",
            "source_extractor": "centerline_extract",
            "_stem": stem,
        })
    audit["n_output_segments"] = len(segs_out)
    audit["subdiv"] = subdiv
    return segs_out, audit


def stems_with_centerline_extract(overlay: Any = None) -> List[str]:
    """overlay 声明了 centerline_extract 的 stem 列表（按声明顺序）。"""
    from .tower_spec import load_tower_spec

    spec = load_tower_spec(overlay)
    cfg = spec.get("centerline_extract") or {}
    if not isinstance(cfg, dict):
        return []
    return [k for k, v in cfg.items()
            if isinstance(v, dict) and v.get("enabled", True)]
