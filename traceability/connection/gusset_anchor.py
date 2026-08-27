"""大样节点板全局锚定（M4 / Gap 2 POC）。

策略（按优先级）：
    1. overlay.gusset_anchors 显式 {gusset_component_id: tower_node_id}
    2. overlay.gusset_auto_anchor=true → 大样 region 中心映射到 front (view_x, view_y)，
       找最近已解算 front 节点（确定性，非随机）
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..model import EngineeringModel
from .detail_view import DetailViewTransform, anchor_transform, local_to_global
from .gusset import GussetPlate, parse_gusset_from_detail


def _transform_from_dict(data: Dict[str, Any]) -> DetailViewTransform:
    og = data.get("origin_global") or [0.0, 0.0, 0.0]
    ol = data.get("origin_local") or [0.0, 0.0]
    return DetailViewTransform(
        detail_id=str(data.get("detail_id") or "detail"),
        scale_to_real=float(data.get("scale_to_real") or 1.0),
        origin_local=(float(ol[0]), float(ol[1])),
        origin_global=(float(og[0]), float(og[1]), float(og[2])),
        rotation_deg=float(data.get("rotation_deg") or 0.0),
        anchor_node_id=data.get("anchor_node_id"),
        anchored=bool(data.get("anchored")),
    )


def _detail_center_vx_vy(detail_region: dict, front_region: dict) -> Tuple[float, float]:
    """大样 region 中心 → front 视图局部 (view_x, view_y≈z)。"""
    reg = detail_region.get("region") or []
    if len(reg) >= 4:
        cx = (float(reg[0]) + float(reg[1])) / 2.0
        cy = (float(reg[2]) + float(reg[3])) / 2.0
    else:
        ox, oy = detail_region.get("origin") or [0.0, 0.0]
        cx, cy = float(ox), float(oy)
    fox, foy = front_region.get("origin") or [0.0, 0.0]
    return cx - float(fox), cy - float(foy)


def _find_nearest_front_node(
    model: EngineeringModel,
    target_vx: float,
    target_vy: float,
) -> Optional[str]:
    best: Optional[Tuple[float, str]] = None
    for cid, comp in model.components.items():
        if comp.kind != "tower_node":
            continue
        p = comp.properties
        if p.get("view_type") != "front" or p.get("solve_status") != "solved":
            continue
        vx, vy = p.get("view_x"), p.get("view_y")
        if vx is None or vy is None:
            continue
        d = math.hypot(float(vx) - target_vx, float(vy) - target_vy)
        if best is None or d < best[0]:
            best = (d, cid)
    return best[1] if best else None


def anchor_gusset_to_node(
    model: EngineeringModel,
    gusset_cid: str,
    node_cid: str,
    *,
    anchor_origin: str = "manual",
) -> bool:
    """把 pending 节点板锚定到 tower_node，写入 polygon_global。"""
    gusset = model.components.get(gusset_cid)
    node = model.components.get(node_cid)
    if gusset is None or node is None or gusset.kind != "gusset_plate":
        return False
    if node.kind != "tower_node":
        return False
    nx, ny, nz = node.properties.get("x"), node.properties.get("y"), node.properties.get("z")
    if None in (nx, ny, nz):
        return False

    tdict = dict(gusset.properties.get("transform") or {})
    transform = _transform_from_dict(tdict)
    transform.origin_local = (0.0, 0.0)
    transform = anchor_transform(
        transform,
        (float(nx), float(ny), float(nz)),
        anchor_node_id=node_cid,
    )

    poly_local = [
        (float(p[0]), float(p[1]))
        for p in (gusset.properties.get("polygon_local") or [])
    ]
    global_poly: List[List[float]] = []
    for p in poly_local:
        gp = local_to_global(p[0], p[1], transform, z_global=float(nz))
        if gp is not None:
            global_poly.append(list(gp))

    gusset.properties["transform"] = transform.to_dict()
    gusset.properties["anchor_node_id"] = node_cid
    gusset.properties["anchor_origin"] = anchor_origin
    gusset.properties["solve_status"] = "verified" if global_poly else "pending_review"
    if global_poly:
        gusset.properties["polygon_global"] = global_poly
        gusset.properties.pop("global_coords", None)
    else:
        gusset.properties["global_coords"] = "pending_anchor"

    detail_id = gusset.properties.get("detail_id")
    source_file = gusset.properties.get("source_file")
    for cid, comp in model.components.items():
        if comp.kind != "detail_view":
            continue
        if comp.properties.get("detail_id") != detail_id:
            continue
        if source_file and comp.properties.get("source_file") != source_file:
            continue
        comp.properties.update(transform.to_dict())
        comp.properties["solve_status"] = "verified" if transform.anchored else "pending_review"
    return bool(global_poly)


def auto_anchor_gussets(
    model: EngineeringModel,
    overlay: Optional[str | Path | dict] = None,
) -> Dict[str, Any]:
    """按 overlay 显式映射或 nearest_front 策略锚定全部 pending gusset。"""
    from ..intake.tower_spec import load_tower_spec, view_regions

    ov = load_tower_spec(overlay) if overlay else {}
    explicit: Dict[str, str] = dict(ov.get("gusset_anchors") or {})
    auto = bool(ov.get("gusset_auto_anchor"))

    cross = ov.get("cross_file_views") or {}
    sheets = cross.get("sheets") or {}
    front_stem = sheets.get("front")
    front_region = None
    if front_stem:
        for r in view_regions(front_stem, overlay=overlay):
            if r.get("kind") == "front":
                front_region = r
                break

    report: Dict[str, Any] = {"anchored": [], "skipped": [], "pending": []}

    for cid, comp in list(model.components.items()):
        if comp.kind != "gusset_plate":
            continue
        if comp.properties.get("global_coords") != "pending_anchor" and not comp.properties.get("polygon_global"):
            if comp.properties.get("solve_status") == "verified":
                report["skipped"].append(cid)
            continue

        node_cid = explicit.get(cid)
        origin = "overlay_explicit"
        if not node_cid and auto and front_region:
            detail_stem = comp.properties.get("source_file") or comp.properties.get("drawing_view")
            detail_region = None
            if detail_stem:
                for r in view_regions(detail_stem, overlay=overlay):
                    if r.get("kind") == "detail":
                        detail_region = r
                        break
            if detail_region:
                tvx, tvy = _detail_center_vx_vy(detail_region, front_region)
                node_cid = _find_nearest_front_node(model, tvx, tvy)
                origin = "nearest_front_by_detail_region"

        if not node_cid:
            report["pending"].append(cid)
            continue
        if anchor_gusset_to_node(model, cid, node_cid, anchor_origin=origin):
            report["anchored"].append({"gusset": cid, "node": node_cid, "origin": origin})
        else:
            report["pending"].append(cid)

    df = model.components.get("drawing_file")
    if df is not None and report["anchored"]:
        df.properties["gusset_anchor_report"] = report

    if report["anchored"]:
        from ..harness.tower_validators import inject_connection_rules
        inject_connection_rules(model)
    return report
