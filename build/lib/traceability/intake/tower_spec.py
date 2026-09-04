"""铁塔图纸规范（schema/tower_layer_map.json）的共享读取入口。

解析器与 DXF 生成器都从这里读图层映射、件号正则和视图区域，
保证「自己画的图自己读得回来」。

规范文件位置：<repo>/schema/tower_layer_map.json

P1-5 图层映射可配置化：
    * 所有读取函数都支持 per-project overlay（dict 或 JSON 文件路径）
    * overlay 与基础规范做深合并：列表替换、view_regions 按 stem 合并
    * CLI 传 --layer-map 即可换一套图，不改解析代码
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SPEC: Optional[dict] = None
_SPEC_PATH: Optional[Path] = None


def spec_path() -> Path:
    global _SPEC_PATH
    if _SPEC_PATH is None:
        _SPEC_PATH = Path(__file__).resolve().parents[2] / "schema" / "tower_layer_map.json"
    return _SPEC_PATH


def load_tower_spec(overlay: Optional[str | Path | dict] = None) -> dict:
    """加载铁塔图层/视图规范（不存在则返回空 dict）。

    overlay：可选的项目覆盖配置，可以是 JSON 文件路径或 dict。
    """
    global _SPEC
    if _SPEC is None:
        p = spec_path()
        if p.exists():
            _SPEC = json.loads(p.read_text(encoding="utf-8"))
        else:
            _SPEC = {}
    spec = copy.deepcopy(_SPEC)
    ov = _load_overlay(overlay)
    if ov:
        spec = _deep_merge(spec, ov)
    return spec


def _load_overlay(overlay: Optional[str | Path | dict]) -> Optional[dict]:
    if overlay is None:
        return None
    if isinstance(overlay, dict):
        return copy.deepcopy(overlay)
    p = Path(overlay)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _deep_merge(base: dict, overlay: dict) -> dict:
    """把 overlay 合并进 base：dict 递归合并，列表整体替换。"""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def layer_names(group: str, default: List[str], overlay: Optional[str | Path | dict] = None) -> List[str]:
    """读取某一组图层名（bar_layers / node_layers / dim_layers / text_layers）。"""
    spec = load_tower_spec(overlay)
    val = spec.get(group)
    if isinstance(val, list) and val:
        return [str(v) for v in val]
    return list(default)


def layer_names_for_stem(
    stem: str,
    group: str,
    default: List[str],
    overlay: Optional[str | Path | dict] = None,
) -> List[str]:
    """按文件 stem 读取图层组；overlay 中 `{group}_by_stem` 可覆盖单张图。"""
    spec = load_tower_spec(overlay)
    by_stem = spec.get(f"{group}_by_stem") or {}
    if stem in by_stem and isinstance(by_stem[stem], list) and by_stem[stem]:
        return [str(v) for v in by_stem[stem]]
    return layer_names(group, default, overlay)


def bar_id_patterns(default: List[str], overlay: Optional[str | Path | dict] = None) -> List[str]:
    spec = load_tower_spec(overlay)
    val = spec.get("bar_id_patterns")
    if isinstance(val, list) and val:
        return [str(v) for v in val]
    return list(default)


def view_regions(stem: str, overlay: Optional[str | Path | dict] = None) -> List[dict]:
    """某张图（按文件 stem）的视图区域定义列表。"""
    spec = load_tower_spec(overlay)
    regions = spec.get("view_regions", {}) or {}
    return list(regions.get(stem, []) or [])


def view_region(stem: str, kind: str, overlay: Optional[str | Path | dict] = None) -> Optional[dict]:
    for r in view_regions(stem, overlay):
        if r.get("kind") == kind:
            return r
    # P2 统一视图类型：精确匹配失败后按 canonical_view_type 回退匹配，
    # 使 view_region(stem, "front") 能命中 kind="elevation" 的 region
    # （front/elevation 语义同为「正立面」）。
    target = canonical_view_type(kind)
    if target != kind:
        for r in view_regions(stem, overlay):
            if canonical_view_type(str(r.get("kind") or "")) == target:
                return r
    return None


def view_origin(stem: str, kind: str, default: Optional[Tuple[float, float]] = None,
                overlay: Optional[str | Path | dict] = None) -> Tuple[float, float]:
    """读取视图原点（无规范时回退到调用方提供的默认值）。"""
    r = view_region(stem, kind, overlay)
    if r and "origin" in r:
        ox, oy = r["origin"]
        return (float(ox), float(oy))
    if default is not None:
        return default
    return (0.0, 0.0)


def view_z_level(stem: str, kind: str, overlay: Optional[str | Path | dict] = None) -> Optional[float]:
    r = view_region(stem, kind, overlay)
    if r:
        return r.get("z_level")
    return None


def view_z_offset(stem: str, kind: str, overlay: Optional[str | Path | dict] = None) -> float:
    """视图的 Z 底部标高偏移（真实 mm）。用于多张分段立面图沿 Z 堆叠成整塔。

    每张模块图（如 35A1-JC1-10..29）是塔的一段正立面，各自局部 view_y=0
    对应该段底部。堆叠时把局部 view_y 加上 z_offset 得到全局 Z。
    未声明时返回 0.0（单立面图，无堆叠）。
    """
    r = view_region(stem, kind, overlay)
    if r:
        v = r.get("z_offset")
        if v is not None:
            return float(v)
    return 0.0


def view_z_span_mm(stem: str, kind: str, overlay: Optional[str | Path | dict] = None) -> Optional[float]:
    """视图的标注段高（真实 mm）。分段立面图几何里常含相邻段的接头重叠，
    用 z_span_mm 把该段几何在垂直方向线性归一化到标注高度，消除重叠。
    未声明时返回 None（不缩放，直接用几何跨度）。"""
    r = view_region(stem, kind, overlay)
    if r:
        v = r.get("z_span_mm")
        if v is not None:
            return float(v)
    return None


def view_axes(stem: str, kind: str, overlay: Optional[str | Path | dict] = None) -> List[str]:
    r = view_region(stem, kind, overlay)
    if r:
        return list(r.get("axes", []) or [])
    return []


def view_expand(stem: str, kind: str, overlay: Optional[str | Path | dict] = None) -> float:
    r = view_region(stem, kind, overlay)
    if r:
        for key in ("y_expand", "x_expand"):
            if key in r:
                return float(r[key])
    return 0.0


def cross_file_view_manifest(overlay: Optional[str | Path | dict] = None) -> dict:
    """读取 overlay 中的 cross_file_views 分册清单（front/plan/side 分文件映射）。"""
    spec = load_tower_spec(overlay)
    manifest = spec.get("cross_file_views") or {}
    return dict(manifest) if isinstance(manifest, dict) else {}


# --------------------------------------------------------------------------- #
# Phase A1  sheet_role 枚举 —— 管线永远知道「这张图是什么角色」，不靠塔型硬编码
# --------------------------------------------------------------------------- #

# 固定图纸角色枚举（配置与代码共用；未列出的角色一律不进入空间 3D 合并）。
SHEET_ROLE_ELEVATION = "elevation"
SHEET_ROLE_PLAN = "plan"
SHEET_ROLE_SECTION = "section"
SHEET_ROLE_MODULE_PANEL = "module_panel"
SHEET_ROLE_NODE_DETAIL = "node_detail"
SHEET_ROLE_INDEX = "index"
SHEET_ROLE_TITLE = "title"

SHEET_ROLES: Tuple[str, ...] = (
    SHEET_ROLE_ELEVATION,
    SHEET_ROLE_PLAN,
    SHEET_ROLE_SECTION,
    SHEET_ROLE_MODULE_PANEL,
    SHEET_ROLE_NODE_DETAIL,
    SHEET_ROLE_INDEX,
    SHEET_ROLE_TITLE,
)

# spatial_merge（M3）只接受正交投影视图；front/side 是历史名称，规范化为
# elevation 参与合并。module_panel / node_detail / index / title 永不进 M3。
SPATIAL_MERGE_ROLES: frozenset = frozenset({
    SHEET_ROLE_ELEVATION,
    SHEET_ROLE_PLAN,
    SHEET_ROLE_SECTION,
})

# 视图 region kind / 文件名分流 kind -> 规范 sheet_role。
_SHEET_ROLE_ALIASES: Dict[str, str] = {
    "elevation": SHEET_ROLE_ELEVATION,
    "front": SHEET_ROLE_ELEVATION,
    "side": SHEET_ROLE_ELEVATION,
    "立面": SHEET_ROLE_ELEVATION,
    "正立面": SHEET_ROLE_ELEVATION,
    "侧立面": SHEET_ROLE_ELEVATION,
    "plan": SHEET_ROLE_PLAN,
    "平面": SHEET_ROLE_PLAN,
    "section": SHEET_ROLE_SECTION,
    "剖面": SHEET_ROLE_SECTION,
    "module_panel": SHEET_ROLE_MODULE_PANEL,
    "module": SHEET_ROLE_MODULE_PANEL,
    "panel": SHEET_ROLE_MODULE_PANEL,
    "assembly": SHEET_ROLE_MODULE_PANEL,
    "模块": SHEET_ROLE_MODULE_PANEL,
    "node_detail": SHEET_ROLE_NODE_DETAIL,
    "detail": SHEET_ROLE_NODE_DETAIL,
    "大样": SHEET_ROLE_NODE_DETAIL,
    "详图": SHEET_ROLE_NODE_DETAIL,
    "index": SHEET_ROLE_INDEX,
    "toc": SHEET_ROLE_INDEX,
    "catalog": SHEET_ROLE_INDEX,
    "contents": SHEET_ROLE_INDEX,
    "目录": SHEET_ROLE_INDEX,
    "bom": SHEET_ROLE_INDEX,
    "材料表": SHEET_ROLE_INDEX,
    "title": SHEET_ROLE_TITLE,
    "title_block": SHEET_ROLE_TITLE,
    "cover": SHEET_ROLE_TITLE,
    "图签": SHEET_ROLE_TITLE,
}


def canonical_sheet_role(kind: str) -> str:
    """把任意图纸/视图 kind 规范化为 sheet_role 枚举值。

    未知 kind 原样返回（不猜测），但一定不属于 SPATIAL_MERGE_ROLES，
    因此不会进入 M3 空间合并。
    """
    if not isinstance(kind, str):
        return ""
    return _SHEET_ROLE_ALIASES.get(kind.strip().lower(), kind.strip().lower())


# 视图类型归一化（区别于 sheet_role 聚合）：front/elevation 都指「正立面」，
# 统一为 front；side/section 保留区分（section 是剖面，side 是侧立面）。
# 供 merge_view_coordinates 等按 view_type 分桶的地方使用，避免 elevation
# 来源节点被漏进 nodes_by_view["elevation"] 而查不到 nodes_by_view["front"]。
_VIEW_TYPE_ALIASES: Dict[str, str] = {
    "front": "front",
    "elevation": "front",
    "立面": "front",
    "正立面": "front",
    "side": "side",
    "侧立面": "side",
    "section": "section",
    "剖面": "section",
    "plan": "plan",
    "平面": "plan",
    "detail": "detail",
    "大样": "detail",
    "详图": "detail",
}

# 正交投影视图类型（可参与空间解算/合并）。
ORTHO_VIEW_TYPES: frozenset = frozenset({"front", "side", "section", "plan"})


def canonical_view_type(view_type: str) -> str:
    """把任意视图类型规范化为 front/side/section/plan/detail 之一。

    与 canonical_sheet_role 的区别：这里保留 front/side/section 的区分
    （它们在三视图解算里语义不同），只把 elevation→front 等历史别名归一化。
    未知类型原样返回（不猜测），调用方自行判断是否属于 ORTHO_VIEW_TYPES。
    """
    if not isinstance(view_type, str):
        return ""
    return _VIEW_TYPE_ALIASES.get(view_type.strip().lower(), view_type.strip().lower())


def is_ortho_view_type(view_type: str) -> bool:
    """该视图类型是否为可参与空间解算的正交投影视图。"""
    return canonical_view_type(view_type) in ORTHO_VIEW_TYPES


def is_spatial_merge_role(role: str) -> bool:
    """该 sheet_role 是否允许进入 spatial_merge（M3）。"""
    return canonical_sheet_role(role) in SPATIAL_MERGE_ROLES


def _region_kinds_for_stem(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
    *,
    require_axes: bool,
) -> List[str]:
    """某 stem 在 overlay view_regions 中声明的 view kind 列表。

    require_axes=True 时只统计带 axes 的视图（真正可解析正交投影的视图）。
    """
    out: List[str] = []
    for r in view_regions(stem, overlay=overlay):
        if require_axes and not list(r.get("axes") or []):
            continue
        kind = str(r.get("kind", "drawing"))
        if kind:
            out.append(kind)
    return out


def sheet_role_for_stem(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
) -> Optional[str]:
    """按 overlay 声明的视图角色推导 sheet_role（无声明返回 None，由调用方兜底）。

    sheet 角色 = 该图所有带 axes 视图角色的并集。若只有无 axes 视图
    （detail / 模块页），返回对应的非空间角色（node_detail / module_panel）。
    """
    kinds = _region_kinds_for_stem(stem, overlay=overlay, require_axes=True)
    roles = {canonical_sheet_role(k) for k in kinds}
    if roles:
        # 空间角色的优先级：elevation/plan/section 任一出现即按主视图角色；
        # 混合了模块页时以「有 axes 的视图」为准，因为空间合并只看 axes 视图。
        spatial = roles & set(SPATIAL_MERGE_ROLES)
        if spatial:
            return next(iter(sorted(spatial)))
        return next(iter(sorted(roles)))
    # 无 axes 视图：detail / module_panel / index / title
    kinds_all = _region_kinds_for_stem(stem, overlay=overlay, require_axes=False)
    for k in kinds_all:
        role = canonical_sheet_role(k)
        if role in (
            SHEET_ROLE_MODULE_PANEL,
            SHEET_ROLE_NODE_DETAIL,
            SHEET_ROLE_INDEX,
            SHEET_ROLE_TITLE,
        ):
            return role
    return None


def sheet_is_spatial_mergeable(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
) -> bool:
    """Phase A2 硬边界：一张图只有 overlay 声明了带 axes 的正交视图
    （elevation/plan/section）才允许进入 spatial_merge。

    无 overlay 声明、只有 detail / module_panel / index / title 视图、
    或视图无 axes 的图纸一律返回 False。
    """
    kinds = _region_kinds_for_stem(stem, overlay=overlay, require_axes=True)
    if not kinds:
        return False
    return any(is_spatial_merge_role(k) for k in kinds)


def sheet_roles_report(overlay: Optional[str | Path | dict] = None) -> Dict[str, Any]:
    """Phase F1 配置校验：报告每张已声明 stem 的角色与空间合并资格。"""
    spec = load_tower_spec(overlay)
    regions_map = spec.get("view_regions") or {}
    out: Dict[str, Any] = {"sheets": {}, "warnings": []}
    if not isinstance(regions_map, dict):
        return out
    for stem, regions in regions_map.items():
        roles = [canonical_sheet_role(r.get("kind", "drawing")) for r in (regions or [])]
        mergeable = sheet_is_spatial_mergeable(str(stem), overlay=overlay)
        out["sheets"][str(stem)] = {
            "roles": sorted({r for r in roles if r}),
            "spatial_mergeable": mergeable,
        }
        unknown = sorted({r for r in roles if r and r not in SHEET_ROLES})
        if unknown:
            out["warnings"].append({
                "stem": str(stem),
                "unknown_roles": unknown,
                "message": "sheet_role 不在固定枚举内，将不进入 spatial_merge",
            })
        # A2：声明了空间角色但视图无 axes -> 配置错误
        declared = [r for r in (regions or []) if r.get("kind") in ("front", "side", "elevation", "plan", "section")]
        no_axes = [r for r in declared if not list(r.get("axes") or [])]
        if no_axes:
            out["warnings"].append({
                "stem": str(stem),
                "declared_spatial_without_axes": [r.get("kind") for r in no_axes],
                "message": "声明为空间视图但 axes=[]，不会进入 spatial_merge",
            })
    return out


def parseable_view_kinds_by_stem(overlay: Optional[str | Path | dict] = None) -> Dict[str, set]:
    """各 stem 在 overlay 中声明的可解析视图 kind 集合（须带 axes）。

    Phase A2：只返回空间可合并角色（elevation/plan/section 及历史 front/side
    的规范化值），detail / module_panel 等即使被提升解析也不在此列。
    """
    spec = load_tower_spec(overlay)
    regions_map = spec.get("view_regions") or {}
    out: Dict[str, set] = {}
    if not isinstance(regions_map, dict):
        return out
    for stem, regions in regions_map.items():
        kinds: set = set()
        for r in regions or []:
            if not r.get("axes"):
                continue
            kind = str(r.get("kind", "drawing"))
            if is_spatial_merge_role(kind):
                kinds.add(canonical_sheet_role(kind))
        if kinds:
            out[str(stem)] = kinds
    return out


def should_use_cross_file_merge(overlay: Optional[str | Path | dict] = None) -> bool:
    """overlay 是否描述「分文件多视图」且应走 merge_cross_file_views 而非 ID 前缀假合并。"""
    manifest = cross_file_view_manifest(overlay)
    sheets = manifest.get("sheets") or {}
    if isinstance(sheets, dict) and len(sheets) >= 2:
        roles = {str(v) for v in sheets.values() if v}
        if len(roles) >= 2:
            return True
    kinds_by_stem = parseable_view_kinds_by_stem(overlay)
    if len(kinds_by_stem) < 2:
        return False
    all_kinds = set().union(*kinds_by_stem.values())
    merge_sets = (
        {"front", "plan"},
        {"front", "side"},
        {"front", "side", "section"},
        {"elevation", "plan"},
    )
    return any(ms <= all_kinds for ms in merge_sets)


def cross_file_z_ref(overlay: Optional[str | Path | dict] = None) -> Optional[float]:
    """front+plan 配对时 front 节点应对齐的 Z 参考（来自 cross_file_views.view_align）。"""
    manifest = cross_file_view_manifest(overlay)
    align = manifest.get("view_align") or {}
    if not isinstance(align, dict):
        return None
    for key, meta in align.items():
        if not isinstance(meta, dict):
            continue
        if "z_ref" in meta:
            return float(meta["z_ref"])
        if str(key).endswith(":front") and "z_level" in meta:
            return float(meta["z_level"])
    return None


def cross_file_allow_z_peer_interpolate(overlay: Optional[str | Path | dict] = None) -> bool:
    manifest = cross_file_view_manifest(overlay)
    return bool(manifest.get("allow_z_peer_y_interpolate"))


def cross_file_normalize_x(overlay: Optional[str | Path | dict] = None) -> bool:
    """cross_file 合并后是否把主立面 X 中心归零（view_align.*.normalize_x）。"""
    manifest = cross_file_view_manifest(overlay)
    align = manifest.get("view_align") or {}
    if isinstance(align, dict):
        for meta in align.values():
            if isinstance(meta, dict) and meta.get("normalize_x"):
                return True
    return False


def cross_file_infer_side_stems(overlay: Optional[str | Path | dict] = None) -> List[str]:
    manifest = cross_file_view_manifest(overlay)
    stems = manifest.get("infer_side_on_stems") or []
    return [str(s) for s in stems] if isinstance(stems, list) else []


def cross_file_parse_all_project_sheets(overlay: Optional[str | Path | dict] = None) -> bool:
    """全册追溯解析：详图/模块页也解析杆件，写入各 sheet JSON（供 M1 index / BOM / 追溯）。

    仅此一个开关。它与 spatial_merge（M3）正交：开启后 detail/模块页会按
    正立面比例解析杆件，但绝不因此进入 cross_file_merge_stems（A2 硬边界）。
    """
    return bool(cross_file_view_manifest(overlay).get("parse_all_project_sheets"))


def default_front_region_scales(overlay: Optional[str | Path | dict] = None) -> Tuple[float, float]:
    """全册合并时详图页继承主立面 scale_x/scale_y。"""
    manifest = cross_file_view_manifest(overlay)
    sheets = manifest.get("sheets") or {}
    front_stem = sheets.get("front") if isinstance(sheets, dict) else None
    if front_stem:
        for region in view_regions(str(front_stem), overlay=overlay):
            if region.get("kind") == "front" and region.get("axes"):
                return region_scale_xy(region)
    return 50.2, 85.1


def elevate_regions_for_full_merge(
    regions: List[dict],
    overlay: Optional[str | Path | dict] = None,
) -> List[dict]:
    """全册跑批：把 detail 区（axes 为空）提升为 front 立面，产出杆件供追溯/BOM。

    注意（Phase A2）：提升只用于「逐 sheet 解析杆件」（M1 全册 index / 追溯），
    不改变该图在 spatial_merge（M3）中的资格。提升后的 region 会带
    ``promoted_from`` 与 ``spatial_merge=False`` 标记，任何 M3 消费方都应
    用 sheet_is_spatial_mergeable() 按「原始 overlay 声明」判断，而不是
    看提升后的 region。
    """
    if not cross_file_parse_all_project_sheets(overlay) or not regions:
        return regions
    sx, sy = default_front_region_scales(overlay)
    out: List[dict] = []
    for region in regions:
        rc = copy.deepcopy(region)
        if not list(rc.get("axes") or []):
            rc["kind"] = "front"
            rc["axes"] = ["x", "z"]
            rc.setdefault("scale_x", sx)
            rc.setdefault("scale_y", sy)
            rc.setdefault("z_flip", True)
            rc["promoted_from"] = canonical_sheet_role(
                str(region.get("kind", "detail"))
            )
            rc["spatial_merge"] = False
        out.append(rc)
    return out


def cross_file_merge_stems(overlay: Optional[str | Path | dict] = None) -> set:
    """cross_file 真 3D 合并应纳入的图纸 stem（不含图册内其它详图/模块页）。

    来源：cross_file_views.sheets 中 front/plan/side/elevation 非空 stem
    + infer_side_on_stems + merge_stems_extra。

    Phase A2 强制边界（对所有项目生效，不按塔型）：
        * 候选 stem 必须 overlay 声明了带 axes 的正交视图
          （elevation/plan/section；front/side 为历史 elevation 名称）；
        * module_panel / node_detail / index / title 即使出现在
          merge_stems_extra 或 infer_side_on_stems 里，也会被剔除；
        * 只有无 axes 视图（大样/模块页）的图纸永远不能进 spatial_merge。

    未配置 manifest 时返回空集，由调用方决定是否合并全部模型（旧路径兼容）。
    """
    manifest = cross_file_view_manifest(overlay)
    sheets = manifest.get("sheets") or {}
    merge_roles = ("front", "plan", "side", "elevation")
    stems: set = set()
    if isinstance(sheets, dict):
        for role in merge_roles:
            v = sheets.get(role)
            if v and str(v).strip():
                stems.add(str(v).strip())
    for s in cross_file_infer_side_stems(overlay):
        stems.add(str(s))
    extra = manifest.get("merge_stems_extra") or []
    if isinstance(extra, list):
        for s in extra:
            if s and str(s).strip():
                stems.add(str(s).strip())

    # A2 硬边界：逐 stem 过滤，只有 overlay 声明的空间可合并角色才保留。
    if manifest:
        stems = {
            s for s in stems
            if sheet_is_spatial_mergeable(s, overlay=overlay)
        }
    return stems


def min_bar_length_mm(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
    default: float = 0.0,
) -> float:
    """杆件最短长度（真实 mm）；短于该值的线段不进入 tower_bar。"""
    spec = load_tower_spec(overlay)
    by_stem = spec.get("min_bar_length_mm_by_stem") or {}
    if stem in by_stem:
        return float(by_stem[stem])
    return float(spec.get("min_bar_length_mm", default))


def cluster_eps_mm(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
    default: float = 50.0,
) -> float:
    """端点聚类阈值（真实 mm）；解析时按视图 scale_ratio 换算回图纸单位。"""
    spec = load_tower_spec(overlay)
    by_stem = spec.get("cluster_eps_mm_by_stem") or {}
    if stem in by_stem:
        return float(by_stem[stem])
    return float(spec.get("cluster_eps_mm", default))


def dimension_beat_anchor_config(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
) -> Optional[dict]:
    """P2.1 DIMENSION 节拍锚定配置（坐标链证据标定）。

    overlay 形如：
        "dimension_beat_anchor": {
            "35A1-JC1-06": {"z_base_mm": 12000, "enabled": true}
        }
    返回该 stem 的 dict（含 z_base_mm / beat_min_mm / beat_max_mm /
    enabled），未声明返回 None（该册走分位数归一化旧行为）。
    """
    spec = load_tower_spec(overlay)
    by_stem = spec.get("dimension_beat_anchor") or {}
    if not isinstance(by_stem, dict):
        return None
    cfg = by_stem.get(stem)
    if not isinstance(cfg, dict) or not cfg or not cfg.get("enabled", True):
        return None
    return dict(cfg)


def region_scale_ratio(region: Optional[dict]) -> float:
    """视图区域比例：scale_ratio=10 表示图纸 1:10（真实尺寸 = 图面 × 10）。

    国网立面图横向/竖向比例不同（scale_x ≠ scale_y），无单一 scale_ratio 时
    回退到 scale_x/scale_y 的几何平均作为各向同性代理（供 min_bar_len / eps
    换算用；精确分轴比例见 region_scale_xy）。
    """
    if not region:
        return 1.0
    if "scale_ratio" in region:
        return float(region["scale_ratio"])
    sx = region.get("scale_x")
    sy = region.get("scale_y")
    if sx is not None and sy is not None:
        return (float(sx) * float(sy)) ** 0.5
    return 1.0


def region_scale_xy(region: Optional[dict]) -> Tuple[float, float]:
    """视图区域 x/y 分轴比例（P1 坐标对齐）。

    真实国网立面图横向（塔宽）与竖向（塔高）常是不同比例（如 02 图
    宽高比 1.18 vs GT 0.151），单一 scale_ratio 会差 8 倍。支持 overlay
    显式 scale_x / scale_y；未给时回退 scale_ratio（两轴同值，兼容旧图）。
    """
    if not region:
        return 1.0, 1.0
    sx = region.get("scale_x")
    sy = region.get("scale_y")
    if sx is not None and sy is not None:
        return float(sx), float(sy)
    s = region_scale_ratio(region)
    return s, s


def double_line_merge_config(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
) -> Optional[dict]:
    """按 stem 读取双线中心线合并配置（角钢两肢各画一条平行线的国网图）。

    overlay 中可配置：
        double_line_merge: {
            "<stem>": {
                "max_offset_units": 3.0,       # 两线中点最大距离（图纸单位）
                "max_length_diff_ratio": 0.25, # 两线长度差上限（相对较长线）
                "max_angle_rad": 0.25,         # 方向角差上限（含 pi 翻转）
                "min_length_units": 3.0,       # 参与配对的线段最短长度（图纸单位）
            }
        }
    返回 None 表示该 stem 不启用双线合并。
    """
    spec = load_tower_spec(overlay)
    cfg = spec.get("double_line_merge") or {}
    if not isinstance(cfg, dict):
        return None
    v = cfg.get(stem)
    return dict(v) if isinstance(v, dict) and v else None


def exact_overlap_dedup_tolerance(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
) -> Optional[float]:
    """按 stem 读取精确重合线去重容差（P3.3，LINE+LWPOLYLINE 复制线）。

    overlay 中可配置：
        exact_overlap_dedup: {"<stem>": {"tolerance_units": 0.5}}

    两端点距离之和 < 2*tolerance_units 的线对视为同一图元的重复绘制，
    保留先出现者。返回 None 表示该 stem 不启用。
    """
    spec = load_tower_spec(overlay)
    cfg = spec.get("exact_overlap_dedup") or {}
    if not isinstance(cfg, dict):
        return None
    v = cfg.get(stem)
    if isinstance(v, dict) and v:
        try:
            return float(v.get("tolerance_units", 0.5))
        except (TypeError, ValueError):
            return None
    return None


def collinear_merge_config(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
) -> Optional[dict]:
    """按 stem 读取共线碎段合并配置（国网图把一根角钢画成多个短碎段）。

    overlay 中可配置：
        collinear_merge: {
            "<stem>": {
                "colinear_tol": 2.0,   # 点到主轴直线的垂直距离上限（图纸单位）
                "gap_tol": 30.0,       # 端点沿主轴投影间距上限（图纸单位）
                "max_angle_deg": 8.0,  # 方向角差上限（含 pi 翻转）
            }
        }
    返回 None 表示该 stem 不启用共线合并（保持旧行为）。
    """
    spec = load_tower_spec(overlay)
    cfg = spec.get("collinear_merge") or {}
    if not isinstance(cfg, dict):
        return None
    v = cfg.get(stem)
    return dict(v) if isinstance(v, dict) and v else None


def cross_file_synthetic_side_from_front(overlay: Optional[str | Path | dict] = None) -> bool:
    manifest = cross_file_view_manifest(overlay)
    return bool(manifest.get("synthetic_side_from_front"))


def cross_file_synthetic_side_view_x_scale(overlay: Optional[str | Path | dict] = None) -> float:
    """synthetic side 的 side.view_x = front.view_x * scale。

    默认 1.0（M5 原行为：假侧视与正立面 1:1，会得到 y≈x 的 45° 斜片）。
    国网单立面可设 0.0（假设侧视投影中心在 0），解得 y ≈ -a*x，得到
    关于 X=0 对称、深度适中的 2.5D 体，避免 y=x 剪切。
    """
    manifest = cross_file_view_manifest(overlay)
    val = manifest.get("synthetic_side_view_x_scale")
    return float(val) if val is not None else 1.0


def cross_file_plan_sheets(overlay: Optional[str | Path | dict] = None) -> List[Dict[str, Any]]:
    """多 plan 分册：cross_file_views.plan_sheets 或 sheets.plan + view_align z_level。"""
    manifest = cross_file_view_manifest(overlay)
    raw = manifest.get("plan_sheets")
    if isinstance(raw, list) and raw:
        out: List[Dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict) and item.get("stem"):
                out.append({
                    "stem": str(item["stem"]),
                    "z_level": float(item["z_level"]) if item.get("z_level") is not None else 0.0,
                })
        return out
    sheets = manifest.get("sheets") or {}
    align = manifest.get("view_align") or {}
    plan_stem = sheets.get("plan") if isinstance(sheets, dict) else None
    if not plan_stem:
        return []
    z_level = 0.0
    for key, meta in align.items():
        if isinstance(meta, dict) and str(key).endswith(":plan") and "z_level" in meta:
            z_level = float(meta["z_level"])
            break
    return [{"stem": str(plan_stem), "z_level": z_level}]


def cross_file_z_band_scale(overlay: Optional[str | Path | dict] = None) -> float:
    manifest = cross_file_view_manifest(overlay)
    val = manifest.get("z_band_scale")
    return float(val) if val is not None else 4.0


def assembly_split_min_gap_ratio(overlay: Optional[str | Path | dict] = None) -> float:
    manifest = cross_file_view_manifest(overlay)
    val = manifest.get("assembly_split_min_gap_ratio")
    return float(val) if val is not None else 0.5


def resolve_geom_method_for_sheet(
    stem: str,
    overlay: Optional[str | Path | dict] = None,
    *,
    mergeable: bool = True,
    default: str = "auto",
) -> str:
    """按 overlay 解析单张 sheet 的 A2 geom_method。

    P2.4：``mllm_keep_drop_sheets`` 内的空间分册走 centerline（DXF 候选 +
    MLLM keep/drop，坐标仍来自 DXF）。``geom_method_by_stem`` 可显式覆盖。
    非空间段（mergeable=False）固定 ezdxf。
    """
    if not mergeable:
        return "ezdxf"
    spec = load_tower_spec(overlay)
    by_stem = spec.get("geom_method_by_stem") or {}
    if isinstance(by_stem, dict) and stem in by_stem:
        return str(by_stem[stem])
    keep_drop = spec.get("mllm_keep_drop_sheets") or []
    if stem in keep_drop:
        return "centerline"
    return default
