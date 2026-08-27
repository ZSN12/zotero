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
