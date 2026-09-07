"""Phase 4 纯图纸 z 链：从 01-1 单线总图的高度阶梯推导分段立面的绝对 z。

背景（Phase 4 前置研究，2026-09-04 实测定稿）：
  * 三塔（35A1-ZC1/35A2-JC1/35A3-SZC1）无 overlay 手工 z 声明时，所有分段立面
    在 z∈[0,span] 各自为政，整塔 3D 合并塌在底部——tier-3 门禁（A2 拓扑召回
    ≥85%）不可能达成；
  * ZC1 的历史 overlay z 值来自 GT 反投影网格搜索（scripts/calibrate_view.py
    P3.20），是研究标定而非生产路径——铁律禁止生产管线读 GT；
  * 三塔每册的 01-1（单线总图）带一列「高度阶梯」尺寸：横向 DIM 沿图高逐格
    排列，总和精确等于塔总高（35A2-JC1 实测 40200 = GT 顶 40200；35A2-ZC1
    列内含累计锚 39400/33000 = GT 顶/主结点）。这是**图纸内**的绝对 z 证据；
  * 分段立面图纸自带该段的阶梯节拍复本（35A2-ZC1-07 的横向大标注
    6400/3600/2800 即 01-1 阶梯尾部节拍），可作为段定位指纹。

算法（全部只读 DXF，无 GT）：
  1. ``ladder_from_sheet``：在 01-1 的 H 向 DIM 里找「阶梯列」——同 x 邻域、
     沿 y 等距堆叠、含大值（累计锚 ≥ 0.5×塔高量级）或节拍密集的列；
     产出（自底向上）节拍序列 beats 与累计结点 junctions（mm）。
  1b. ``distributed_ladder``（35A1-ZC1 版式兜底）：01-1 无整塔阶梯时，
     从各分段立面自带的段内阶梯列（首值=段高锚）重建——签名去重
     重复视图后按册号序堆叠。
  2. ``sheet_segment_evidence``：对每张分段立面提取塔身区（长竖线腿对定界）
     的 h_draw/w_draw 与区内的横向大标注（≥2000mm，段定位指纹）。
  3. ``assign_z_chain``：把每张立面匹配到阶梯的相邻结点对 (z1, z2)——
     评分 = 指纹节拍在 (z1,z2) 邻域的重合度 + 段高一致性（(z2-z1) 与图纸
     自身段高标注互证）。输出 per-stem ``z_offset``/``z_span_mm``。

铁律对齐：本模块只读 DIMENSION/线几何，不接触 GT 坐标，不生成任何 3D
起止点——产物是 view_region 的两个标量（z 底/段高），供
``_normalize_segment_view_y`` 既有归一化链消费。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ---- 参数（三塔 + ZC1 已知册实测标定；量纲：mm / 图面单位）----
# 横向大标注进入段定位指纹的最小真实 mm（段节拍 ≥2000，构件截面 ≤~500）
_FINGERPRINT_MIN_MM = 2000.0
# 阶梯节拍列：同 x 邻域宽度（图面单位）
_LADDER_COLUMN_TOL = 40.0
# 子列切分容差：同册「标注对」两根尺寸线 x 相差 ~10-14 单位（宽度列/高度列）
_LADDER_SUBCOLUMN_TOL = 5.0
# 阶梯节拍最小值（mm）——过滤构件尺寸/编号混入
_LADDER_BEAT_MIN_MM = 400.0
# 累计锚认定：值 ≥ 塔高估计的此比例
_CUM_ANCHOR_RATIO = 0.5
# 腿判定：近竖线长 ≥ 图高的此比例，且长 ≥ 斜率的 3 倍
_LEG_HEIGHT_RATIO = 0.15
# 塔身区腿对间距占图宽的上限（区分塔身与右侧标题栏竖线群）
_TOWER_PAIR_MAX_W = 0.45
# 段高一致性评分的容差（相对）
_HEIGHT_TOL = 0.18


@dataclass
class Ladder:
    """01-1 高度阶梯：自底向上节拍与结点（mm）。"""

    beats: List[float] = field(default_factory=list)  # 自底向上逐格高度
    junctions: List[float] = field(default_factory=list)  # 自底向上累计结点（含 0）
    cum_anchors: List[float] = field(default_factory=list)  # 图纸明示的累计标高
    source_stem: str = ""

    @property
    def total(self) -> float:
        return self.junctions[-1] if self.junctions else 0.0


@dataclass
class SheetEvidence:
    """分段立面图纸的塔身区证据（图面单位 + 真实 mm 混合，字段自明）。"""

    stem: str
    h_draw: float  # 塔身区图面高
    w_draw: float  # 腿对图面宽
    fingerprint_mm: List[float] = field(default_factory=list)  # 区内横向大标注
    vdim_mm: List[float] = field(default_factory=list)  # 区内竖向标注（含段高）


def _dim_rows(doc) -> List[Tuple[float, float, float, str]]:
    """(value_mm, x, y, orientation) 列表；值解析失败跳过。"""
    rows = []
    for e in doc.modelspace().query("DIMENSION"):
        txt = str(getattr(e.dxf, "text", "") or "")
        try:
            val = float(txt.replace("mm", "").strip())
        except (TypeError, ValueError):
            continue
        try:
            p1 = e.dxf.defpoint
            p2 = e.dxf.defpoint3
        except AttributeError:
            continue
        orient = "V" if abs(p2.y - p1.y) > abs(p2.x - p1.x) else "H"
        rows.append((val, float(p1.x), float(p1.y), orient))
    return rows


def _segment_cloud(doc, bar_layers: Sequence[str]) -> List[Tuple[float, float, float, float]]:
    """LINE/LWPOLYLINE 线段云（INSERT 展开），按图层过滤。"""
    from .tower_dxf import _flatten_modelspace_entities, _layer_hit

    segs = []
    for e in _flatten_modelspace_entities(doc.modelspace()):
        layer = str(getattr(e.dxf, "layer", "0") or "0")
        if not _layer_hit(layer, list(bar_layers)):
            continue
        if e.dxftype() == "LINE":
            segs.append((e.dxf.start.x, e.dxf.start.y,
                         e.dxf.end.x, e.dxf.end.y))
        elif e.dxftype() == "LWPOLYLINE":
            try:
                pts = list(e.get_points("xy"))
            except Exception:
                continue
            for i in range(len(pts) - 1):
                segs.append((pts[i][0], pts[i][1],
                             pts[i + 1][0], pts[i + 1][1]))
    return segs


def _strip_frame(segs):
    """去掉图框外矩形（bbox 边缘的整长轴线）。"""
    if not segs:
        return segs
    xs = [c for s in segs for c in (s[0], s[2])]
    ys = [c for s in segs for c in (s[1], s[3])]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    W, H = x1 - x0, y1 - y0
    keep = []
    for ax, ay, bx, by in segs:
        if abs(ay - by) < 0.5 and abs(ax - bx) > 0.6 * W and (
                abs(ay - y0) < 1 or abs(ay - y1) < 1):
            continue
        if abs(ax - bx) < 0.5 and abs(ay - by) > 0.6 * H and (
                abs(ax - x0) < 1 or abs(ax - x1) < 1):
            continue
        keep.append((ax, ay, bx, by))
    return keep


def _leg_columns(segs) -> List[Tuple[float, float, int]]:
    """腿列聚类：[(x_lo, x_hi, n)]，按 x 排序。"""
    if not segs:
        return []
    ys = [c for s in segs for c in (s[1], s[3])]
    xs_all = [c for s in segs for c in (s[0], s[2])]
    H = max(ys) - min(ys)
    W = max(xs_all) - min(xs_all)
    if H <= 0 or W <= 0:
        return []
    legs = [s for s in segs
            if abs(s[3] - s[1]) > _LEG_HEIGHT_RATIO * H
            and abs(s[3] - s[1]) > 3 * max(abs(s[2] - s[0]), 1e-9)]
    if not legs:
        return []
    lm: Dict[float, float] = {}
    for s in legs:
        xm = round((s[0] + s[2]) / 2, 1)
        lm[xm] = max(abs(s[3] - s[1]), lm.get(xm, 0.0))
    maxlen = max(lm.values())
    xs = sorted(x for x in lm if lm[x] > 0.3 * maxlen)
    if not xs:
        return []
    cols = [[xs[0]]]
    for x in xs[1:]:
        if x - cols[-1][-1] > 0.08 * W:
            cols.append([x])
        else:
            cols[-1].append(x)
    return [(c[0], c[-1], len(c)) for c in cols]


def _tower_zone(doc, bar_layers: Sequence[str]):
    """塔身区 (lo, hi, segs_in_zone)：两根最高相邻腿列界定的带邻域。"""
    segs = _segment_cloud(doc, bar_layers)
    if len(segs) < 150:
        return None
    segs = _strip_frame(segs)
    if not segs:
        return None
    ys = [c for s in segs for c in (s[1], s[3])]
    xs_all = [c for s in segs for c in (s[0], s[2])]
    H = max(ys) - min(ys)
    W = max(xs_all) - min(xs_all)
    x_min, x_max = min(xs_all), max(xs_all)
    cols = _leg_columns(segs)
    if len(cols) < 2:
        return None
    colh = []
    lm: Dict[float, float] = {}
    for s in segs:
        if abs(s[3] - s[1]) > _LEG_HEIGHT_RATIO * H and abs(
                s[3] - s[1]) > 3 * max(abs(s[2] - s[0]), 1e-9):
            xm = round((s[0] + s[2]) / 2, 1)
            lm[xm] = max(abs(s[3] - s[1]), lm.get(xm, 0.0))
    for lo, hi, _n in cols:
        colh.append(max(lm.get(x, 0.0) for x in (lo, hi)) if lm else 0.0)
    best = None
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            lo, hi = cols[i][0], cols[j][1]
            if not (0.04 * W < hi - lo < _TOWER_PAIR_MAX_W * W):
                continue
            if lo - x_min < 0.03 * W or x_max - hi < 0.03 * W:
                continue  # 贴边列 = 图框残余
            score = min(colh[i], colh[j]) * (1 + 0.1 * min(cols[i][2], cols[j][2]))
            if best is None or score > best[0]:
                best = (score, lo, hi)
    if best is None:
        return None
    _, lo, hi = best
    marg = (hi - lo) * 0.35 + 5
    zone = [s for s in segs
            if min(s[0], s[2]) >= lo - marg and max(s[0], s[2]) <= hi + marg]
    if len(zone) < 80:
        return None
    return lo, hi, zone


def ladder_from_sheet(
    dxf_path: str | Path,
    bar_layers: Optional[Sequence[str]] = None,
) -> Optional[Ladder]:
    """从 01-1 单线总图提取高度阶梯。

    国网版式实测（三塔一致）：01-1 的 H 向 DIM 按精确 x 排成多根「子列」，
    相邻子列成对（宽度列/高度列同 y 呼应，子列和相等）；其中最左的子列组
    是主阶梯——段高列（少数大值，总和 = 塔总高）+ 节拍列（多数小值，
    总和同 = 塔总高）。ZC1 的主阶梯另含累计锚（39400/33000 = 图纸明示标高）。

    选列规则（无 GT，三塔版式实测）：
        1. H 向 DIM 按容差 5 图面单位切成原子子列；
        2. 塔总高 total = 所有子列和的最大值（段高列/节拍列并列最大）；
        3. 阶梯区 = 总和 ≥ 0.99×total 的最左子列 + 其 x 邻域 25 单位内的
           全部子列（ZC1 实测锚列/节拍列 x 相差 8~17 单位）；
        4. 区内按 y 去重取值（同 y 多值取最大——节拍列 y 网格对齐），
           ≥ ``_CUM_ANCHOR_RATIO``×total 为累计锚，其余为节拍；
        5. 结点 = 累计锚（图纸明示标高）∪ {total} ∪ 自底向上节拍累计。
           ZC1 实测：锚 39400/33000 + 尾节拍对 (2800,3600) 推出 36600，
           塔身节拍因示意不按比例只作候选（段定位交给图纸指纹匹配）。
    """
    import ezdxf

    if bar_layers is None:
        bar_layers = ("0", "1", "2", "3", "4", "5", "7", "8")
    doc = ezdxf.readfile(str(dxf_path))
    rows = [r for r in _dim_rows(doc) if r[3] == "H" and r[0] > 0]
    if not rows:
        return None
    rows.sort(key=lambda r: r[1])
    # 原原子列（容差 5）：精确 x 的尺寸列
    atoms: List[List[Tuple[float, float, float, str]]] = [[rows[0]]]
    for r in rows[1:]:
        if r[1] - atoms[-1][-1][1] > _LADDER_SUBCOLUMN_TOL:
            atoms.append([r])
        else:
            atoms[-1].append(r)
    sums = [sum(v for v, *_ in a) for a in atoms]
    if not sums or max(sums) < 10000:
        return None  # 没有塔高量级的列
    total = max(sums)
    # 阶梯区：总和达 total 的最左子列 + x 邻域 25 单位内子列
    seed_idx = min(i for i, s in enumerate(sums) if s >= 0.99 * total)
    seed_x = atoms[seed_idx][0][1]
    zone_atoms = [a for a in atoms if abs(a[0][1] - seed_x) <= 25.0]
    if not zone_atoms:
        zone_atoms = [atoms[seed_idx]]
    # 节拍列 = 区内计数最多的子列（JC1 实测 27 格节拍列；ZC1 9 格）；
    # 累计锚从区内全部子列收（ZC1 的 39400/33000 在锚列/混合列里）
    beat_col = max(zone_atoms, key=len)
    zone_rows = [r for a in zone_atoms for r in a]
    anchors = sorted(set(v for v, _x, _y, _o in zone_rows
                         if v >= _CUM_ANCHOR_RATIO * total))
    beats = [v for v, _x, _y, _o in sorted(beat_col, key=lambda r: r[2])
             if _LADDER_BEAT_MIN_MM <= v < _CUM_ANCHOR_RATIO * total]
    junctions = {0.0, float(total)}
    junctions.update(anchors)
    # 节拍累计（自底向上，示意比例不可靠——只并入，不覆盖锚）
    acc = 0.0
    for b in beats:
        acc += b
        junctions.add(round(acc, 1))
    js = sorted(set(round(j, 1) for j in junctions if 0.0 <= j <= total + 400.0))
    return Ladder(beats=beats, junctions=js, cum_anchors=anchors,
                  source_stem=Path(dxf_path).stem)


def sheet_segment_evidence(
    dxf_path: str | Path,
    bar_layers: Optional[Sequence[str]] = None,
) -> Optional[SheetEvidence]:
    """分段立面图纸的塔身区证据。"""
    import ezdxf

    if bar_layers is None:
        bar_layers = ("0", "1", "2", "3", "4", "5", "7", "8")
    doc = ezdxf.readfile(str(dxf_path))
    zone = _tower_zone(doc, bar_layers)
    if zone is None:
        return None
    lo, hi, segs = zone
    ys = [c for s in segs for c in (s[1], s[3])]
    ty0, ty1 = min(ys), max(ys)
    marg = (hi - lo) * 0.7 + 5
    fingerprint: List[float] = []
    vdims: List[float] = []
    for v, x, y, orient in _dim_rows(doc):
        if not (lo - marg <= x <= hi + marg and ty0 - 5 <= y <= ty1 + 5):
            continue
        if orient == "H" and v >= _FINGERPRINT_MIN_MM:
            fingerprint.append(v)
        elif orient == "V" and v >= _FINGERPRINT_MIN_MM:
            vdims.append(v)
    return SheetEvidence(
        stem=Path(dxf_path).stem,
        h_draw=float(ty1 - ty0),
        w_draw=float(hi - lo),
        fingerprint_mm=sorted(set(fingerprint), reverse=True),
        vdim_mm=sorted(set(vdims), reverse=True),
    )


def _junction_windows(ladder: Ladder) -> List[Tuple[float, float]]:
    """相邻结点对窗口 [(z1, z2)]（跨度 ≥ 1500mm 才算一段）。"""
    js = ladder.junctions
    return [(js[i], js[i + 1]) for i in range(len(js) - 1)
            if js[i + 1] - js[i] >= 1500.0]


def _beat_support(window: Tuple[float, float], ladder: Ladder,
                  fingerprint: Sequence[float]) -> int:
    """窗口对指纹的支持数：窗口边界附近存在能拼接出 (z2-z1) 或边界的节拍。"""
    z1, z2 = window
    span = z2 - z1
    support = 0
    for fp in fingerprint:
        # 指纹值 == 段高 / 半段高 / 边界锚附近 → 支持
        if (abs(fp - span) <= 300.0
                or any(abs(fp - (j - z1)) <= 300.0 for j in ladder.junctions
                       if z1 <= j <= z2)
                or any(abs(fp - (z2 - j)) <= 300.0 for j in ladder.junctions
                       if z1 <= j <= z2)):
            support += 1
    return support


def _sheet_ladder_columns(dxf_path: str | Path,
                          bar_layers: Optional[Sequence[str]] = None,
                          ) -> List[List[float]]:
    """分段立面内的全部 H 向阶梯列（按图面 y 自底向上，mm）。

    35A1-ZC1 版式实测：每张分段立面左缘自带「段内阶梯列」——
    最大值为段高锚（例：5400 + 900/900/800/700×4），相邻列节拍
    错半位（800+1600×4+800），并集覆盖 GT 的半节拍层。

    提取规则：H 向 DIM 按 x 容差切列，只留「格数 ≥ 2 且列和 ≥ 4000」
    （段高量级）的列；列内按 y 升序原样累计（锚不特殊处理——04 页
    实测锚 6500 在 y 序中段，累计口径与 GT 验证一致）。
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    rows = [r for r in _dim_rows(doc)
            if r[3] == "H" and r[0] >= _LADDER_BEAT_MIN_MM]
    if not rows:
        return []
    rows.sort(key=lambda r: r[1])
    atoms: List[List[Tuple[float, float, float, str]]] = [[rows[0]]]
    for r in rows[1:]:
        if r[1] - atoms[-1][-1][1] > _LADDER_SUBCOLUMN_TOL * 2:
            atoms.append([r])
        else:
            atoms[-1].append(r)
    cols = []
    for a in atoms:
        beats = [v for v, _x, _y, _o in sorted(a, key=lambda r: r[2])]
        # 阶梯列判据：格数 ≥ 2 且列和达段高量级（07 半拍列 sum=4000 实测有效）
        if len(beats) >= 2 and sum(beats) >= 4000:
            cols.append(beats)
    return cols


