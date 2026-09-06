"""Phase 2c 意图路由：把 sheet_intent 四分类接入管线（overlay 缺省时生效）。

目标（goal Phase 2 措辞）：「MLLM 四分类 + 置信度，分类驱动管线选择」，
验收 = JC1/ZC1 无 per-stem 手工 overlay 意图声明端到端跑通且指标不回退。

接线原则（三条，顺序不可变）：
    1. **overlay 声明优先**：stem 在 overlay view_regions 里有带 kind/axes
       的 region 声明时，意图路由完全不干预——JC1/ZC1 现行 overlay 路径
       逐字节不变，红线零风险；
    2. **只补意图**：stem 的声明缺 kind/axes（Phase 2e 剥离实验产物）时，
       意图分类补挂 kind/axes，**几何全部继承声明原值**（origin/region/
       scale_x/scale_y/z_offset/z_span_mm/z_axis_up——这些是标定与塔级
       路由，不是意图；国网版式多视图按位置定序：首区=front、次区=side，
       第三个及以后保守按 detail 不产杆件）。无任何声明的 stem（第三
       梯队通用化）走聚类合成：主塔形簇 bbox 为 front，满足孪生判据
       （同高±10% + 横向分离）的次簇为 side，无 scale（DIM 标定补）；
    3. **不产 z/比例**：合成的 region 不含任何 z_offset/z_span_mm——
       塔级路由（cross_file_views.sheets + z 堆叠）保持 overlay/人工通道
       （图纸内 z 歧义，ZC1 07:[5482,11292] vs 12:[10500,18814] 带重叠
       实测不可自证）。

单点接线：tower_spec.view_regions() 在 overlay 未有效声明该 stem 时查
本模块的注册表。由此 sheet_is_spatial_mergeable / sheet_role_for_stem /
cross_file_merge_stems / resolve_drawing_kind / extract_tower_from_dxf
（regions 直接来自 view_regions，B6 兜底自动失效）全部变为意图驱动，
无需逐个改函数。

注册入口：intake_tower_batch / build_project_from_directory /
_build_hybrid_project 在批量开始前调用 register_sheet_intents()（一次性
对全册 DXF 跑 classify_batch_intents——batch 参照（缩微门分母）需要整册
上下文，per-stem 调用没有意义）。

MLLM 不可用时确定性几何判据兜底（classify_batch_intents 内建）；
特征/verdict 两级缓存均落 out/sheet_intent/，脱网可复跑。

铁律对齐：MLLM 只做图纸意图分类；本模块输出的 view_regions 不含任何
3D 几何起止点坐标——坐标仍由 ezdxf 提取 + 求解器产出。
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .sheet_intent import (
    INTENT_ASSEMBLY_FRONT,
    INTENT_ASSEMBLY_SIDE,
    INTENT_FABRICATION_DETAIL,
    INTENT_PLAN_PROJECTION,
    SheetIntent,
    classify_batch_intents,
)

# overlay 身份（None / path / dict）→ {stem: [region, ...]} 注册表。
# 进程级缓存：同一 overlay 的一次管线只分类一次（多入口注册幂等）。
_REGISTRATIONS: Dict[Any, Dict[str, List[dict]]] = {}
_REGISTRATIONS_REPORT: Dict[Any, Dict[str, Any]] = {}
_LOCK = threading.Lock()

# 意图 → (主 region kind, axes)。front/side 同为 elevation（管线历史枚举
# front/side，canonical_sheet_role 规范化为 sheet_role=elevation）。
_INTENT_AXES = {
    INTENT_ASSEMBLY_FRONT: ["x", "z"],
    INTENT_ASSEMBLY_SIDE: ["x", "z"],
    INTENT_FABRICATION_DETAIL: [],
    INTENT_PLAN_PROJECTION: ["x", "y"],
}

# 孪生立面判定（仅用于「无声明」stem 的聚类合成）：
#   两显著塔形簇高度差 ≤10%（真并排立面画同一塔，基准一致：ZC1 05/08/
#   09/10 与 JC1-02 实测高度差 0~0.4%；JC1-07 塔段+大样差 26% 被拒），
#   x 区间不重叠 + 次簇线数 ≥25% 主簇 + 次簇跨度 ≥40% 主簇。
_PAIR_HEIGHT_TOL = 0.10
_PAIR_MIN_N_RATIO = 0.25
_PAIR_MIN_SPAN_RATIO = 0.40
# 聚类合成的显著塔形簇门槛（相对最大簇线数），与 sheet_intent 的
# _CROP_COMPONENT_RATIO 语义一致：只圈「塔」本身，大样/标注小簇不进。
_REGION_COMPONENT_RATIO = 0.3
_MIN_CLUSTER_LINES = 8


def _overlay_key(overlay: Optional[Union[str, Path, dict]]) -> Any:
    """注册表键：path 用 resolved str，dict 用 id()（进程内同 dict 复用）。"""
    if overlay is None:
        return None
    if isinstance(overlay, dict):
        return id(overlay)
    return str(Path(str(overlay)).resolve())


def _declared_regions(
    stem: str,
    overlay: Optional[Union[str, Path, dict]],
) -> Tuple[List[dict], bool]:
    """overlay 原始声明 regions + 是否含有效意图字段（kind/axes）。

    含义：effective=True 表示 overlay 说了算（本模块不干预）；
    effective=False 表示声明存在但缺意图（Phase 2e 剥离产物）——
    声明作为几何载体，意图由分类补挂。
    """
    from .tower_spec import load_tower_spec

    spec = load_tower_spec(overlay)
    regions_map = spec.get("view_regions") or {}
    declared = [r for r in (regions_map.get(stem) or [])
                if isinstance(r, dict)]
    effective = any((r.get("kind") or r.get("axes")) for r in declared)
    return declared, effective


def _region_from_declared(base: dict, kind: str, axes: List[str]) -> dict:
    """声明 region 副本 + 意图补挂的 kind/axes（几何字段全保留）。"""
    r = copy.deepcopy(base)
    r["kind"] = kind
    r["axes"] = list(axes)
    r["intent_source"] = "sheet_intent"
    return r


def _synth_from_declared(
    declared: List[dict],
    intent: SheetIntent,
) -> List[dict]:
    """声明存在但缺 kind/axes：按意图 + 版式位置补挂（几何不动）。

    国网版式（正立面主视图在左，侧立面并排在右）：
        * elevation 意图：首区 front、次区 side，第 3+ 区保守 detail
          （不产杆件，防未知视图混入立面）；
        * plan 意图：首区 plan，其余 detail；
        * detail 意图：全部 detail（空 axes，不产杆件）。
    """
    if intent.intent in (INTENT_ASSEMBLY_FRONT, INTENT_ASSEMBLY_SIDE):
        out = []
        for i, base in enumerate(declared):
            if i == 0:
                out.append(_region_from_declared(base, "front", ["x", "z"]))
            elif i == 1:
                out.append(_region_from_declared(base, "side", ["x", "z"]))
            else:
                out.append(_region_from_declared(base, "detail", []))
        return out
    if intent.intent == INTENT_PLAN_PROJECTION:
        return [
            _region_from_declared(
                base, "plan" if i == 0 else "detail",
                ["x", "y"] if i == 0 else [])
            for i, base in enumerate(declared)
        ]
    return [
        _region_from_declared(base, "detail", []) for base in declared
    ]


def _region_from_component(comp: Dict[str, Any], kind: str, axes: List[str],
                           conf: float) -> dict:
    """聚类 bbox 合成 region（无声明 stem 的通用化路径；无 scale/z——
    scale 由 DIM 比例标定补，z 由塔级路由补）。"""
    x0, x1, y0, y1 = comp["bbox"]
    return {
        "kind": kind,
        "title": f"{kind}（sheet_intent 聚类合成，conf={conf:.2f}）",
        "origin": [x0, y0],
        "region": [x0, x1, y0, y1],
        "axes": list(axes),
        "z_level": None,
        "intent_source": "sheet_intent_cluster",
    }


def _synth_from_clusters(intent: SheetIntent) -> List[dict]:
    """无声明 stem：聚类特征合成 regions（第三梯队通用化路径）。

    * detail/plan：主簇 bbox 单 region；
    * front/side：主塔形簇为 front；若存在孪生簇（同高+横向分离），
      右簇补 side（国网版式正立面在左）。
    """
    comps = [c for c in ((intent.features or {}).get("components") or [])
             if c.get("bbox")]
    if not comps:
        return []
    conf = intent.confidence

    if intent.intent not in (INTENT_ASSEMBLY_FRONT, INTENT_ASSEMBLY_SIDE):
        kind = ("plan" if intent.intent == INTENT_PLAN_PROJECTION
                else "detail")
        axes = ["x", "y"] if kind == "plan" else []
        return [_region_from_component(comps[0], kind, axes, conf)]

    top_n = float(comps[0].get("n") or 0)
    threshold = max(float(_MIN_CLUSTER_LINES),
                    _REGION_COMPONENT_RATIO * top_n)
    significant = [c for c in comps if float(c.get("n") or 0) >= threshold]
    main = significant[0] if significant else comps[0]

    regions = [_region_from_component(main, "front", ["x", "z"], conf)]
    if intent.intent == INTENT_ASSEMBLY_SIDE and len(significant) >= 2:
        a = main
        h_a = float(a.get("h") or 0)
        span_a = max(float(a.get("w") or 0), h_a)
        for b in significant[1:]:
            n_ratio = (float(b.get("n") or 0)
                       / max(float(a.get("n") or 1), 1.0))
            h_b = float(b.get("h") or 0)
            span_b = max(float(b.get("w") or 0), h_b)
            if h_a <= 0 or abs(h_b - h_a) > _PAIR_HEIGHT_TOL * max(h_a, h_b):
                continue  # 孪生要求同高（同塔同基准并排画法）
            ax0, ax1 = float(a["bbox"][0]), float(a["bbox"][1])
            bx0, bx1 = float(b["bbox"][0]), float(b["bbox"][1])
            if not (bx0 >= ax1 or ax0 >= bx1):
                continue  # x 区间须不重叠（左右并排）
            if (n_ratio >= _PAIR_MIN_N_RATIO
                    and span_b >= _PAIR_MIN_SPAN_RATIO * max(span_a, 1e-6)):
                regions.append(_region_from_component(
                    b, "side", ["x", "z"], conf))
                break
    return regions


def register_sheet_intents(
    dxf_paths: List[Union[str, Path]],
    overlay: Optional[Union[str, Path, dict]] = None,
    *,
    use_mllm: bool = True,
    cache_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, SheetIntent]:
    """批量注册意图合成的 view_regions（管线入口调用一次）。

    幂等：同 overlay 已注册（且 DXF 列表一致）时直接返回既有分类。
    返回 {stem: SheetIntent}（供调用方写 run_manifest 留痕）。
    """
    dxf_paths = [Path(p) for p in dxf_paths]
    if not dxf_paths:
        return {}
    key = _overlay_key(overlay)
    sig = tuple(sorted(str(p) for p in dxf_paths))
    with _LOCK:
        rep = _REGISTRATIONS_REPORT.get(key)
        if rep is not None and tuple(rep.get("signature") or ()) == sig:
            return rep["intents"]

    intents = classify_batch_intents(
        dxf_paths, use_mllm=use_mllm, cache_dir=cache_dir)

    synthesized: Dict[str, List[dict]] = {}
    for stem, si in intents.items():
        declared, effective = _declared_regions(stem, overlay)
        if effective:
            continue  # overlay 显式声明优先，意图不干预
        if declared:
            regions = _synth_from_declared(declared, si)
        else:
            regions = _synth_from_clusters(si)
        if regions:
            synthesized[stem] = regions

    with _LOCK:
        _REGISTRATIONS[key] = synthesized
        _REGISTRATIONS_REPORT[key] = {
            "signature": sig,
            "intents": intents,
        }
    return intents


def intent_regions_for_stem(
    stem: str,
    overlay: Optional[Union[str, Path, dict]] = None,
) -> List[dict]:
    """tower_spec.view_regions 的意图补充（单点接线，仅供其调用）。

    返回空列表时 view_regions() 维持原 overlay 语义（含 B6 兜底）。
    """
    key = _overlay_key(overlay)
    with _LOCK:
        regs = _REGISTRATIONS.get(key)
    if not regs:
        return []
    out = regs.get(stem)
    return list(out) if out else []


def registration_report(
    overlay: Optional[Union[str, Path, dict]] = None,
) -> Dict[str, Any]:
    """审计留痕：该 overlay 的意图注册概要（写 run_manifest 用）。"""
    key = _overlay_key(overlay)
    with _LOCK:
        rep = _REGISTRATIONS_REPORT.get(key)
    if rep is None:
        return {"registered": False}
    intents = rep["intents"]
    return {
        "registered": True,
        "n_sheets": len(intents),
        "elevation_stems": sorted(
            s for s, si in intents.items()
            if si.intent in (INTENT_ASSEMBLY_FRONT, INTENT_ASSEMBLY_SIDE)),
        "plan_stems": sorted(
            s for s, si in intents.items()
            if si.intent == INTENT_PLAN_PROJECTION),
        "detail_stems": sorted(
            s for s, si in intents.items()
            if si.intent == INTENT_FABRICATION_DETAIL),
        "intents": {s: si.to_dict() for s, si in intents.items()},
    }


def clear_registrations() -> None:
    """清空注册表（测试用；生产路径一次管线注册一次）。"""
    with _LOCK:
        _REGISTRATIONS.clear()
        _REGISTRATIONS_REPORT.clear()
