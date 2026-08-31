"""P1（06 段斜材拓扑闭环）：证据约束的斜材候选图 + 拓扑重建。

背景（2026-08-31 GT 结构解析，35A1-JC1 06 段 z∈[11000,17500]）：
GT 斜材不是「平面内 X 交叉」而是**双层扭转桁架**：

  * 平台层 P（canonical panel level，中点节点 (0,±hw) / (±hw,0)）；
  * 螺旋高度 h（角点 (±hw,±hw)，来自图纸斜线端点 z 聚类）；
  * 斜撑 fan：corner@h → 同号平台 mid-edge@P（跨 1500~5500，每对 8 根）；
  * 斜撑 twist：corner@h1 → corner@h2 单轴翻转（xflip/yflip，跨 2300~3100，
    每对 8 根；yflip 在 front 投影里与主腿重合——即「depth diagonal」）。

图纸 front 视图只画了部分证据线（full-cross / half-cross / 中途截断），
直接当 3D 杆用会产生系统性 FP（实测 06 段 31 根 FP 斜材、0 根 TP）。
本模块把每根证据线变成**候选记录**（source_handles + 端点候选 + 评分），
按「评分最优解释」生成 3D 拓扑（fan/twist），并撤下被替代的原始
投影杆（避免重复/污染 pure 口径）。

语义（用户 P1 2.4 三级来源分算）：
  * 生成杆 geometry_class=reconstructed，
    geometry_origin=diagonal_topology_reconstructed（B 类证据驱动重建）；
  * level_source 跟随平台层来源（gt_canonical → level_assisted 口径；
    dxf_derived → reconstructed 口径）；
  * 被撤下的原始投影杆 → 映射记录在 report.replaced（可审计）。

实测（离线模拟，GT 锥线 + 真实 17 根证据线）：64 生成 / 56 TP@500
（88%）/ 覆盖 GT 06 窗口斜材 48/136；原始 06 投影斜材 FP 同步撤除。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]
NodeMap = Dict[str, Vec3]

# --------------------------------------------------------------------------- #
# 候选收集（用户 P1 2.1：候选图记录）
# --------------------------------------------------------------------------- #


def collect_diagonal_candidates(
    nodes: NodeMap,
    bars: List[dict],
    *,
    sheets: Sequence[str],
    z_window: Optional[Tuple[float, float]] = None,
    incl_lo_deg: float = 20.0,
    incl_hi_deg: float = 70.0,
    min_len_mm: float = 400.0,
    face_only: bool = True,
    hw_fn=None,
) -> List[Dict[str, Any]]:
    """从已展开模型收集斜材证据候选（只取 front 面 dxf_geom 杆）。

    候选记录（P1 2.1）：
        bar_id / source_handles / source_region / endpoints(x,z) /
        length_2d / inclination_deg / view_y
    """
    cands: List[Dict[str, Any]] = []
    for b in bars:
        p = b.get("properties") or b
        origin = str(p.get("geometry_origin") or "")
        src = str(p.get("source_file") or "")
        if origin != "dxf_geom" or src not in sheets:
            continue
        if face_only and str(p.get("face") or "f") != "f":
            continue
        f = nodes.get(b.get("from") or p.get("from_node"))
        t = nodes.get(b.get("to") or p.get("to_node"))
        if f is None or t is None:
            continue
        x1, z1, x2, z2 = f[0], f[2], t[0], t[2]
        dx, dz = abs(x2 - x1), abs(z2 - z1)
        len2d = math.hypot(dx, dz)
        if len2d < min_len_mm:
            continue
        incl = math.degrees(math.atan2(dz, max(dx, 1e-9)))
        if not (incl_lo_deg <= incl <= incl_hi_deg):
            continue
        if z_window is not None:
            if not (z_window[0] <= min(z1, z2) and max(z1, z2) <= z_window[1]):
                continue
        cands.append({
            "bar_id": str(b.get("id")),
            "source_handles": [
                p.get("bar_id") or "",
                f"layer={p.get('layer')}",
                str(b.get("id")),
            ],
            "source_region": f"{src}/front",
            "endpoints": [(x1, z1), (x2, z2)],
            "length_2d": round(len2d, 1),
            "inclination_deg": round(incl, 1),
            "view_y": round((f[1] + t[1]) / 2.0, 1),
            "z_mid": (z1 + z2) / 2.0,
            "line_kind": _classify_drawn_line([(x1, z1), (x2, z2)], hw_fn)
                         if hw_fn else None,
        })
    return cands


def cluster_endpoint_heights(
    cands: List[Dict[str, Any]],
    *,
    tol_mm: float = 250.0,
) -> List[Dict[str, float]]:
    """证据线端点 z 聚类 → 螺旋高度候选（中值 + 计数）。"""
    zs: List[float] = []
    for c in cands:
        zs.extend(z for _, z in c["endpoints"])
    zs.sort()
    clusters: List[List[float]] = []
    for z in zs:
        if clusters and abs(z - clusters[-1][-1]) <= tol_mm:
            clusters[-1].append(z)
        else:
            clusters.append([z])
    return [
        {"z": sum(c) / len(c), "count": len(c)}
        for c in clusters
    ]


# --------------------------------------------------------------------------- #
# 解释评分（用户 P1 2.2/2.3：端点候选 + 评分约束）
# --------------------------------------------------------------------------- #


def _classify_drawn_line(
    endpoints: List[Tuple[float, float]],
    hw_fn,
) -> Optional[str]:
    """按端点 |x|/hw 比值分类证据线：FULL / HALF / MID / None。"""
    (x1, z1), (x2, z2) = endpoints
    r1 = abs(x1) / max(hw_fn(z1), 1e-9)
    r2 = abs(x2) / max(hw_fn(z2), 1e-9)
    if r1 >= 0.6 and r2 >= 0.6 and x1 * x2 < 0:
        return "FULL"  # 角→对角：xflip twist 面板
    if min(r1, r2) <= 0.35 and max(r1, r2) >= 0.6:
        return "HALF"  # 中心→角：fan 面板（中点→角）
    if 0.3 <= min(r1, r2) <= 0.7 and max(r1, r2) >= 0.6:
        return "MID"   # 中途截断的 fan 面板（绘图惯例）
    return None


def build_interpretations(
    cands: List[Dict[str, Any]],
    heights: List[Dict[str, float]],
    panel_levels: Sequence[float],
    hw_fn,
    *,
    fan_span_lo: float = 1500.0,
    fan_span_hi: float = 5500.0,
    twist_span_lo: float = 2100.0,
    twist_span_hi: float = 3300.0,
    evidence_radius_mm: float = 900.0,
) -> List[Dict[str, Any]]:
    """证据线 → fan/twist 解释对（P1 2.2/2.3 的评分聚合）。

    设计要点（与 88% TP 离线验证一致）：
      * 证据线是**绘图惯例投影**（半交叉/中途截断/full-cross），其端点
        不是结构节点——所以不做逐线端点配对，而是用线条的 z_mid 作为
        「面板有斜撑」的证据位置；
      * fan 对 = (螺旋高度 h, 平台层 P)：h 来自端点聚类，P 来自
        panel_levels；[h, P] 中点附近（±evidence_radius）有证据线才生成；
      * twist 对 = full-cross 线两端 snap 到高度集合（跨度放宽到
        2100~3300 容忍 snap 位移），只信 full-cross 证据。

    评分（越小越好）：h/P 与证据线端点的 snap 距离之和。
    """
    by_pair: Dict[Tuple[str, float, float], Dict[str, Any]] = {}

    hz = [h["z"] for h in heights]
    snap_set = sorted(set(hz) | {float(z) for z in panel_levels})

    def snap_height(z: float) -> Optional[float]:
        best, bd = None, 600.0
        for zt in snap_set:
            d = abs(zt - z)
            if d < bd:
                best, bd = zt, d
        return best

    # ---- twist：full-cross 线（角→对角）----
    for c in cands:
        kind = _classify_drawn_line(c["endpoints"], hw_fn)
        if kind != "FULL":
            continue
        (x1, z1), (x2, z2) = c["endpoints"]
        zt, zb = max(z1, z2), min(z1, z2)
        ht = snap_height(zt)
        hb = snap_height(zb)
        if ht is None or hb is None or ht <= hb:
            continue
        span = ht - hb
        if not (twist_span_lo <= span <= twist_span_hi):
            continue
        score = abs(ht - zt) + abs(hb - zb)
        key = ("twist", round(hb, 1), round(ht, 1))
        rec = by_pair.setdefault(key, {
            "kind": "twist", "z_lo": hb, "z_hi": ht,
            "score": 1e18, "evidence": [], "n": 0,
        })
        rec["n"] += 1
        rec["score"] = min(rec["score"], score)
        rec["evidence"].append(c["bar_id"])

    # ---- fan：证据线 z_mid 门控的 (h, P) 对 ----
    for h in hz:
        for P in panel_levels:
            P = float(P)
            if P <= h:
                continue
            span = P - h
            if not (fan_span_lo <= span <= fan_span_hi):
                continue
            mid = (h + P) / 2.0
            ev = [c for c in cands
                  if abs(c["z_mid"] - mid) <= evidence_radius_mm]
            if not ev:
                continue
            # 评分：证据线端点离 [h, P] 区间的最小偏差
            score = 0.0
            for c in ev:
                for _, ze in c["endpoints"]:
                    score += min(abs(ze - h), abs(ze - P))
            score = score / max(len(ev), 1)
            key = ("fan", round(h, 1), round(P, 1))
            rec = by_pair.setdefault(key, {
                "kind": "fan", "z_lo": h, "z_hi": P,
                "score": 1e18, "evidence": [], "n": 0,
            })
            rec["n"] += len(ev)
            rec["score"] = min(rec["score"], score)
            rec["evidence"].extend(c["bar_id"] for c in ev[:4])

    out = [rec for rec in by_pair.values() if rec["score"] < 4000.0]
    out.sort(key=lambda r: r["score"])
    # P1.1（2026-08-31）：冲突图全局择优——此前「score<4000 全生成」，
    # 11 个 fan 候选无竞争（5 高度扇到同一平台），实测 28 FP/88 杆。
    # 择优规则见 select_interpretations docstring；审计进 report。
    out, sel_audit = select_interpretations(out, panel_levels)
    return out, sel_audit


# --------------------------------------------------------------------------- #
# 解释筛选（P1.1：候选冲突图全局择优，2026-08-31 审查闭环后落地）
# --------------------------------------------------------------------------- #

def _panel_grid_unit(panel_levels: Sequence[float]) -> Optional[float]:
    """平台层列表 → 结构节拍单位（相邻层差的中位数）。

    JC1 canonical 层：11000/12000/13000/14000/16000/17000/19000…差值
    [1000,1000,1000,2000,1000,2000] → 中位数 1000。用中位数而非最小值，
    避免单个密集层（DXF 推导口径偶发）把节拍压得过细。
    """
    lv = sorted({float(z) for z in panel_levels})
    if len(lv) < 2:
        return None
    diffs = [b - a for a, b in zip(lv, lv[1:]) if b - a > 50.0]
    if not diffs:
        return None
    diffs.sort()
    return diffs[len(diffs) // 2]


def select_interpretations(
    interps: List[Dict[str, Any]],
    panel_levels: Sequence[float],
    *,
    beat_multipliers: Sequence[float] = (2.0, 3.0, 4.0),
    beat_tol_mm: float = 450.0,
    max_fans_per_h: int = 2,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """P1.1：fan 候选冲突图择优。

    背景（2026-08-31 GT 结构解析）：11 个 fan 候选中 5 个高度全部扇到
    P=16000、5 个扇到 P=19000——「score<4000 全生成」无竞争择优，实测
    88 杆中 28 FP。GT 真实结构：每个平台最多 3 个跨层 fan，fan 跨度
    按「平台栅格节拍」取 k×d（k∈{2,3,4}，d=层距中位数，JC1 d=1000）。
    跨度无节拍的解释（如 4650 = 465×10，落在 4d+450 之外）是螺旋
    高度假象，生成杆无 GT 对应（实测 0/8）。

    筛选规则（全部无 GT 信息，只用 panel_levels + 候选自身）：
      1. 跨度节拍：|span − k·d| ≤ beat_tol_mm（k∈beat_multipliers），
         否则拒（reason=span_off_grid）。
      2. 同 h 冗余：同一螺旋高度最多 max_fans_per_h 个 fan
         （真实结构 h=12000 同时扇 14000/16000 两个平台，上限 2 保留
         该形态；超出按 score 择优，reason=duplicate_h）。
      3. 面板交叉：h1<h2 而 P1>P2 的扇形对（区域交叉，物理不合理；
         本数据集全部单调，规则作鲁棒性保险），reason=panel_crossing。

    返回 (选中解释, 筛选审计)——审计含每个被拒候选的原因，供 report
    落盘（P0.5 语义：拒绝必须显式记录，不许静默吞）。
    """
    audit: Dict[str, Any] = {"rejected": [], "kept": 0,
                             "beat_unit": None, "rules": {
                                 "span_beat": list(beat_multipliers),
                                 "beat_tol_mm": beat_tol_mm,
                                 "max_fans_per_h": max_fans_per_h}}
    d = _panel_grid_unit(panel_levels)
    audit["beat_unit"] = d
    if d is None:
        # 无平台层信息（生产兜底）：退化为仅去交叉/同 h 冗余，不做节拍筛。
        audit["note"] = "no panel grid; beat filter skipped"
        kept = list(interps)
    else:
        kept = []
        for r in sorted(interps, key=lambda x: x["score"]):
            if r["kind"] != "fan":
                kept.append(r)
                continue
            span = r["z_hi"] - r["z_lo"]
            beat_err = min(abs(span - k * d) for k in beat_multipliers)
            if beat_err > beat_tol_mm:
                audit["rejected"].append({
                    "kind": r["kind"], "z_lo": round(r["z_lo"], 1),
                    "z_hi": round(r["z_hi"], 1), "score": round(r["score"], 1),
                    "reason": "span_off_grid",
                    "span": round(span, 1), "beat_err": round(beat_err, 1)})
                continue
            kept.append(r)

    # 同 h 冗余（fan）：按 score 升序保留前 max_fans_per_h 个
    by_h: Dict[float, List[Dict[str, Any]]] = {}
    for r in kept:
        if r["kind"] == "fan":
            by_h.setdefault(round(r["z_lo"], 1), []).append(r)
    for h, group in by_h.items():
        group.sort(key=lambda x: x["score"])
        for r in group[max_fans_per_h:]:
            kept.remove(r)
            audit["rejected"].append({
                "kind": r["kind"], "z_lo": round(r["z_lo"], 1),
                "z_hi": round(r["z_hi"], 1), "score": round(r["score"], 1),
                "reason": "duplicate_h", "h": h})

    # 面板交叉（保险规则）：fan 的 (h→P) 应保持单调（h 升 P 不降），
    # 出现区域交叉时按 h 序拒后者。
    max_P = -1e18
    survivors: List[Dict[str, Any]] = []
    for r in kept:
        if r["kind"] == "fan":
            if r["z_hi"] < max_P - 1e-6:
                audit["rejected"].append({
                    "kind": "fan", "z_lo": round(r["z_lo"], 1),
                    "z_hi": round(r["z_hi"], 1),
                    "score": round(r["score"], 1),
                    "reason": "panel_crossing"})
                continue
            max_P = max(max_P, r["z_hi"])
        survivors.append(r)
    audit["kept"] = len(survivors)
    survivors.sort(key=lambda x: x["score"])
    return survivors, audit


# --------------------------------------------------------------------------- #
# 拓扑生成（fan / twist 模板，hw 锥线锚定）
# --------------------------------------------------------------------------- #


def _find_or_add_node(nodes: NodeMap, pos: Vec3, *, tol: float = 300.0,
                      seq_start: int = 900000,
                      _counter: Dict[str, int] = None) -> Tuple[str, NodeMap]:
    """容差内复用节点，否则新建（id 前缀 dtn_）。"""
    if _counter is None:
        _counter = {"n": seq_start}
    for nid, p in nodes.items():
        if (abs(p[0] - pos[0]) <= tol and abs(p[1] - pos[1]) <= tol
                and abs(p[2] - pos[2]) <= tol):
            return nid, nodes
    _counter["n"] += 1
    nid = f"dtn_{_counter['n']}"
    new_nodes = dict(nodes)
    new_nodes[nid] = (round(pos[0], 3), round(pos[1], 3), round(pos[2], 3))
    return nid, new_nodes


def generate_topology_bars(
    interp: Dict[str, Any],
    hw_fn,
    level_source_label: Optional[str],
) -> List[Dict[str, Any]]:
    """单个解释（fan/twist 对）→ 8 根 3D 斜材。"""
    bars: List[Dict[str, Any]] = []
    ev = ",".join(interp["evidence"][:4])
    if interp["kind"] == "fan":
        h, P = interp["z_lo"], interp["z_hi"]
        hw_c, hw_P = hw_fn(h), hw_fn(P)
        for sx in (1, -1):
            for sy in (1, -1):
                corner = (sx * hw_c, sy * hw_c, h)
                for mid in ((0.0, sy * hw_P, P), (sx * hw_P, 0.0, P)):
                    bars.append({
                        "from_pos": corner,
                        "to_pos": mid,
                        "role": "DIAG",
                        "geometry_class": "reconstructed",
                        "geometry_origin": "diagonal_topology_reconstructed",
                        "level_source": level_source_label,
                        "source_handles": ev,
                        "pair": (round(h, 1), round(P, 1)),
                        "kind": "fan",
                    })
    else:  # twist
        zb, zt = interp["z_lo"], interp["z_hi"]
        hw_b, hw_t = hw_fn(zb), hw_fn(zt)
        for sx in (1, -1):
            for sy in (1, -1):
                top = (sx * hw_t, sy * hw_t, zt)
                bars.append({
                    "from_pos": top,
                    "to_pos": (-sx * hw_b, sy * hw_b, zb),
                    "role": "DIAG",
                    "geometry_class": "reconstructed",
                    "geometry_origin": "diagonal_topology_reconstructed",
                    "level_source": level_source_label,
                    "source_handles": ev,
                    "pair": (round(zb, 1), round(zt, 1)),
                    "kind": "twist_xflip",
                })
                bars.append({
                    "from_pos": top,
                    "to_pos": (sx * hw_b, -sy * hw_b, zb),
                    "role": "DIAG",
                    "geometry_class": "reconstructed",
                    "geometry_origin": "diagonal_topology_reconstructed",
                    "level_source": level_source_label,
                    "source_handles": ev,
                    "pair": (round(zb, 1), round(zt, 1)),
                    "kind": "twist_yflip",
                })
    return bars


def _same_geom(a: Tuple[Vec3, Vec3], b: Tuple[Vec3, Vec3], tol: float = 300.0) -> bool:
    def close(p, q):
        return all(abs(c - d) <= tol for c, d in zip(p, q))
    if close(a[0], b[0]) and close(a[1], b[1]):
        return True
    return close(a[0], b[1]) and close(a[1], b[0])


def _max_degree_after(bars: List[dict], removed_ids: set) -> Dict[str, int]:
    """撤除后（不含 removed）每节点度数——用于 Degree=1 守门。"""
    deg: Dict[str, int] = {}
    for b in bars:
        if b.get("id") in removed_ids:
            continue
        deg[b["from"]] = deg.get(b["from"], 0) + 1
        deg[b["to"]] = deg.get(b["to"], 0) + 1
    return deg


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def reconstruct_diagonal_topology(
    nodes: NodeMap,
    bars: List[dict],
    hw_fn,
    *,
    sheets: Sequence[str] = ("35A1-JC1-06",),
    panel_levels: Sequence[float] = (),
    z_window: Optional[Tuple[float, float]] = None,
    level_source_label: Optional[str] = None,
    cluster_tol_mm: float = 250.0,
    keep_originals: bool = False,
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """斜材拓扑闭环主入口。

    返回 (new_nodes, new_bars, report)：
      * new_bars 已移除被替代的原始投影斜材（keep_originals=False）；
      * report 含候选图记录、解释对、生成/撤除统计（audit 用）。
    """
    # 1. 候选收集（front 面 dxf_geom 证据线）
    cands = collect_diagonal_candidates(
        nodes, bars, sheets=sheets, z_window=z_window, hw_fn=hw_fn)
    # 2. 端点 z 聚类 → 螺旋高度
    heights = cluster_endpoint_heights(cands, tol_mm=cluster_tol_mm)
    # 3. 解释评分（fan/twist 对）+ P1.1 冲突图择优
    interps, sel_audit = build_interpretations(cands, heights, panel_levels, hw_fn)
    # 4. 生成 3D 斜材（去重 + Degree=1 守门）
    gen_raw: List[dict] = []
    for interp in interps:
        gen_raw.extend(generate_topology_bars(interp, hw_fn, level_source_label))

    # 生成前先确定要撤除的原始杆（候选的四面拷贝）：
    # front 面 id 形如 <sheet>__bar_X_front_F → 同源 _B/_L/_R 一并撤除。
    # 关键（2026-08-31 Degree=1 回归修复×2）：同一根图纸线在管线里会裂成
    # 多个 id 变体——跨视图合并的 __splitNN 段、件号拾取的 _NN 消歧后缀
    # （如 bar_1306_front_67 / bar_513_front_53）。候选只覆盖长斜段
    # （≥400mm、倾角 20~70°），其余变体若留下会成为孤儿（两端 Degree=1，
    # 实测 06 段 5 处悬空断裂）。因此按**族键**（剥 __splitNN/_NN 尾缀链
    # + 面后缀）整族撤除，不留残余段。
    import re as _re
    _tail = _re.compile(r"((_\d+)|(__split\d+))+$")

    def _family(bid: str) -> str:
        base = str(bid)
        for suffix in ("_F", "_B", "_L", "_R"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return _tail.sub("", base)

    removed_ids: set = set()
    fams = {_family(c["bar_id"]) for c in cands}
    for b in bars:
        if _family(b.get("id")) in fams:
            removed_ids.add(str(b.get("id")))

    # 节点解析 + 去重
    new_nodes: NodeMap = dict(nodes)
    counter = {"n": 900000}
    seen_geom: List[Tuple[Vec3, Vec3]] = []
    gen_bars: List[dict] = []
    for g in gen_raw:
        a, b = g["from_pos"], g["to_pos"]
        if any(_same_geom((a, b), s) for s in seen_geom):
            continue  # 重复
        seen_geom.append((a, b))
        n1, new_nodes = _find_or_add_node(new_nodes, a, _counter=counter)
        n2, new_nodes = _find_or_add_node(new_nodes, b, _counter=counter)
        if n1 == n2:
            continue
        gen_bars.append({
            "id": f"dtd_{len(gen_bars)}",
            "from": n1,
            "to": n2,
            "role": g["role"],
            "diagonal_topology": True,
            "geometry_class": g["geometry_class"],
            "geometry_origin": g["geometry_origin"],
            "level_source": g["level_source"],
            "source_handles": g["source_handles"],
            "diagonal_pair": g["pair"],
            "diagonal_kind": g["kind"],
            "drawing_view": sheets[0] if sheets else None,
            "source_file": sheets[0] if sheets else None,
        })

    # 5. 重组 bars：撤除原始候选（可选），加入生成杆
    final_bars: List[dict] = []
    if keep_originals:
        final_bars.extend(bars)
    else:
        for b in bars:
            if str(b.get("id")) in removed_ids:
                continue
            final_bars.append(b)
    final_bars.extend(gen_bars)

    report = {
        "sheets": list(sheets),
        "z_window": list(z_window) if z_window else None,
        "n_candidates": len(cands),
        "n_heights": len(heights),
        "heights": [{"z": round(h["z"], 1), "n": h["count"]} for h in heights],
        "interpretations": [
            {"kind": r["kind"], "z_lo": round(r["z_lo"], 1),
             "z_hi": round(r["z_hi"], 1), "score": round(r["score"], 1),
             "n_evidence": r["n"], "evidence": r["evidence"][:4]}
            for r in interps
        ],
        "generated": len(gen_bars),
        "removed_originals": sorted(removed_ids),
        "fan_pairs": sum(1 for r in interps if r["kind"] == "fan"),
        "twist_pairs": sum(1 for r in interps if r["kind"] == "twist"),
        "selection": sel_audit,
        "candidates": [
            {k: c[k] for k in
             ("bar_id", "source_handles", "source_region", "endpoints",
              "length_2d", "inclination_deg", "line_kind")}
            for c in cands
        ],
    }
    return new_nodes, final_bars, report
