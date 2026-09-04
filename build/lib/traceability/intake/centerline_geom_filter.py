# -*- coding: utf-8 -*-
"""P2.4 / 05 分册：centerline 几何 keep/drop（MLLM 不可用时的生产回退）。

MLLM centerline 分类不可用时，用纯几何规则滤掉尺寸线/短碎段/图框类噪声，
坐标仍来自 DXF 矢量中心线。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .tower_spec import load_tower_spec


def _seg_len(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x2 - x1, y2 - y1)


def _angle_deg(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0


def _is_dim_like(
    length: float,
    ang: float,
    *,
    dim_max_len: float,
    dim_angle_tol: float,
) -> bool:
    """近水平/近竖直的短线——尺寸标注特征。"""
    if length > dim_max_len:
        return False
    horiz = min(abs(ang), abs(180.0 - ang)) <= dim_angle_tol
    vert = abs(ang - 90.0) <= dim_angle_tol
    return horiz or vert


def filter_config_for_stem(stem: str, overlay: Optional[Any]) -> Dict[str, float]:
    spec = load_tower_spec(overlay)
    by_stem = spec.get("centerline_geom_filter_by_stem") or {}
    defaults = {
        "min_len_mm": float(spec.get("centerline_geom_min_len_mm", 80.0)),
        "dim_max_len_mm": float(spec.get("centerline_geom_dim_max_len_mm", 450.0)),
        "dim_angle_tol_deg": float(spec.get("centerline_geom_dim_angle_tol_deg", 8.0)),
    }
    stem_cfg = by_stem.get(stem) if isinstance(by_stem, dict) else None
    if not isinstance(stem_cfg, dict):
        return defaults
    return {
        "min_len_mm": float(stem_cfg.get("min_len_mm", defaults["min_len_mm"])),
        "dim_max_len_mm": float(stem_cfg.get("dim_max_len_mm", defaults["dim_max_len_mm"])),
        "dim_angle_tol_deg": float(
            stem_cfg.get("dim_angle_tol_deg", defaults["dim_angle_tol_deg"])),
    }


def should_keep_centerline_segment(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    scale_mm: float = 1.0,
    cfg: Optional[Dict[str, float]] = None,
) -> Tuple[bool, Optional[str]]:
    cfg = cfg or {}
    length = _seg_len(x1, y1, x2, y2) * float(scale_mm)
    min_len = float(cfg.get("min_len_mm", 80.0))
    if length < min_len:
        return False, "too_short"
    ang = _angle_deg(x1, y1, x2, y2)
    if _is_dim_like(
        length,
        ang,
        dim_max_len=float(cfg.get("dim_max_len_mm", 450.0)),
        dim_angle_tol=float(cfg.get("dim_angle_tol_deg", 8.0)),
    ):
        return False, "dim_like"
    return True, None


def filter_drawing_bars(
    bars: Sequence[Dict[str, Any]],
    *,
    stem: str,
    overlay: Optional[Any] = None,
    scale_mm: float = 1.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """过滤 hybrid ezdxf_bars（drawing 坐标）。返回 kept, dropped_audit, report。"""
    cfg = filter_config_for_stem(stem, overlay)
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {}
    for b in bars:
        try:
            x1, y1 = float(b["x1"]), float(b["y1"])
            x2, y2 = float(b["x2"]), float(b["y2"])
        except (KeyError, TypeError, ValueError):
            kept.append(dict(b))
            continue
        ok, reason = should_keep_centerline_segment(
            x1, y1, x2, y2, scale_mm=scale_mm, cfg=cfg)
        if ok:
            kept.append(dict(b))
        else:
            reasons[reason or "?"] = reasons.get(reason or "?", 0) + 1
            dropped.append({
                "bar_uid": b.get("bar_uid"),
                "component_id": b.get("component_id"),
                "reason": reason,
                "len_mm": round(_seg_len(x1, y1, x2, y2) * scale_mm, 1),
            })
    return kept, dropped, {
        "stem": stem,
        "n_in": len(bars),
        "n_kept": len(kept),
        "n_dropped": len(dropped),
        "reasons": reasons,
        "config": cfg,
    }


def filter_bar_segments(
    segments: Sequence[Dict[str, Any]],
    *,
    stem: str,
    overlay: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """过滤 tower_dxf bar_segments（图纸单位，含 scale_ratio）。"""
    cfg = filter_config_for_stem(stem, overlay)
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {}
    for seg in segments:
        scale = float(seg.get("scale_ratio") or 1.0)
        s, e = seg["start"], seg["end"]
        ok, reason = should_keep_centerline_segment(
            float(s[0]), float(s[1]), float(e[0]), float(e[1]),
            scale_mm=scale, cfg=cfg)
        if ok:
            kept.append(seg)
        else:
            reasons[reason or "?"] = reasons.get(reason or "?", 0) + 1
            dropped.append({"reason": reason, "layer": seg.get("layer")})
    return kept, {
        "stem": stem,
        "n_in": len(segments),
        "n_kept": len(kept),
        "n_dropped": len(dropped),
        "reasons": reasons,
    }


def stem_uses_centerline_geom_filter(stem: str, overlay: Optional[Any]) -> bool:
    spec = load_tower_spec(overlay)
    sheets = spec.get("mllm_keep_drop_sheets") or spec.get("centerline_geom_filter_sheets") or []
    return stem in sheets and bool(spec.get("centerline_geom_filter", True))
