#!/usr/bin/env python3
"""Phase 6.5：节点板 + 螺栓样例 GLB 生成器（TASK_VIEWER_POLISH 任务 6.5）。

背景：全塔模型 0 个节点板——节点板只存在于详图页（03 页 gusset_D1），且
polygon_global 未解算（主线程 Phase 7 的几何问题，本脚本不做全局定位）。
本脚本用 polygon_local + bolt_group 数据在**自建样例坐标系**里生成一个
可交互查看的节点板样例：板体 + 真孔位标记 + 连接杆件截面示意。

数据现实（2026-08-31 勘察，写进产物 note 保持诚实）：
    * gusset_D1.polygon_local 只有 10 点、bbox ≈19×16 单位——解析碎片，
      远小于螺栓孔群分布范围（56 孔跨 250×300）；
    * gusset_D1.bolt_holes 是 32 个 dict 键名的扁平残留（非孔坐标），
      真实孔位以 16 个 bolt_group_D1_B* 组件为准（共 56 孔，孔径 17.5mm）；
    * thickness_mm 为 null → 按任务书假定 8mm。
因此板体轮廓 = convex_hull(polygon_local ∪ 全部孔心) 外扩 margin，
保证所有孔都在板内；轮廓方法写进 bar_map.note，不冒充图纸原物。

孔的呈现：无 CSG 后端（manifold3d/blender 均缺）→ 任务书允许的
「圆片标记」方案：每孔一个深色贯通圆柱片（半径=hole_diameter/2）。

示意杆：取孔数最多的 2 个螺栓组，沿孔群主轴在板面两侧各摆一根
L 型截面短柱（--stub-section，默认 Q345L100X7），bar_map 标
kind=bar_stub_schematic（示意，非图纸实体）。

输出：
    web/demo/35A1-JC1/detail_sample.glb
    web/demo/35A1-JC1/detail_sample.bar_map.json

用法：
    python3 scripts/build_detail_sample.py [--sheet PATH] [--out-dir DIR]
        [--plate-thickness 8] [--margin 25] [--stub-section Q345L100X7]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SHEET = REPO_ROOT / "web/demo/35A1-JC1/latest_deliver/sheets/35A1-JC1-03.json"
DEFAULT_OUT_DIR = REPO_ROOT / "web/demo/35A1-JC1"

Point = Tuple[float, float]


# --------------------------------------------------------------------------- #
# 纯 Python 凸包 / 外扩（不依赖 shapely——环境未装）
# --------------------------------------------------------------------------- #

def convex_hull(points: List[Point]) -> List[Point]:
    """Andrew 单调链，返回 CCW 凸包（去共线）。"""
    pts = sorted(set((float(x), float(y)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 1e-9:
            lower.pop()
        lower.append(p)
    upper: List[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 1e-9:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def offset_convex_ccw(poly: List[Point], margin: float) -> List[Point]:
    """CCW 凸多边形每条外法向平移 margin，相邻平移边求交 → 外扩多边形。"""
    n = len(poly)
    lines = []  # (点, 单位方向)
    for i in range(n):
        p1, p2 = poly[i], poly[(i + 1) % n]
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        ln = math.hypot(dx, dy)
        if ln < 1e-12:
            continue
        ux, uy = dx / ln, dy / ln
        # CCW 的外法向 = 方向顺时针旋转 90°：(uy, -ux)
        nx, ny = uy, -ux
        lines.append(((p1[0] + nx * margin, p1[1] + ny * margin), (ux, uy)))

    def intersect(l1, l2) -> Optional[Point]:
        (p1, d1), (p2, d2) = l1, l2
        den = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(den) < 1e-12:
            return p2
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
        return (p1[0] + d1[0] * t, p1[1] + d1[1] * t)

    return [intersect(lines[i - 1], lines[i]) or lines[i][0]
            for i in range(len(lines))]


def extrude_polygon_mesh(poly: List[Point], thickness: float):
    """凸多边形（CCW）拉伸成 watertight 实体：扇形封盖 + 侧壁四边面。"""
    import numpy as np
    import trimesh

    n = len(poly)
    z0, z1 = 0.0, float(thickness)
    verts = np.zeros((2 * n, 3))
    for i, (x, y) in enumerate(poly):
        verts[i] = (x, y, z0)
        verts[n + i] = (x, y, z1)
    faces: List[Tuple[int, int, int]] = []
    for i in range(1, n - 1):          # 底盖（法向 -Z）
        faces.append((0, i + 1, i))
    for i in range(1, n - 1):          # 顶盖（法向 +Z）
        faces.append((n, n + i, n + i + 1))
    for i in range(n):                 # 侧壁
        j = (i + 1) % n
        faces.append((i, j, n + j))
        faces.append((i, n + j, n + i))
    return trimesh.Trimesh(vertices=verts, faces=np.array(faces), process=True)


# --------------------------------------------------------------------------- #
# 03 页数据解析
# --------------------------------------------------------------------------- #

def parse_detail_sheet(sheet_path: Path) -> dict:
    """读详图页 JSON → {detail_id, polygon_local, groups:[{id,holes,...}]}。"""
    data = json.loads(Path(sheet_path).read_text(encoding="utf-8"))
    comps = data.get("components", {})
    gussets = {k: v for k, v in comps.items() if v.get("kind") == "gusset_plate"}
    if not gussets:
        raise SystemExit(f"{sheet_path} 里没有 gusset_plate 组件")
    # 取第一个节点板（03 页当前只有 D1；多板时逐个跑）
    gid, g = sorted(gussets.items())[0]
    props = g.get("properties", {})
    groups = []
    for k, v in comps.items():
        if v.get("kind") != "bolt_group":
            continue
        p = v.get("properties", {})
        holes = [(float(h[0]), float(h[1])) for h in (p.get("holes") or []) if len(h) >= 2]
        if holes:
            groups.append({
                "component_id": k,
                "group_id": p.get("group_id") or k,
                "holes": holes,
                "count": p.get("count") or len(holes),
                "diameter_mm": p.get("diameter_mm"),
                "hole_diameter_mm": p.get("hole_diameter_mm"),
                "length_mm": p.get("length_mm"),
            })
    return {
        "gusset_component_id": gid,
        "detail_id": props.get("detail_id") or gid,
        "polygon_local": [tuple(map(float, p[:2])) for p in (props.get("polygon_local") or [])],
        "thickness_mm": props.get("thickness_mm"),
        "material": (props.get("material") or "").strip() or None,
        "solve_status": props.get("solve_status"),
        "transform": props.get("transform") or {},
        "groups": sorted(groups, key=lambda x: x["component_id"]),
    }


# --------------------------------------------------------------------------- #
# 样例场景组装
# --------------------------------------------------------------------------- #

def _l_stub_mesh(trimesh, leg: float, t: float, length: float):
    """L 型截面短柱（两正交薄板盒），底面中心在原点、沿 +Z 拉伸。"""
    web = trimesh.creation.box(extents=(leg, t, length))
    web.apply_translation((leg / 2 - t / 2, 0, length / 2))
    flange = trimesh.creation.box(extents=(t, leg, length))
    flange.apply_translation((0, leg / 2 - t / 2, length / 2))
    return trimesh.util.concatenate([web, flange])


def _parse_stub_section(section: str) -> Tuple[float, float]:
    import re
    m = re.search(r"L\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)", str(section or ""))
    if m:
        return float(m.group(1)), float(m.group(2))
    return 100.0, 7.0


def build_sample(detail: dict, plate_thickness: float, margin: float,
                 stub_section: str) -> Tuple[object, List[dict]]:
    """返回 (trimesh.Scene, bar_map 列表)。"""
    import trimesh
    import numpy as np

    groups = detail["groups"]
    all_holes: List[Tuple[str, Point]] = [
        (g["component_id"], h) for g in groups for h in g["holes"]]
    if not all_holes:
        raise SystemExit("样例页没有可用孔位（bolt_group holes 全空）")

    # 场景原点 = 孔心质心；轮廓 = hull(polygon ∪ 孔心) 外扩 margin
    cx = sum(h[1][0] for h in all_holes) / len(all_holes)
    cy = sum(h[1][1] for h in all_holes) / len(all_holes)
    seed = [(x - cx, y - cy) for x, y in detail["polygon_local"]] + \
           [(x - cx, y - cy) for _, (x, y) in all_holes]
    hull = convex_hull(seed)
    outline = offset_convex_ccw(hull, margin)

    scene = trimesh.Scene()
    bar_map: List[dict] = []

    # --- 板体（镀锌钢浅灰顶点色；viewer 侧再上金属材质） ---
    plate = extrude_polygon_mesh(outline, plate_thickness)
    plate.visual.face_colors = [176, 182, 192, 255]
    pid = f"detail_gusset_{detail['detail_id']}"
    plate.metadata = {"component_id": pid}
    scene.add_geometry(plate, geom_name=pid)
    bar_map.append({
        "component_id": pid, "kind": "gusset_plate", "detail_id": detail["detail_id"],
        "plate_thickness_mm": plate_thickness, "thickness_assumed": detail["thickness_mm"] is None,
        "n_holes": len(all_holes), "n_groups": len(groups),
        "material": detail["material"], "solve_status": detail["solve_status"],
        "outline_method": "convex_hull(polygon_local ∪ 孔心) 外扩 %gmm" % margin,
        "note": ("polygon_local 为解析碎片（bbox≈19×16），真实孔群跨 250×300；"
                 "板体轮廓为凸包修复，非图纸原物轮廓。polygon_global 未解算，"
                 "本样例仅展示节点板形态，非塔上 3D 位置。"),
    })

    # --- 孔片：每螺栓组合并成一个深色圆柱片网格（无 CSG 后端的标记方案） ---
    t = plate_thickness
    for g in groups:
        discs = []
        hd = float(g["hole_diameter_mm"] or 17.5) / 2.0
        for (hx, hy) in g["holes"]:
            disc = trimesh.creation.cylinder(radius=hd, height=t + 0.8, sections=20)
            disc.apply_translation((hx - cx, hy - cy, t / 2))
            discs.append(disc)
        merged = trimesh.util.concatenate(discs)
        merged.visual.face_colors = [38, 42, 50, 255]
        mid = f"detail_holes_{g['component_id']}"
        merged.metadata = {"component_id": mid}
        scene.add_geometry(merged, geom_name=mid)
        bar_map.append({
            "component_id": mid, "kind": "bolt_holes", "detail_id": detail["detail_id"],
            "group_id": g["group_id"], "count": g["count"],
            "hole_diameter_mm": g["hole_diameter_mm"], "bolt_diameter_mm": g["diameter_mm"],
            "bolt_length_mm": g["length_mm"],
        })

    # --- 示意杆：孔数最多的 2 组，沿孔群主轴在板面两侧各摆一根 L 截面短柱 ---
    leg, th = _parse_stub_section(stub_section)
    stub_len = 220.0
    top_groups = sorted(groups, key=lambda g: -len(g["holes"]))[:2]
    for gi, g in enumerate(top_groups):
        pts = np.array([[x - cx, y - cy] for (x, y) in g["holes"]])
        mean = pts.mean(axis=0)
        u, s, vt = np.linalg.svd(pts - mean, full_matrices=False)
        axis = vt[0]           # 孔群主轴
        normal = vt[1]         # 板面内垂直方向（示意杆分置两侧）
        spread = float(np.abs((pts - mean) @ normal).max())
        for side in (-1, 1):
            stub = _l_stub_mesh(trimesh, leg, th, stub_len)
            ang = math.atan2(axis[1], axis[0])
            rot = np.eye(4)
            c_, s_ = math.cos(ang), math.sin(ang)
            rot[:2, :2] = [[c_, -s_], [s_, c_]]
            stub.apply_transform(rot)
            off = mean + normal * side * (spread + leg + 8.0)
            stub.apply_translation((off[0], off[1], t))
            stub.visual.face_colors = [120, 150, 200, 255]
            sid = f"detail_stub_{detail['detail_id']}_{gi}_{'NS' if side > 0 else 'SN'}"
            stub.metadata = {"component_id": sid}
            scene.add_geometry(stub, geom_name=sid)
            bar_map.append({
                "component_id": sid, "kind": "bar_stub_schematic",
                "detail_id": detail["detail_id"], "group_id": g["group_id"],
                "section": stub_section,
                "note": "连接示意短柱（03 页无杆件实体数据），非图纸对象",
            })

    return scene, bar_map


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="详图页节点板 → 样例 GLB（Phase 6.5）")
    ap.add_argument("--sheet", default=str(DEFAULT_SHEET))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--plate-thickness", type=float, default=8.0,
                    help="板厚 mm（数据 thickness 为 null 时的假定值，任务书钉 8）")
    ap.add_argument("--margin", type=float, default=25.0, help="凸包外扩边距 mm")
    ap.add_argument("--stub-section", default="Q345L100X7", help="示意杆 L 截面规格")
    args = ap.parse_args(argv)

    if not Path(args.sheet).exists():
        print(f"详图页不存在：{args.sheet}（先跑 scripts/sync_demo_assets.py）", file=sys.stderr)
        return 2
    detail = parse_detail_sheet(Path(args.sheet))
    scene, bar_map = build_sample(detail, args.plate_thickness, args.margin, args.stub_section)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    glb = out_dir / "detail_sample.glb"
    scene.export(str(glb))
    (out_dir / "detail_sample.bar_map.json").write_text(
        json.dumps(bar_map, ensure_ascii=False, indent=1), encoding="utf-8")
    n_holes = sum(1 for e in bar_map if e["kind"] == "bolt_holes")
    print(f"节点板样例：detail {detail['detail_id']} | "
          f"{len(bar_map)} mesh（板 1 + 螺栓组 {n_holes} + 示意杆若干）| "
          f"孔 {bar_map[0]['n_holes']} 个")
    print(f"  → {glb}")
    print(f"  → {out_dir / 'detail_sample.bar_map.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