@dataclass
class SegmentLadder:
    """一张分段立面的段内阶梯：锚 + 全列节拍并集结点（段内相对 z）。

    junctions 是全部阶梯列（主列+半拍列）按图面 y 序累计的并集（含 0），
    口径与 GT 验证一致（锚作为一格参与累计，不做特殊分离）。
    """

    stem: str
    anchor_mm: float  # 段高锚（列内最大值）
    beats: List[float]  # 主列节拍（签名去重用，不含锚）
    junctions: List[float]  # 全列累计并集（含 0；可超过锚——错位列累计）
    x_pos: float  # 主列 x（排序参考）


def segment_ladders_from_sheets(
    dxf_paths: Sequence[str | Path],
    bar_layers: Optional[Sequence[str]] = None,
    exclude_stems: Optional[Sequence[str]] = None,
) -> List[SegmentLadder]:
    """从各分段立面收集段内阶梯，签名去重重复视图（35A1-ZC1 版式）。

    重复视图判据（无 GT）：锚值 + 节拍多重集完全相同（07/11、10/13
    实测互为复制视图）。去重保留册号最小者。``exclude_stems`` 排除
    索引图（01-1——它的分段高度标注不是阶梯列，实测会污染堆叠首段）。
    """
    skip = {Path(s).stem for s in (exclude_stems or [])}
    segs: List[SegmentLadder] = []
    seen: Dict[Tuple[float, Tuple[float, ...]], str] = {}
    for p in sorted(dxf_paths):
        stem = Path(p).stem
        if stem in skip:
            continue
        cols = _sheet_ladder_columns(p, bar_layers)
        if not cols:
            continue
        main = max(cols, key=lambda c: sum(c))
        anchor = max(main)
        beats = sorted(v for v in main if v != anchor)
        jset = {0.0}
        for c in cols:
            acc = 0.0
            for v in c:
                acc += v
                jset.add(round(acc, 1))
        sig = (round(anchor, 1), tuple(round(b, 1) for b in beats))
        dup = seen.get(sig)
        if dup is not None:
            continue  # 重复视图（同段另一面/复制图），不重复计入
        seen[sig] = stem
        segs.append(SegmentLadder(
            stem=stem, anchor_mm=anchor, beats=beats,
            junctions=sorted(jset), x_pos=0.0))
    return segs


