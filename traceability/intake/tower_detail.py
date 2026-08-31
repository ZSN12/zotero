"""节点大样 DXF 抽取（Gap 2 / M3）。

从 detail 视图区域抽取：
    * 闭合 LINE/LWPOLYLINE 环 → GussetPlate
    * CIRCLE + 螺栓标注 TEXT → BoltGroup + r_bolt_group 规则
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..connection.bolt_verify import (
    BoltGroup,
    inject_bolt_verification_rule,
    parse_bolt_annotation,
    verify_bolt_group,
)
from ..connection.detail_view import attach_detail_transform, parse_detail_view_meta
from ..connection.gusset import add_gusset_to_model, parse_gusset_from_detail
from ..model import EngineeringModel
from .tower_dxf import _flatten_modelspace_entities, _in_region

_THICKNESS_RE = re.compile(
    r"(?:t\s*=?\s*|厚度\s*)(\d+(?:\.\d+)?)\s*(?:mm)?",
    re.IGNORECASE,
)


def _snap(pt: Tuple[float, float], eps: float = 0.5) -> Tuple[float, float]:
    return (round(pt[0] / eps) * eps, round(pt[1] / eps) * eps)


def _collect_segments(msp, region: dict) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    edges: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for e in _flatten_modelspace_entities(msp):
        if e.dxftype() == "LINE":
            pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
        elif e.dxftype() == "LWPOLYLINE":
            raw = list(e.get_points("xy"))
            pts = raw if e.closed and len(raw) > 1 else []
            if not e.closed:
                pts = raw
            if len(raw) >= 2 and not e.closed:
                for i in range(len(raw) - 1):
                    a, b = raw[i], raw[i + 1]
                    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                    if _in_region(mx, my, region):
                        edges.append((_snap(a), _snap(b)))
                continue
        else:
            continue
        if len(pts) >= 2:
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                if _in_region(mx, my, region):
                    edges.append((_snap(a), _snap(b)))
    return edges


def _find_largest_cycle(edges: List[Tuple[Tuple[float, float], Tuple[float, float]]],
                        max_len: int = 24) -> Optional[List[Tuple[float, float]]]:
    """从线段图找最大简单环（节点板外轮廓近似）。"""
    adj: Dict[Tuple[float, float], set] = defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    if not adj:
        return None

    best: Optional[List[Tuple[float, float]]] = None
    starts = sorted(adj.keys(), key=lambda k: -len(adj[k]))[:12]
    for start in starts:
        stack: List[Tuple[Tuple[float, float], List[Tuple[float, float]], set]] = [
            (start, [start], {start}),
        ]
        while stack:
            node, path, seen = stack.pop()
            if len(path) > max_len:
                continue
            for nb in adj[node]:
                if nb == start and len(path) >= 3:
                    if best is None or len(path) > len(best):
                        best = path[:]
                elif nb not in seen:
                    stack.append((nb, path + [nb], seen | {nb}))
    return best


def _collect_circles(msp, region: dict) -> List[Tuple[float, float, float]]:
    circles: List[Tuple[float, float, float]] = []
    for e in _flatten_modelspace_entities(msp):
        if e.dxftype() != "CIRCLE":
            continue
        cx, cy = e.dxf.center.x, e.dxf.center.y
        if _in_region(cx, cy, region):
            circles.append((cx, cy, float(e.dxf.radius)))
    return circles


def _dominant_hole_radius(circles: List[Tuple[float, float, float]]) -> Optional[float]:
    if not circles:
        return None
    rounded = [round(r, 1) for _, _, r in circles]
    top = Counter(rounded).most_common(1)[0][0]
    return float(top)


def _collect_texts(msp, region: dict) -> List[Tuple[float, float, str]]:
    out: List[Tuple[float, float, str]] = []
    for e in _flatten_modelspace_entities(msp):
        if e.dxftype() == "TEXT":
            t = str(e.dxf.text or "")
            x, y = e.dxf.insert.x, e.dxf.insert.y
        elif e.dxftype() == "MTEXT":
            t = str(e.text or "")
            x, y = e.dxf.insert.x, e.dxf.insert.y
        else:
            continue
        if _in_region(x, y, region):
            out.append((x, y, t.strip()))
    return out


def _local_point(x: float, y: float, region: dict) -> Tuple[float, float]:
    ox, oy = region.get("origin", [0.0, 0.0])
    return round(x - float(ox), 2), round(y - float(oy), 2)


def extract_detail_connections(
    model: EngineeringModel,
    msp,
    regions: List[dict],
    stem: str,
    dxf_path: str | Path,
    overlay: Optional[str | Path | dict] = None,
) -> Dict[str, Any]:
    """从大样视图区域抽取节点板与螺栓群，写入 model 并注入验算规则。

    仅处理 kind="detail" 的视图区域。立面图（front/elevation 等）不在此列——
    2026-08-31 实测教训：04/05/06/07 分段立面图因文件名规则被判 node_detail，
    旧版 fallback `or list(regions)` 会把整个 front 区域（含材料表）当大样处理，
    BOM 表中的螺栓条目（如 '9M16X40'）被当作孔位标注、表格符号圆被抓为孔，
    产生 113 个必然失败的假 bolt_group 规则（孔间距 2.5mm、孔在轮廓外）。
    无 detail 区域时直接返回空报告（该图无节点大样是诚实结论）。
    """
    detail_regions = [r for r in regions if r.get("kind") == "detail"]
    report: Dict[str, Any] = {"plates": 0, "bolt_groups": 0, "rules": [], "skipped_no_detail_region": not detail_regions}
    if not detail_regions:
        return report

    for idx, region in enumerate(detail_regions):
        title = str(region.get("title") or f"{stem} detail")
        transform = parse_detail_view_meta(title, region.get("region"))
        transform.detail_id = transform.detail_id if transform.detail_id != "detail" else f"D{idx + 1}"
        attach_detail_transform(model, transform)

        edges = _collect_segments(msp, region)
        cycle = _find_largest_cycle(edges)
        circles = _collect_circles(msp, region)
        texts = _collect_texts(msp, region)
        hole_r = _dominant_hole_radius(circles)

        thickness_text = None
        for _, _, t in texts:
            if _THICKNESS_RE.search(t):
                thickness_text = t
                break

        polygon_local: List[Tuple[float, float]] = []
        if cycle:
            polygon_local = [_local_point(x, y, region) for x, y in cycle]
        elif circles:
            xs = [c[0] for c in circles]
            ys = [c[1] for c in circles]
            pad = (hole_r or 1.0) * 4
            polygon_local = [
                _local_point(min(xs) - pad, min(ys) - pad, region),
                _local_point(max(xs) + pad, min(ys) - pad, region),
                _local_point(max(xs) + pad, max(ys) + pad, region),
                _local_point(min(xs) - pad, max(ys) + pad, region),
            ]

        plate_id = transform.detail_id
        plate = parse_gusset_from_detail(
            plate_id,
            polygon_local,
            thickness_text=thickness_text,
            transform=transform,
        )
        if polygon_local:
            add_gusset_to_model(model, plate)
            report["plates"] += 1

        bolt_annos = [(x, y, t) for x, y, t in texts if parse_bolt_annotation(t)]
        hole_pts = [
            (cx, cy) for cx, cy, r in circles
            if hole_r is None or abs(r - hole_r) <= max(0.6, hole_r * 0.25)
        ]
        used_holes: set = set()
        for gi, (tx, ty, anno) in enumerate(bolt_annos):
            spec = parse_bolt_annotation(anno)
            if spec is None:
                continue
            ranked = sorted(
                hole_pts,
                key=lambda h: (h[0] - tx) ** 2 + (h[1] - ty) ** 2,
            )
            holes_local: List[Tuple[float, float]] = []
            for h in ranked:
                key = (round(h[0], 1), round(h[1], 1))
                if key in used_holes:
                    continue
                holes_local.append(_local_point(h[0], h[1], region))
                used_holes.add(key)
                if len(holes_local) >= spec.count:
                    break
            if not holes_local:
                continue
            gid = f"{plate_id}_B{gi + 1}"
            outline = polygon_local or None
            group = BoltGroup(
                group_id=gid,
                spec=spec,
                holes=holes_local,
                plate_outline=outline,
            )
            model.add_component(group.to_component())
            result = verify_bolt_group(group)
            inject_bolt_verification_rule(model, group, result)
            report["bolt_groups"] += 1
            report["rules"].append(f"r_bolt_group_{gid}")

            if plate.bolt_holes is not None:
                plate.bolt_holes.extend(
                    {"group_id": gid, "holes": [list(h) for h in holes_local]}
                )

    df = model.components.get("drawing_file")
    if df is not None:
        df.properties["detail_extract"] = report

    from ..harness.tower_validators import inject_connection_rules
    inject_connection_rules(model)
    return report
