# -*- coding: utf-8 -*-
"""LevelGridSolver：DXF 证据自推层网格（P2）。

设计冻结 v1 与实测数字见 docs/LEVEL_GRID_SOLVER_DESIGN.md（2026-09-05）。
目标：替换三张 GT 层表（terminal/platform/diaphragm override）与
tower_geometry.generate_diaphragms 的硬编码层常数——层位由图纸证据
投票产生，每层带 provenance（册/origin/杆数），无塔专属常数。

纪律边界（对照 P2.6 注入撤回教训，design doc §7）：
- 本模块不 import gt_profile、不读任何 GT 文件——GT 只进
  scripts/validate_level_grid.py 的离线验证；
- leg_synth / marker_synth 杆端点不投票：leg_synth 的 span 表是被撤回
  的注入通道，marker_synth 的文本证据已由锚层（beam_marker_levels）
  表达，再投票等于双计；
- 参数（桶宽/链距/吸收半径）由提取噪声尺度决定，两塔共用，不按塔调。

算法（v6 原型冻结）：
1. 每册端点 z 直方图（100mm 桶，按 origin 加权）；
2. 核桶 = ≥2 独立杆的桶（单杆桶只加权不连链——防稀疏桥把 7000-11400
   糊成 89 杆巨簇，2026-09-05 实测教训）；
3. 核桶链 gap≤400；
4. 链内贪心剥峰：最强核发射为层 z（众数桶，不用均值——桶均值会把
   层位拉偏 100-700mm），±400 吸收其余核，循环；
5. 锚骨架：标注层(marker,w3) + 册边界(boundary,w2)，±300 去重
   （marker 值优先——ZC1 的 datum 是实测非整值，标注层更准）；
6. 几何补位：按权重降序，离骨架任一层 ≥400 才加入。
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常数（一般参数：提取噪声尺度决定，两塔共用）
# ---------------------------------------------------------------------------
ORIGIN_WEIGHTS: Dict[str, int] = {
    "dxf_geom": 2,      # 直读画线端点
    "diag_synth": 1,    # 画线拓扑补全（纯口径已承认）
    "diag_complete": 1,
}
# 明确排除：leg_synth（表驱动，P2.6 注入通道）、marker_synth 杆
# （文本证据由锚层表达，防双计）。

BUCKET_MM = 100          # 直方图桶宽
NUCLEUS_MIN_BARS = 2     # 核桶判据：独立杆数
CHAIN_GAP_MM = 400       # 核桶链距（< 最小真实层间距 500）
ABSORB_MM = 400          # 峰吸收半径（14400/14500=100 允许并，8000/8500=500 不吞）
ANCHOR_DEDUP_MM = 300    # 锚层去重距离
FILL_MIN_DIST_MM = 400   # 几何补位与骨架最小距离
MARKER_WEIGHT = 3
BOUNDARY_WEIGHT = 2
BEAT_WEIGHT = 2           # 尺寸标注节拍（dimension_beat_anchors，标注类证据）

_KIND_PRIORITY = {"marker": 2, "beat": 1, "boundary": 1, "geom": 0}


def _sheet_endpoint_histogram(
    endpoints: Sequence[Tuple[float, str, str]],
) -> Tuple[Dict[int, int], Dict[int, set]]:
    """端点 (z, bar_id, origin) → (桶权重, 桶独立杆集合)。

    origin 不在 ORIGIN_WEIGHTS 的端点**不进桶**——leg_synth（表驱动，
    P2.6 注入通道）等排除类从入口阻断，不靠权重归零兜底。
    """
    buckets: Dict[int, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for z, bid, org in endpoints:
        if org not in ORIGIN_WEIGHTS:
            continue
        buckets[int(round(z / BUCKET_MM)) * BUCKET_MM][org].add(bid)
    bw: Dict[int, int] = {
        z: sum(len(ids) * ORIGIN_WEIGHTS[o] for o, ids in b.items())
        for z, b in buckets.items()
    }
    bars: Dict[int, set] = {
        z: set().union(*b.values()) for z, b in buckets.items()
    }
    return bw, bars


def _peel_peaks(
    bw: Dict[int, int], bars: Dict[int, set], sheet: str
) -> List[dict]:
    """核桶链 + 贪心剥峰。返回 [{z, weight, n_bars, sheet}]。"""
    nuclei = [z for z in sorted(bw) if len(bars[z]) >= NUCLEUS_MIN_BARS]
    if not nuclei:
        return []
    chains: List[List[int]] = []
    cur = [nuclei[0]]
    for z in nuclei[1:]:
        if z - cur[-1] <= CHAIN_GAP_MM:
            cur.append(z)
        else:
            chains.append(cur)
            cur = [z]
    chains.append(cur)

    out: List[dict] = []
    for ch in chains:
        pool = list(ch)
        while pool:
            peak = max(pool, key=lambda z: (bw[z], -z))
            group = [z for z in pool if abs(z - peak) <= ABSORB_MM]
            # 弱桶（单杆）在 ±250 内只加权，不改变峰位
            gbars: set = set()
            for z in sorted(bw):
                if any(abs(z - p) <= 250 for p in group):
                    gbars |= bars[z]
            out.append({
                "z": float(peak),
                "weight": int(sum(bw[z] for z in group)),
                "n_bars": len(gbars),
                "sheet": sheet,
            })
            pool = [z for z in pool if z not in group]
    return out


def vote_level_grid(
    sheet_endpoints: Dict[str, List[Tuple[float, str, str]]],
    marker_levels: Dict[str, List[float]],
    z_offsets: Dict[str, float],
    beat_anchors: Optional[Dict[str, List[float]]] = None,
) -> Tuple[List[float], List[dict]]:
    """投票层网格主入口（纯函数，无 IO）。

    参数
    ----
    sheet_endpoints : {册名: [(z, bar_id, geometry_origin), ...]}
        端点 z 需已复原（view_y + z_offset）。
    marker_levels : {册名: [标注层 z]}（beam_marker_levels_mm）
    z_offsets : {册名: datum z}（view_regions，仅高程册）
    beat_anchors : {册名: [尺寸节拍 z]}（dimension_beat_anchors，
        图纸尺寸链节拍——标注类证据，与 marker 文本/几何端点独立）

    返回
    ----
    (levels, records)——levels 升序；records 每层一条 provenance：
    {z, kind, weight, n_bars, sheets, origins}（geom 层带 origins 计数）。
    """
    # 1) 锚骨架
    anchors: List[Tuple[float, int, str, str]] = []  # (z, w, kind, sheet)
    for sheet, lvs in marker_levels.items():
        for z in lvs:
            anchors.append((float(z), MARKER_WEIGHT, "marker", sheet))
    for sheet, beats in (beat_anchors or {}).items():
        for z in beats:
            anchors.append((float(z), BEAT_WEIGHT, "beat", sheet))
    for sheet, zoff in z_offsets.items():
        if zoff:
            anchors.append((float(zoff), BOUNDARY_WEIGHT, "boundary", sheet))
    anchors.sort(key=lambda a: (a[0], -_KIND_PRIORITY[a[2]]))

    grid: List[dict] = []
    for z, w, kind, sheet in anchors:
        if grid and z - grid[-1]["z"] <= ANCHOR_DEDUP_MM:
            g = grid[-1]
            g["weight"] += w
            g["sheets"].append(sheet)
            # 代表值优先级：marker > boundary
            if _KIND_PRIORITY[kind] > _KIND_PRIORITY[g["kind"]]:
                g["z"], g["kind"] = z, kind
        else:
            grid.append({"z": z, "kind": kind, "weight": w,
                         "n_bars": 0, "sheets": [sheet], "origins": {}})

    # 2) 几何投票层
    geo: List[dict] = []
    for sheet, eps in sheet_endpoints.items():
        if sheet not in z_offsets:
            continue  # 非高程册（z 未复原），不入投票
        bw, bars = _sheet_endpoint_histogram(eps)
        for rec in _peel_peaks(bw, bars, sheet):
            geo.append(rec)

    # 3) 补位（权重降序，离骨架 ≥400）
    for rec in sorted(geo, key=lambda r: (-r["weight"], r["z"])):
        if any(abs(rec["z"] - g["z"]) < FILL_MIN_DIST_MM for g in grid):
            continue
        grid.append({"z": rec["z"], "kind": "geom", "weight": rec["weight"],
                     "n_bars": rec["n_bars"], "sheets": [rec["sheet"]],
                     "origins": {}})

    grid.sort(key=lambda g: g["z"])
    return [g["z"] for g in grid], grid


# ---------------------------------------------------------------------------
# sheets/ 中间产物加载（验证脚本与测试用；管线内集成走内存接口）
# ---------------------------------------------------------------------------
def endpoints_from_sheet_model(model: dict, z_offset: float) -> List[Tuple[float, str, str]]:
    """从单册 sheets/*.json 模型提取可投票端点（z = view_y + datum）。"""
    nodes: Dict[str, float] = {}
    for cid, c in (model.get("components") or {}).items():
        if c.get("kind") != "tower_node":
            continue
        pr = c.get("properties") or {}
        vy = pr.get("view_y")
        if vy is None:
            continue
        nodes[cid] = float(vy) + float(z_offset)
    eps: List[Tuple[float, str, str]] = []
    for cid, c in (model.get("components") or {}).items():
        if c.get("kind") != "tower_bar":
            continue
        pr = c.get("properties") or {}
        org = pr.get("geometry_origin")
        if org not in ORIGIN_WEIGHTS:
            continue  # leg_synth 等排除类（loader 层同样过滤，双保险）
        for nid in (pr.get("from_node"), pr.get("to_node")):
            if nid in nodes:
                eps.append((nodes[nid], cid, org))
    return eps


def beat_anchors_from_cross_file(model: dict) -> Dict[str, List[float]]:
    """从 cross_file 模型的 drawing_file.properties 提取尺寸节拍锚。

    dimension_beat_anchors_by_sheet：{册: {"z": [...], ...}}——图纸
    尺寸链节拍（设计师画的标高节拍），与 marker 文本/几何端点独立的
    第三证据源。仅取 n_beats>0 的真实节拍（region_span_linear 退化为
    端点两值的册不贡献中间层）。
    """
    out: Dict[str, List[float]] = {}
    for c in (model.get("components") or {}).values():
        if c.get("kind") != "drawing_file":
            continue
        by_sheet = (c.get("properties") or {}).get(
            "dimension_beat_anchors_by_sheet") or {}
        for sheet, rec in by_sheet.items():
            if not isinstance(rec, dict) or not rec.get("z"):
                continue
            zs = [float(z) for z in rec["z"] if z is not None]
            if len(zs) >= 3:  # 退化模式（仅端点两值）不投票
                out[sheet] = zs
    return out


def grid_from_sheets_dir(
    sheets_dir: Path, overlay: dict, cross_file_model: Optional[dict] = None,
) -> Tuple[List[float], List[dict], List[str]]:
    """从交付 sheets/ 目录 + overlay 配置构建投票网格。

    cross_file_model：可选 cross_file/model.json dict——提供时启用尺寸
    节拍锚（beat_anchors）第三证据源。
    返回 (levels, records, warnings)。仅 view_regions 里有 datum 的册
    参与（其余册 z 不可复原，记 warning）。
    """
    sheets_dir = Path(sheets_dir)
    z_offsets: Dict[str, float] = {}
    for stem, regs in (overlay.get("view_regions") or {}).items():
        if not isinstance(regs, list):
            continue
        for r in regs:
            if isinstance(r, dict) and r.get("z_offset") is not None:
                z = float(r["z_offset"])
                if z > 0:  # datum=0.0 是未标定册占位（如 JC1-40 基础详图）
                    z_offsets[stem] = z
                break  # 同册多 region（front/side）datum 一致，取首个非空
    marker_levels: Dict[str, List[float]] = {}
    for stem, cfg in (overlay.get("centerline_extract") or {}).items():
        if isinstance(cfg, dict):
            marker_levels[stem] = [
                float(z) for z in (cfg.get("beam_marker_levels_mm") or [])
            ]

    sheet_endpoints: Dict[str, List[Tuple[float, str, str]]] = {}
    warnings: List[str] = []
    for path in sorted(sheets_dir.glob("*.json")):
        stem = path.stem
        if stem not in z_offsets:
            warnings.append(f"{stem}: 无 datum（view_regions），跳过投票")
            continue
        model = _load_json(path)
        if model is None:
            warnings.append(f"{stem}: 解析失败，跳过")
            continue
        sheet_endpoints[stem] = endpoints_from_sheet_model(model, z_offsets[stem])

    beat = beat_anchors_from_cross_file(cross_file_model) if cross_file_model else {}
    levels, records = vote_level_grid(
        sheet_endpoints, marker_levels, z_offsets, beat_anchors=beat)
    return levels, records, warnings


def _load_json(path: Path) -> Optional[dict]:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