def distributed_ladder(
    dxf_paths: Sequence[str | Path],
    bar_layers: Optional[Sequence[str]] = None,
    ladder_sheet: Optional[str | Path] = None,
) -> Optional[Ladder]:
    """分散式整塔 z 链（35A1-ZC1 版式：01-1 无整塔阶梯的兜底）。

    版式背景（2026-09-04 诊断实测定稿）：35A1-ZC1 的 01-1 是分段索引图
    （H 向标注只有每站的接地节拍对与两个分段高度 4500/6400、4000/7000，
    无整塔阶梯列）；阶梯证据分散在各分段立面自带的段内阶梯列。

    重建规则（纯图纸，无 GT）：
        1. 各页提取段内阶梯列（``_sheet_ladder_columns``）；
        2. 节拍签名去重（锚+节拍多重集相同 = 重复视图，如 07/11）；
        3. 剩余页按册号序自底向上堆叠（国网分段立面惯例），
           段边界 = 锚前缀和，整塔 junctions = 各段全列节拍累计并集
           （平移到绝对 z）。

    已知限制（Phase 4 记录）：主链截止页无独立版面信号——当前取全部
    去重后页面堆叠（35A1-ZC1 实测 9 页和 58800 超塔高），需配合
    ``assign_z_chain`` 的段高互证约束裁剪；塔头细节页（09/12 等）
    不分配 z 链。
    """
    segs = segment_ladders_from_sheets(
        dxf_paths, bar_layers,
        exclude_stems=[ladder_sheet] if ladder_sheet else None)
    if len(segs) < 2:
        return None
    junctions = {0.0}
    acc = 0.0
    for s in segs:
        # 段内 junctions（含 0）平移到绝对 z：先平移再加锚，
        # 否则段自身的锚会被重复叠加
        junctions.update(round(acc + j, 1) for j in s.junctions)
        acc += s.anchor_mm
        junctions.add(round(acc, 1))
    js = sorted(j for j in junctions if j >= 0)
    return Ladder(beats=[s.anchor_mm for s in segs],
                  junctions=js, cum_anchors=[], source_stem="distributed")


