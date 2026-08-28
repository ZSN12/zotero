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


def parseable_view_kinds_by_stem(overlay: Optional[str | Path | dict] = None) -> Dict[str, set]:
    """各 stem 在 overlay 中声明的可解析视图 kind 集合（须带 axes）。"""
    spec = load_tower_spec(overlay)
    regions_map = spec.get("view_regions") or {}
    out: Dict[str, set] = {}
    if not isinstance(regions_map, dict):
        return out
    for stem, regions in regions_map.items():
        kinds: set = set()
        for r in regions or []:
            if r.get("axes"):
                kinds.add(str(r.get("kind", "drawing")))
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


def cross_file_merge_stems(overlay: Optional[str | Path | dict] = None) -> set:
    """cross_file 真 3D 合并应纳入的图纸 stem（不含图册内其它详图/模块页）。

    来源：cross_file_views.sheets 中 front/plan/side/elevation 非空 stem
    + infer_side_on_stems。detail / bom 等角色不参与空间合并。
    未配置 manifest 时返回空集，由调用方决定是否合并全部模型。
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
        stems.add(s)
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
