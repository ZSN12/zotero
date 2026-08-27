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
from typing import Dict, List, Optional, Tuple

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


def cross_file_infer_side_stems(overlay: Optional[str | Path | dict] = None) -> List[str]:
    manifest = cross_file_view_manifest(overlay)
    stems = manifest.get("infer_side_on_stems") or []
    return [str(s) for s in stems] if isinstance(stems, list) else []


def assembly_split_min_gap_ratio(overlay: Optional[str | Path | dict] = None) -> float:
    manifest = cross_file_view_manifest(overlay)
    val = manifest.get("assembly_split_min_gap_ratio")
    return float(val) if val is not None else 0.5
