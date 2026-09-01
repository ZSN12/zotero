#!/usr/bin/env python3
"""Phase 1 候选中心线提取器 + 段内候选召回评测（DXF 直读，无 GT 合成）。

背景（docs/PHASE1_06_DIAGNOSIS.md）：06/07 段 A2-pure 召回为 0 的根因不是
「图纸没画」，而是现有链路没有把图纸几何整理成 GT 拓扑粒度的中心线：

  1. 主腿画成双线角钢（外缘+内缘两条近平行线）→ 需配对出中心线；
  2. X 撑画成通长线（跨节间）→ 需 T/X 交叉细分；
  3. 横杆只画「双短划标记对」（塔中心 x、层位 y，1.2 图纸单位长）→
     需从标记层位 + 腿位置合成层位横杆中心线（x 断点=腿位置）；
  4. 内腿（±891mm）只画部分节段 → 共线缝合补全。

本脚本输出候选召回（匈牙利 1:1，tol=500mm），并为 4 面对称塔提供
`--mirror-4` 倍增（前视 1 条线 → f/b/l/r 4 个投影候选），用于衡量
「图纸几何信息是否足以覆盖 GT 中心线」（Phase 1 门禁口径）。

用法：
  python3 scripts/eval_segment_candidates.py --sheet 06 [--mirror-4] [--tol 500]

评测标定：z 由横杆标记层锚点 + 腿端点做线性拟合（图纸自身证据）；
评测时允许 --reg-band 配准微调（默认 ±150mm z 平移搜索，测「画没画」
而非「标没标对」；生产标定走 DIMENSION，另案）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import ezdxf  # noqa: E402

from traceability.eval.metrics import (  # noqa: E402
    _classify_3d, gt_bars_2d, hungarian_match, segment_cost,
)

DXF_DIR = REPO / "out/jc1-batch/dxf"
GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"
Z_WINDOWS = {  # segment_gate 同源（模型 source_file 推导，见 diagnose_recall.py）
    "06": (12143.0, 17135.8),
    "07": (6667.6, 12573.0),
}
Z_MODULE_SPAN_MM = {  # GT 模块结构跨度（图纸 z 比例粗估用）
    "06": 5000.0,
    "07": 5500.0,
}


# ----------------------------------------------------------------------------
# 1) 塔区几何收集
# ----------------------------------------------------------------------------

def collect_segments(dxf_path: Path, bbox: tuple[float, float, float, float]):
    """bbox=(x0,x1,y0,y1) 图纸单位；返回 [(x1,y1,x2,y2,layer)]。"""
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    x0, x1, y0, y1 = bbox
    out = []
    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE":
                pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
                if all(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in pts):
                    out.append((pts[0][0], pts[0][1], pts[1][0], pts[1][1], e.dxf.layer))
            elif t == "LWPOLYLINE":
                pts = list(e.get_points("xy"))
                if all(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in pts):
                    for i in range(len(pts) - 1):
                        out.append((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], e.dxf.layer))
        except Exception:
            continue
    return out


def auto_tower_bbox(dxf_path: Path):
    """自动找塔立面区：长斜线（X 撑）锚定 + 长竖线（腿）收束。

    图签框线是纯竖线（90.0°），塔腿带锥度（86°/94°），X 撑是 ±34°~
    ±38° 长斜线（100~230 单位）——只有立面才有。先用长斜线定位主簇，
    再用同簇内的竖线收 x/y 包络。"""
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    diags, verts = [], []
    for e in msp:
        try:
            if e.dxftype() != "LINE":
                continue
            p1, p2 = e.dxf.start, e.dxf.end
            L = math.hypot(p2.x - p1.x, p2.y - p1.y)
            if L < 60:
                continue
            a = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x)) % 180.0
            if 25.0 <= a <= 65.0 or 115.0 <= a <= 155.0:
                diags.append(((p1.x + p2.x) / 2, p1, p2))
            elif 80.0 <= a <= 100.0:
                verts.append(((p1.x + p2.x) / 2, p1, p2))
        except Exception:
            continue
    if not diags:
        raise SystemExit("auto_tower_bbox: 找不到长斜线（X 撑）")
    # 斜线 x 中点主簇（bin=60 连续簇，取线数最多）
    bins = defaultdict(int)
    for xm, _, _ in diags:
        bins[int(xm // 60) * 60] += 1
    bs = sorted(bins)
    clusters, cur = [], [bs[0]]
    for b in bs[1:]:
        if b - cur[-1] <= 60:
            cur.append(b)
        else:
            clusters.append(cur); cur = [b]
    clusters.append(cur)
    best = max(clusters, key=lambda cl: sum(bins[b] for b in cl))
    dsel = [(xm, p1, p2) for xm, p1, p2 in diags if int(xm // 60) * 60 in best]
    xs = [v for xm, p1, p2 in dsel for v in (p1.x, p2.x)]
    x0, x1 = min(xs), max(xs)
    # 竖线中落在 [x0-10, x1+10] 的收 y 包络（腿与 X 撑同区）
    ysel = [v for xm, p1, p2 in dsel for v in (p1.y, p2.y)]
    for xm, p1, p2 in verts:
        if x0 - 10 <= xm <= x1 + 10:
            ysel += [p1.y, p2.y]
            xs += [p1.x, p2.x]
            x0, x1 = min(x0, p1.x, p2.x), max(x1, p1.x, p2.x)
    return x0 - 25, x1 + 25, min(ysel) - 15, max(ysel) + 15


# ----------------------------------------------------------------------------
# 2) 几何整理：分类 / 共线缝合 / 双线配对
# ----------------------------------------------------------------------------

def _ang(s):
    return math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180.0


def _len(s):
    return math.hypot(s[2] - s[0], s[3] - s[1])


def _cls(s):
    a = _ang(s)
    if a < 12 or a > 168:
        return "horiz"
    if 78 < a < 102:
        return "vert"
    return "diag"


def stitch_collinear(segs, gap_tol=6.0, ang_tol=6.0, col_tol=1.5):
    """共线缝合：同向近角 + 端点投影 gap ≤ gap_tol → 链式拼通长线。"""
    chains = []
    used = [False] * len(segs)
    order = sorted(range(len(segs)), key=lambda i: -_len(segs[i]))
    for i in order:
        if used[i]:
            continue
        used[i] = True
        cur = segs[i]
        grew = True
        while grew:
            grew = False
            for j in range(len(segs)):
                if used[j]:
                    continue
                s = segs[j]
                if abs(_ang(s) - _ang(cur)) > ang_tol and abs(_ang(s) - _ang(cur) - 180) > ang_tol:
                    continue
                for flip in (0, 1):
                    a, b = (cur[:4], cur[2:4] + cur[:2])
                    c = s if not flip else (s[2], s[3], s[0], s[1], s[4])
                    # 端点距 + 共线检查
                    d = math.hypot(b[0] - c[0], b[1] - c[1])
                    if d > gap_tol:
                        continue
                    # 垂距（s 远端到 cur 直线）
                    x1, y1, x2, y2 = cur[:4]
                    dd = math.hypot(x2 - x1, y2 - y1)
                    px, py = c[2], c[3]
                    t = ((px - x1) * (x2 - x1) + (py - y1) * (y2 - y1)) / (dd * dd)
                    perp = math.hypot(px - (x1 + t * (x2 - x1)), py - (y1 + t * (y2 - y1)))
                    if perp > col_tol:
                        continue
                    cur = (cur[0], cur[1], c[2], c[3], cur[4])
                    used[j] = True
                    grew = True
                    break
                if grew:
                    break
        chains.append(cur)
    return chains


def pair_double_lines(segs, max_off=6.0, ang_tol=4.0, len_ratio=0.7):
    """双线配对：平行 + 偏距 ≤ max_off + 长度相近 → 中心线（保留单线）。

    主腿双线角钢（layer 4 外缘 + layer 1/0 内缘，偏距 ~2-8 单位）合并为
    一条中心线；X 撑对（225.8/223.8 平行近叠）同理。

    方向归一化：国网图同一根杆常在两个图层各画一遍（LINE + LWPOLYLINE
    重复），两条重复线方向可能相反——若不先把 t 翻成与 s 同向，中心线
    端点取的是「s 起点 ↔ t 终点」的中点，两条近重合线会算出零长中心线，
    X 撑整条消失（实测 06 册 X 撑全部丢失的根因）。"""
    used = [False] * len(segs)
    out = []
    for i, s in enumerate(segs):
        if used[i]:
            continue
        best_j, best_off = -1, 1e9
        for j in range(i + 1, len(segs)):
            if used[j]:
                continue
            t = segs[j]
            if abs(_ang(s) - _ang(t)) > ang_tol:
                continue
            lo, hi = min(_len(s), _len(t)), max(_len(s), _len(t))
            if hi <= 0 or lo / hi < len_ratio:
                continue
            # 端点到对方中线的垂距（两方向取 max）
            def off(p, u):
                x1, y1, x2, y2 = u[:4]
                dd = math.hypot(x2 - x1, y2 - y1)
                tt = ((p[0] - x1) * (x2 - x1) + (p[1] - y1) * (y2 - y1)) / (dd * dd)
                tt = min(1.0, max(0.0, tt))
                return math.hypot(p[0] - (x1 + tt * (x2 - x1)), p[1] - (y1 + tt * (y2 - y1)))
            o = max(min(off((s[0], s[1]), t), off((s[2], s[3]), t)),
                    min(off((t[0], t[1]), s), off((t[2], t[3]), s)))
            if 0.3 < o < max_off and o < best_off:
                best_off, best_j = o, j
        if best_j >= 0:
            t = segs[best_j]
            used[best_j] = True
            # 方向归一化：t 与 s 同向（起点靠近起点；反向重复线翻转后再配）
            if math.hypot(t[0] - s[0], t[1] - s[1]) > math.hypot(t[2] - s[0], t[3] - s[1]):
                t = (t[2], t[3], t[0], t[1], t[4])
            ax, ay = (s[0] + t[0]) / 2, (s[1] + t[1]) / 2
            bx, by = (s[2] + t[2]) / 2, (s[3] + t[3]) / 2
            if math.hypot(bx - ax, by - ay) >= 0.5 * max(_len(s), _len(t)):
                out.append((ax, ay, bx, by, s[4]))
                continue
            # 退化（近重合重复线）：保留 s 一条即可（t 已被 used 吞掉）
            out.append(s)
            continue
        out.append(s)
    return out


# ----------------------------------------------------------------------------
# 3) 横杆标记识别 → 层位横杆合成
# ----------------------------------------------------------------------------

def find_beam_markers(segs, x_center, center_tol=14.0, cluster_gap=10.0, min_len=0.8):
    """塔中心的横杆层位标记 → y 列表。

    06 册：塔中心双短划对（x[34546..34550]，1.2 长，间隔 4）。
    07 册：8500 层是 8 条平行长线组（x[34412→34529]，间隔 8），
    11500 层是单条中长线（x[34455→34467]，11.5 长）。
    聚簇规则：中心区（±center_tol）水平线按 y 聚簇（gap ≤ cluster_gap），
    簇内 ≥2 条线或单线宽 ≥10 记为层位（取簇 y 均值）。"""
    marks = []
    for s in segs:
        if _cls(s) != "horiz":
            continue
        L = _len(s)
        if L < min_len or L > 220.0:
            continue
        mx = (s[0] + s[2]) / 2
        if abs(mx - x_center) > center_tol:
            continue
        marks.append((round((s[1] + s[3]) / 2, 1), L))
    marks.sort()
    levels = []
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


def synth_beams(levels, leg_x_positions, z_of_y, x_of_u, x_center_u):
    """层位横杆中心线：x 断点 = 腿位置 ∪ 塔中心，全对组合。

    GT 环形横杆的前视投影跨断点组合多样：正面横杆跨全宽（0↔±外腿，
    不断在内腿），环斜杆跨（外腿↔内腿）/（内腿↔0）。只合成相邻段会漏
    「中心↔角」跨两断点的杆（06/07 每层各 4 根）。全对组合（|跨度|≥300mm）
    覆盖所有组合方式，冗余段对覆盖率无害。"""
    cands = []
    xs = sorted(set(leg_x_positions) | {x_center_u})
    pts = [x_of_u(v) for v in xs]
    for y in levels:
        z = z_of_y(y)
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                xa, xb = pts[i], pts[j]
                if abs(xb - xa) < 300:
                    continue
                cands.append((min(xa, xb), z, max(xa, xb), z))
    return cands


# ----------------------------------------------------------------------------
# 4) 标定：标记层位 → z 线性映射；腿位置 → x 映射
# ----------------------------------------------------------------------------

def calibrate(marker_levels, gt_hint_levels, y_span_u, module_span_mm):
    """z(y) 线性映射 + 假层位过滤。

    检出的层位会混入图签箭头/中心符号等假层位；真层位与 GT 横杆层位
    一一对应且间距一致。先用图纸跨度估 z 比例（module_span/y_span），
    再从所有「选 len(gt) 个层位」的组合里挑间距最一致的组合做锚点。

    gt_hint_levels 只用于【评测标定】（衡量「图纸画没画」），不进生产
    （生产走 DIMENSION，见诊断文档 §三-2）。"""
    import itertools
    if not marker_levels or not gt_hint_levels:
        return None
    m = len(gt_hint_levels)
    ms_all = sorted(marker_levels)
    if len(ms_all) < m:
        return None
    zsc0 = module_span_mm / max(1e-6, y_span_u)
    best, best_score = None, None
    for combo in itertools.combinations(ms_all, m):
        # 组合内间距 vs GT 层位间距（用 zsc0 粗估）
        score = 0.0
        for k in range(m - 1):
            ygap = (combo[k + 1] - combo[k]) * zsc0
            ggap = gt_hint_levels[k + 1] - gt_hint_levels[k]
            score += abs(ygap - ggap) / max(1.0, abs(ggap))
        if best_score is None or score < best_score:
            best_score, best = score, combo
    ms, gs = list(best), list(gt_hint_levels)
    if m >= 2:
        s = (gs[-1] - gs[0]) / (ms[-1] - ms[0])
    else:
        s = zsc0
    a = gs[0] - ms[0] * s
    return lambda y: a + y * s


# ----------------------------------------------------------------------------
# 5) T + X 交叉细分（mm 域）
# ----------------------------------------------------------------------------

def subdiv_t_x(segs, snap=40.0, max_splits=24):
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
    tsm = {i: [] for i in range(len(segs))}
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
    out = []
    for i, s in enumerate(segs):
        ts = sorted(set(round(t, 6) for t in tsm[i]))[:max_splits]
        if not ts:
            out.append(s[:4])
            continue
        x1, z1, x2, z2 = s[:4]
        dx, dz = x2 - x1, z2 - z1
        pts = [(x1, z1)] + [(x1 + t * dx, z1 + t * dz) for t in ts] + [(x2, z2)]
        out += [(pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1]) for k in range(len(pts) - 1)]
    return out


# ----------------------------------------------------------------------------
# 5b) 中心线覆盖率（候选 recall 主口径）
# ----------------------------------------------------------------------------

def coverage_match(gt_segs, cands, tol=500.0, ang_tol=15.0):
    """中心线覆盖：GT 杆两端点都落在某条候选线的 tol 邻域内且夹角相近。

    Phase 1「候选 recall ≥95%（对 GT 中心线）」的口径：图纸是否包含
    GT 中心线的几何路径。图纸把节间连续画线（腿通长、X 通长跨层位），
    GT 是节点级分析模型——1:1 端点匹配会因拓扑切分差异系统性低估
    （实测最优 cost 669 卡在 tol 500 边缘），覆盖率才是「画没画」
    的忠实度量。点到线段距离 + 角度约束。"""
    def pt_seg_dist(p, s):
        x1, z1, x2, z2 = s
        dx, dz = x2 - x1, z2 - z1
        dd = dx * dx + dz * dz
        if dd < 1e-9:
            return math.hypot(p[0] - x1, p[1] - z1)
        t = ((p[0] - x1) * dx + (p[1] - z1) * dz) / dd
        t = min(1.0, max(0.0, t))
        return math.hypot(p[0] - (x1 + t * dx), p[1] - (z1 + t * dz))

    def ang(s):
        return math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180.0

    hits, misses = [], []
    for i, g in enumerate(gt_segs):
        ga = ang(g)
        ok = False
        for c in cands:
            ca = ang(c)
            da = min(abs(ga - ca), 180 - abs(ga - ca))
            if da > ang_tol:
                continue
            if pt_seg_dist((g[0], g[1]), c) <= tol and pt_seg_dist((g[2], g[3]), c) <= tol:
                ok = True
                break
        (hits if ok else misses).append(i)
    return hits, misses


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def extract_sheet_candidates(sheet: str, mirror_4: bool, verbose=False):
    stem = f"35A1-JC1-{sheet}"
    dxf = DXF_DIR / f"{stem}.dxf"
    bbox = auto_tower_bbox(dxf)
    if verbose:
        print(f"[{stem}] 塔区 bbox={tuple(round(v,1) for v in bbox)}")
    segs = collect_segments(dxf, bbox)
    # 细碎噪声过滤（<1.0 单位）但保留标记（0.8+）
    segs = [s for s in segs if _len(s) >= 0.8]
    if verbose:
        print(f"  塔区线段 {len(segs)}（>0.8u）")

    # 共线缝合（内腿碎片 → 通长）
    stitched = stitch_collinear(segs)
    if verbose:
        print(f"  共线缝合后 {len(stitched)}")
    # 双线配对 → 中心线
    centers = pair_double_lines(stitched)
    if verbose:
        from collections import Counter
        print(f"  双线配对后 {len(centers)} | 分类 {dict(Counter(_cls(s) for s in centers))}")

    # 腿位置（图纸域 x）：vert 类长线（>40u）的 x 中点
    leg_x = sorted({round((s[0] + s[2]) / 2, 1) for s in centers if _cls(s) == "vert" and _len(s) > 40})
    x_c = (bbox[0] + bbox[1]) / 2  # 塔中心（近似）
    if leg_x:
        x_c = (min(leg_x) + max(leg_x)) / 2
    if verbose:
        print(f"  腿 x 位置(图纸): {leg_x} | 塔中心 {x_c:.1f}")

    # 横杆标记层位
    marker_levels = find_beam_markers(segs, x_c)
    if verbose:
        print(f"  横杆标记层位 y: {[round(v,1) for v in marker_levels]}")
    return segs, stitched, centers, leg_x, x_c, marker_levels, bbox


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", default="06")
    ap.add_argument("--mirror-4", action="store_true", help="4 面对称倍增候选（覆盖度口径）")
    ap.add_argument("--tol", type=float, default=500.0)
    ap.add_argument("--reg-band", type=float, default=150.0, help="配准 z 平移搜索带（±mm）")
    ap.add_argument("--gt-levels", default="", help="评测标定锚点：逗号分隔 GT 层位 z（默认按 sheet 用 14000,16000/6500,12000）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    gt = json.load(open(GT_PATH))
    g = gt_bars_2d(gt, "front")
    lo, hi = Z_WINDOWS[args.sheet]
    zmid = lambda s: (s[1] + s[3]) / 2.0
    win = [s for s, _, _ in g if lo - 1 <= zmid(s) <= hi + 1]
    inside = [s for s in win if min(s[1], s[3]) >= lo - 1 and max(s[1], s[3]) <= hi + 1]

    segs, stitched, centers, leg_x, x_c, marker_levels, bbox = extract_sheet_candidates(
        args.sheet, args.mirror_4, verbose=args.verbose)

    gt_levels = [float(v) for v in args.gt_levels.split(",") if v.strip()] or (
        [14000.0, 16000.0] if args.sheet == "06" else
        [8500.0, 11500.0] if args.sheet == "07" else [6500.0, 12000.0])

    z_of_y = calibrate(marker_levels, gt_levels,
                       bbox[3] - bbox[2], Z_MODULE_SPAN_MM.get(args.sheet, 5000.0))
    if z_of_y is None:
        raise SystemExit("标定失败：标记层位不足")
    # x 映射：塔中心 → 0；比例锚：外侧腿簇（每侧离中心最远的线群，
    # 排除塔心构造线——07 册塔心有大量竖线会把中位锚拉歪）↔ GT 腿 |x| 中位
    gt_leg_x = [abs(s[0]) for s in inside if _classify_3d(((s[0], 0, s[1]), (s[2], 0, s[3]))) == "leg"]
    import statistics as _st
    lmax = max((v - x_c for v in leg_x), default=44.0)
    rmax = max((x_c - v for v in leg_x), default=44.0)
    outer_u = (lmax + rmax) / 2.0
    med_mm = _st.median(gt_leg_x) if gt_leg_x else 1700.0
    # 外侧腿是锥形线：外缘位置 ↔ GT 最大 |x|，但线中点 ↔ 中位——
    # 取两者中点做初估，配准搜索再精调
    max_mm = max(gt_leg_x) if gt_leg_x else 1852.0
    xu_scale = (0.5 * med_mm + 0.5 * max_mm) / outer_u if outer_u > 0 else 20.0
    x_of_u = lambda u, sc=xu_scale: (u - x_c) * sc

    # 候选中心线 → mm 域（覆盖口径：不细分——通长线覆盖节间子段）
    def build_cands(sc):
        cs = []
        for s in centers:
            x1, z1 = (s[0] - x_c) * sc, z_of_y(s[1])
            x2, z2 = (s[2] - x_c) * sc, z_of_y(s[3])
            if max(_len(s) * sc, abs(z2 - z1)) < 300:  # <300mm 不当候选
                continue
            cs.append((x1, z1, x2, z2))
        cs += synth_beams(marker_levels, [v for v in leg_x], z_of_y,
                          lambda u: (u - x_c) * sc, x_c)
        return [c for c in cs if lo - 600 <= zmid(c) <= hi + 600]

    if args.verbose:
        print(f"  x 比例初估 {xu_scale:.2f}mm/u（腿中位锚）")

    # 覆盖率口径：配准搜索（z 平移 + x 比例）最大化覆盖
    best = None
    sc_range = [xu_scale * (1 + 0.10 * k / 10.0) for k in range(-10, 11)]  # ±10%
    for sc in sc_range:
        cs = build_cands(sc)
        for dz in range(int(-args.reg_band), int(args.reg_band) + 1, 25):
            cm = [(c[0], c[1] + dz, c[2], c[3] + dz) for c in cs]
            hits, _ = coverage_match(inside, cm, args.tol)
            if best is None or len(hits) > best[0]:
                best = (len(hits), dz, sc)
    n_cov, dz, sc_best = best
    cands = build_cands(sc_best)
    xu_scale = sc_best
    cm = [(c[0], c[1] + dz, c[2], c[3] + dz) for c in cands]
    hits, misses = coverage_match(inside, cm, args.tol)
    # 宽角度参考（图纸纵横比不精确的容让口径）
    hits25, _ = coverage_match(inside, cm, args.tol, ang_tol=25.0)

    # 1:1 口径（细分后，参考值）
    sub = subdiv_t_x(cm)
    matched, un_gt, un_m = hungarian_match(inside, sub, segment_cost, args.tol)

    # 分类召回
    from collections import Counter
    miss = Counter()
    for i in misses:
        s = inside[i]
        miss[_classify_3d(((s[0], 0, s[1]), (s[2], 0, s[3])))] += 1
    hit = Counter()
    for i in hits:
        s = inside[i]
        hit[_classify_3d(((s[0], 0, s[1]), (s[2], 0, s[3])))] += 1

    report = {
        "sheet": args.sheet, "gt_inside": len(inside), "n_candidates": len(cands),
        "reg_dz_mm": dz, "x_scale_mm_per_u": round(sc_best, 2),
        "coverage_tp": n_cov,
        "coverage_recall_pct": round(100.0 * n_cov / len(inside), 1),
        "coverage_recall_pct_ang25": round(100.0 * len(hits25) / len(inside), 1),
        "one_to_one_tp": len(matched),
        "one_to_one_recall_pct": round(100.0 * len(matched) / len(inside), 1),
        "recall_by_class": dict(hit), "miss_by_class": dict(miss),
        "mirror_4": args.mirror_4,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"[{args.sheet}] inside-GT {len(inside)} | 候选 {len(cands)} | 配准 dz={dz:+d}mm x_scale={sc_best:.1f}")
        print(f"  覆盖率召回 R={report['coverage_recall_pct']}%（{n_cov}/{len(inside)}，ang≤15°）")
        print(f"  覆盖率召回 R={report['coverage_recall_pct_ang25']}%（ang≤25° 参考口径，容图纸纵横比不精确）")
        print(f"  1:1 召回   R={report['one_to_one_recall_pct']}%（{len(matched)}/{len(inside)}，参考）")
        print(f"  分类命中 {dict(hit)}")
        print(f"  分类缺失 {dict(miss)}")


if __name__ == "__main__":
    main()