def assign_z_chain(
    dxf_paths: Sequence[str | Path],
    ladder_sheet: str | Path,
    bar_layers: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, float]]:
    """整册 z 链分配：返回 {stem: {z_offset, z_span_mm}}（未匹配的 stem 缺席）。

    匹配（纯图纸证据，三信号合流）：
        1. 节拍指纹：sheet 塔身区横向大标注与阶梯节拍重合 → 支持分；
        2. 段高互证：窗口跨度 (z2-z1) 与 sheet 竖向大标注（自身段高）接近；
        3. 互斥约束：一张图一个窗口，多图抢同窗时高分胜出（塔身分段互斥）。

    35A1-ZC1 版式兜底：``ladder_from_sheet`` 失败（01-1 无整塔阶梯）时
    走 ``distributed_ladder``——各分段立面自带段内阶梯列直接堆叠，
    跳过窗口匹配（每页的 z_offset/z_span 来自堆叠位置本身）。
    """
    ladder = ladder_from_sheet(ladder_sheet, bar_layers)
    if ladder is None or not ladder.junctions:
        # 分散式兜底：阶梯在各分段立面里（排除索引图 ladder_sheet）
        segs = segment_ladders_from_sheets(
            dxf_paths, bar_layers, exclude_stems=[ladder_sheet])
        if len(segs) < 2:
            return {}
        out: Dict[str, Dict[str, float]] = {}
        acc = 0.0
        for s in segs:
            out[s.stem] = {
                "z_offset": float(acc),
                "z_span_mm": float(s.anchor_mm),
                "match_score": 0.0,
            }
            acc += s.anchor_mm
        return out
    windows = _junction_windows(ladder)
    if not windows:
        return {}

    evidences: List[SheetEvidence] = []
    for p in dxf_paths:
        stem = Path(p).stem
        if stem == Path(ladder_sheet).stem:
            continue
        ev = sheet_segment_evidence(p, bar_layers)
        if ev is None or ev.h_draw <= 0:
            continue
        # 只对有显著指纹/竖向段高的图参与（真分段立面；加工图自然出局）
        if not ev.fingerprint_mm and not ev.vdim_mm:
            continue
        evidences.append(ev)

    # 评分矩阵
    scores: List[Tuple[float, int, int]] = []  # (score, ev_idx, win_idx)
    for ei, ev in enumerate(evidences):
        for wi, win in enumerate(windows):
            z1, z2 = win
            span = z2 - z1
            supp = _beat_support(win, ladder, ev.fingerprint_mm)
            # 段高互证：竖向大标注最接近 span 的
            vhit = 0.0
            for vd in ev.vdim_mm:
                rel = abs(vd - span) / span
                if rel <= _HEIGHT_TOL:
                    vhit = max(vhit, 1.0 - rel / _HEIGHT_TOL)
            # 图面高比例：窗口跨度占塔总高的比例 ≈ h_draw/图幅高（弱信号，0.2 权重）
            score = supp + 2.0 * vhit
            if score > 0:
                scores.append((score, ei, wi))

    # 贪心互斥分配（高分先占）
    scores.sort(reverse=True)
    used_ev: set = set()
    used_win: set = set()
    out: Dict[str, Dict[str, float]] = {}
    for score, ei, wi in scores:
        if ei in used_ev or wi in used_win or score <= 0:
            continue
        used_ev.add(ei)
        used_win.add(wi)
        z1, z2 = windows[wi]
        out[evidences[ei].stem] = {
            "z_offset": float(z1),
            "z_span_mm": float(z2 - z1),
            "match_score": float(score),
        }
    return out
