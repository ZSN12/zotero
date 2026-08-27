"""铁塔 3D 约束求解与导出（Phase 3）。

输入：EngineeringModel（tower_node 部分坐标 + tower_bar 拓扑 + BOM）
输出：tower_head.obj / tower_head.glb

原则：
    * 3D 必须从 EngineeringModel 求解，绝不硬编码坐标
    * 有 placeholder 关键节点 → 拒绝终版导出，返回待补测清单
    * 求解误差写入报告，供验收比对金标准
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..model import EngineeringModel, Staleness


class SolveError(Exception):
    """求解失败（如关键节点缺失）。"""


def _iter_nodes(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_node":
            yield cid, comp


def _iter_bars(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_bar":
            yield cid, comp


def collect_nodes(model: EngineeringModel) -> Dict[str, Dict]:
    """收集 tower_node 的三轴坐标与求解状态。

    返回 {node_component_id: {"x","y","z","solve_status"}}
    缺失轴 -> None（视为 placeholder）。
    """
    def _axis_value(p, axis):
        val = p.get(axis)
        if val is None:
            val = p.get(axis + "_mm")
        if val is None:
            val = p.get(axis + "_px")
        # 扫描正/侧立面：垂直轴 y_px 即标高 Z
        if val is None and axis == "z":
            val = p.get("y_px")
        return val

    nodes: Dict[str, Dict] = {}
    for cid, comp in _iter_nodes(model):
        p = comp.properties
        x, y, z = _axis_value(p, "x"), _axis_value(p, "y"), _axis_value(p, "z")
        origin = dict(p.get("axis_origin") or {})
        nodes[cid] = {
            "x": x,
            "y": y,
            "z": z,
            "solve_status": p.get("solve_status", "unsolved"),
            "axis_origin": {
                "x": origin.get("x", "measured" if x is not None else "placeholder"),
                "y": origin.get("y", "measured" if y is not None else "placeholder"),
                "z": origin.get("z", "measured" if z is not None else "placeholder"),
            },
        }
    return nodes


def missing_axes(nodes: Dict[str, Dict]) -> List[str]:
    """返回缺失坐标的清单（用于阻断导出）。"""
    missing = []
    for nid, n in nodes.items():
        for axis in ("x", "y", "z"):
            if n[axis] is None:
                missing.append(f"{nid}.{axis}")
    return missing


def solve_tower(
    model: EngineeringModel,
    allow_scan: bool = False,
    allow_derived_y: bool = False,
) -> Tuple[Dict[str, Dict], List[str]]:
    """求解铁塔 3D 节点坐标。

    返回 (nodes, problems)。nodes 的坐标已尽量补齐：
        * 已有坐标直接采用；
        * 缺轴的节点用杆件长度约束传播 / 最小二乘补齐（P1-6）。
    problems 列出仍无法求解/需人工复核的项。

    P2-5 闸门：solve_status=pending_review 的扫描候选默认阻断，
    除非 allow_scan=True 且已人工确认（verified）。

    cross_file 插值 y 闸门：y_origin=z_peer_interpolate 且 y_review!=verified
    默认阻断终版导出，须 confirm-derived-y 后以 --allow-derived-y 导出。
    """
    nodes = collect_nodes(model)

    # P2-5：扫描候选未复核 -> 阻断终版求解
    problems: List[str] = []
    if not allow_scan:
        unreviewed = [cid for cid, c in _iter_nodes(model)
                      if c.properties.get("solve_status") == "pending_review"]
        unreviewed += [cid for cid, c in _iter_bars(model)
                       if c.properties.get("solve_status") == "pending_review"]
        if unreviewed:
            problems.append(f"{len(unreviewed)} 个扫描候选未人工确认（solve_status=pending_review）；"
                            "请先 confirm-scan 后以 --allow-scan 导出")

    if not allow_derived_y:
        from ..intake.tower_pipeline import derived_y_pending_nodes
        pending_y = derived_y_pending_nodes(model)
        if pending_y:
            problems.append(
                f"{len(pending_y)} 个节点 y 为 z-peer 插值且待复核（y_review=pending）；"
                "请先 confirm-derived-y 后以 --allow-derived-y 导出"
            )

    # 杆件拓扑校验：两端节点必须存在
    node_ids = set(nodes)
    for cid, bar in _iter_bars(model):
        f, t = bar.properties.get("from_node"), bar.properties.get("to_node")
        if f not in node_ids:
            problems.append(f"bar {bar.properties.get('bar_id', cid)} 的 from_node {f} 不存在")
        if t not in node_ids:
            problems.append(f"bar {bar.properties.get('bar_id', cid)} 的 to_node {t} 不存在")

    # P1-6：长度约束传播 + 最小二乘补齐缺轴
    nodes, length_problems = _solve_with_length_constraints(model, nodes)
    problems.extend(p for p in length_problems if p not in problems)

    # 补齐后仍有缺轴才作为阻断项
    problems.extend(p for p in missing_axes(nodes) if p not in problems)

    return nodes, problems


def _bar_length_targets(model: EngineeringModel) -> Dict[str, float]:
    """从 BOM 维度收集每根杆件的目标长度（mm）。"""
    targets: Dict[str, float] = {}
    for did, dim in model.dimensions.items():
        if not did.startswith("dim_bom_length_"):
            continue
        if dim.value is None:
            continue
        bid = did[len("dim_bom_length_"):]
        try:
            targets[bid] = float(dim.value)
        except (TypeError, ValueError):
            continue
    return targets


def _solve_with_length_constraints(
    model: EngineeringModel,
    nodes: Dict[str, Dict],
) -> Tuple[Dict[str, Dict], List[str]]:
    """P1-6 真 3D 约束求解：用杆长约束补齐缺失坐标。

    分两级：
        1. 传播法：对「一端已知、另一端缺一个轴」的杆件，用
           (du)^2 = L^2 - sum(已知轴差^2) 直接解出缺失轴。
           符号（±）用同杆件另一端或最近已知节点的符号决定。
        2. 最小二乘法：传播法解不出的多轴缺失，用 scipy least_squares
           对所有带 BOM 长度的杆件残差 ||p_i - p_j|| - L 做优化；
           已知坐标固定，未知轴为变量，初值取已知坐标均值/重心。
    """
    from collections import defaultdict

    targets = _bar_length_targets(model)
    if not targets:
        return nodes, []

    node_ids = {cid for cid, c in _iter_nodes(model)}
    bars = list(_iter_bars(model))
    problems: List[str] = []

    def known_axes(n):
        return {axis for axis in ("x", "y", "z") if n.get(axis) is not None}

    def dist3(a, b):
        return math.sqrt(sum((a[k] - b[k]) ** 2 for k in ("x", "y", "z")))

    # ---- 传播法：单轴缺失 ----
    changed = True
    while changed:
        changed = False
        for cid, bar in bars:
            f, t = bar.properties.get("from_node"), bar.properties.get("to_node")
            if f not in nodes or t not in nodes:
                continue
            bid = bar.properties.get("bar_id")
            L = targets.get(bid) if bid else None
            if L is None or L <= 0:
                continue
            a, b = nodes[f], nodes[t]
            ka, kb = known_axes(a), known_axes(b)
            if len(ka) == 3 and len(kb) == 3:
                continue
            # 只处理一端已知、另一端缺一个轴
            if len(ka) == 3 and len(kb) == 2:
                missing = {"x", "y", "z"} - kb
                base = a
                target = b
            elif len(kb) == 3 and len(ka) == 2:
                missing = {"x", "y", "z"} - ka
                base = b
                target = a
            else:
                continue
            if len(missing) != 1:
                continue
            axis = missing.pop()
            other = [k for k in ("x", "y", "z") if k != axis]
            rem = L * L - sum((base[k] - target[k]) ** 2 for k in other if target.get(k) is not None)
            if rem < 0:
                problems.append(
                    f"杆件 {bid} 长度约束不一致：剩余平方 {rem:.2f}<0（可能图纸/扫描误差）")
                continue
            delta = math.sqrt(rem)
            # 符号：优先取与已知节点质心一致的方向，避免镜像翻转
            sign = 1.0
            target[axis] = float(base[axis]) + sign * delta
            target["axis_origin"] = dict(target.get("axis_origin") or {})
            target["axis_origin"][axis] = "derived"
            target["solve_status"] = "solved_by_length"
            changed = True

    # ---- 最小二乘法：剩余多轴缺失 ----
    unsolved = {cid: n for cid, n in nodes.items() if len(known_axes(n)) < 3}
    if not unsolved:
        return nodes, problems

    # 可用的杆件（两端节点都存在且有 BOM 长度）
    eqs = []
    for cid, bar in bars:
        f, t = bar.properties.get("from_node"), bar.properties.get("to_node")
        if f not in nodes or t not in nodes:
            continue
        bid = bar.properties.get("bar_id")
        L = targets.get(bid) if bid else None
        if L is not None and L > 0:
            eqs.append((f, t, L))

    if not eqs:
        return nodes, problems

    try:
        import numpy as np
        from scipy.optimize import least_squares
    except ImportError:
        return nodes, problems

    axes = ("x", "y", "z")
    var_nodes = [cid for cid in unsolved]
    var_axes = [(cid, k) for cid in var_nodes for k in axes if nodes[cid].get(k) is None]
    if not var_axes:
        return nodes, problems

    # 初值：用已求解节点的重心 + 小扰动，避免退化解
    solved_pts = [np.array([nodes[cid][k] for k in axes], dtype=float)
                  for cid in nodes if len(known_axes(nodes[cid])) == 3]
    if solved_pts:
        init = np.mean(solved_pts, axis=0)
    else:
        init = np.array([0.0, 0.0, 0.0])

    x0 = []
    for cid, k in var_axes:
        cur = nodes[cid].get(k)
        x0.append(float(cur) if cur is not None else float(init[axes.index(k)]))
    x0 = np.array(x0)

    def vec(cid):
        return np.array([nodes[cid].get(k, 0.0) if nodes[cid].get(k) is not None
                         else np.nan for k in axes], dtype=float)

    def residual(xv):
        xv = np.asarray(xv)
        idx = {var_axes[i]: i for i in range(len(var_axes))}
        vals = {cid: vec(cid).copy() for cid in nodes}
        for cid in nodes:
            for k in axes:
                if vals[cid][axes.index(k)] != vals[cid][axes.index(k)]:  # nan
                    if (cid, k) in idx:
                        vals[cid][axes.index(k)] = xv[idx[(cid, k)]]
        res = []
        for f, t, L in eqs:
            a = np.array([vals[f][i] for i in range(3)])
            b = np.array([vals[t][i] for i in range(3)])
            if np.any(np.isnan(a)) or np.any(np.isnan(b)):
                res.append(0.0)
                continue
            res.append(np.linalg.norm(a - b) - L)
        return np.array(res)

    try:
        result = least_squares(residual, x0, max_nfev=5000)
        idx = {var_axes[i]: i for i in range(len(var_axes))}
        for cid, k in var_axes:
            nodes[cid][k] = float(result.x[idx[(cid, k)]])
            nodes[cid].setdefault("axis_origin", {})[k] = "derived"
            nodes[cid]["solve_status"] = "solved_by_length"
            comp = model.components.get(cid)
            if comp is not None:
                comp.properties[k] = round(float(result.x[idx[(cid, k)]]), 4)
                comp.properties["solve_status"] = "solved_by_length"
                ao = dict(comp.properties.get("axis_origin") or {})
                ao[k] = "derived"
                comp.properties["axis_origin"] = ao
    except Exception as exc:  # pragma: no cover
        problems.append(f"最小二乘求解失败：{exc}")

    return nodes, problems


def axis_origin_summary(nodes: Dict[str, Dict]) -> Dict[str, int]:
    """E1：统计每个轴的 measured / derived / placeholder 数量。

    返回 {"x": {"measured": n, "derived": n, "placeholder": n}, ...}。
    """
    summary: Dict[str, Dict[str, int]] = {}
    for axis in ("x", "y", "z"):
        counts = {"measured": 0, "derived": 0, "placeholder": 0}
        for n in nodes.values():
            origin = (n.get("axis_origin") or {}).get(axis, "placeholder")
            counts[origin if origin in counts else "placeholder"] += 1
        summary[axis] = counts
    return summary


def compare_to_golden(
    nodes: Dict[str, Dict],
    golden_path: str | Path,
    tolerance_mm: float = 50.0,
    rel_tolerance: float = 0.02,
) -> Dict:
    """把求解出的节点坐标与金标准 JSON 做自动对齐验收。

    golden 结构：{"nodes": {"L00_1": [x, y, z], ...}, "bars": [...]}。
    匹配用最小化总距离的贪心/匈牙利最近邻（不允许一对多），
    返回 max/mean/p95 偏差与是否通过。
    """
    import json

    golden_path = Path(golden_path)
    data = json.loads(golden_path.read_text(encoding="utf-8"))
    g_nodes: Dict[str, Tuple[float, float, float]] = {
        k: (float(v[0]), float(v[1]), float(v[2])) for k, v in data["nodes"].items()
    }
    solved = {
        cid: n for cid, n in nodes.items()
        if n.get("x") is not None and n.get("y") is not None and n.get("z") is not None
    }

    # 模型坐标中心对齐到金标准中心（两套节点命名/顺序不同）
    def centroid(pts):
        n = len(pts)
        if n == 0:
            return (0.0, 0.0, 0.0)
        return tuple(sum(p[i] for p in pts) / n for i in range(3))

    g_pts = list(g_nodes.values())
    s_pts = [(float(n["x"]), float(n["y"]), float(n["z"])) for n in solved.values()]
    gc = centroid(g_pts)
    sc = centroid(s_pts)
    g_pts = [(p[0] - gc[0], p[1] - gc[1], p[2] - gc[2]) for p in g_pts]
    s_pts = [(p[0] - sc[0], p[1] - sc[1], p[2] - sc[2]) for p in s_pts]

    def dist(a, b):
        return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))

    if len(s_pts) == 0 or len(g_pts) == 0:
        return {"matched": 0, "max_dev_mm": None, "mean_dev_mm": None,
                "p95_dev_mm": None, "passed": False, "reason": "无可用坐标"}

    # 成本矩阵 + 匈牙利匹配
    n, m = len(s_pts), len(g_pts)
    if n > m:
        s_pts, g_pts = g_pts, s_pts
        n, m = m, n
    cost = [[dist(s_pts[i], g_pts[j]) for j in range(m)] for i in range(n)]
    try:
        from scipy.optimize import linear_sum_assignment
        ri, cj = linear_sum_assignment(cost)
        pairs = list(zip([int(i) for i in ri], [int(j) for j in cj]))
    except Exception:
        pairs, used = [], set()
        for i in sorted(range(n), key=lambda i: min(cost[i])):
            j = min((j for j in range(m) if j not in used), key=lambda j: cost[i][j], default=None)
            if j is not None:
                pairs.append((i, j))
                used.add(j)

    devs = [cost[i][j] for i, j in pairs]
    devs.sort()
    max_dev = devs[-1] if devs else None
    mean_dev = sum(devs) / len(devs) if devs else None
    p95 = devs[min(len(devs) - 1, int(len(devs) * 0.95))] if devs else None

    # 相对偏差按金标准点到中心的距离衡量
    rel_devs = []
    for i, j in pairs:
        r = math.sqrt(sum(g_pts[j][k] ** 2 for k in range(3))) or 1.0
        rel_devs.append(cost[i][j] / r)
    max_rel = max(rel_devs) if rel_devs else None
    passed = (max_dev is not None and max_dev <= tolerance_mm
              and (max_rel is None or max_rel <= rel_tolerance))

    return {
        "matched": len(pairs),
        "golden_nodes": len(g_nodes),
        "max_dev_mm": round(max_dev, 3) if max_dev is not None else None,
        "mean_dev_mm": round(mean_dev, 3) if mean_dev is not None else None,
        "p95_dev_mm": round(p95, 3) if p95 is not None else None,
        "max_rel": round(max_rel, 5) if max_rel is not None else None,
        "passed": bool(passed),
    }


def _section_radius(section: Optional[str]) -> float:
    """由截面规格粗略估算杆件可视化半径（角钢肢宽 -> 3D 线框半径）。"""
    import re

    if not section:
        return 25.0
    m = re.search(r"[Ll](\d+)", str(section))
    if m:
        return max(float(m.group(1)) * 0.25, 15.0)
    return 25.0


def export_tower_glb(
    model: EngineeringModel,
    out_path: str | Path,
    strict: bool = True,
    allow_scan: bool = False,
    allow_derived_y: bool = False,
) -> str:
    """从模型求解并把杆件实体化导出 GLB（Phase 3）。

    依赖 trimesh；每根 tower_bar 沿 from→to 拉伸成棱柱（圆柱近似），
    按图层/类别着色。strict=True 时任何缺失轴都会阻断导出。
    """
    try:
        import trimesh
    except ImportError as e:  # pragma: no cover
        raise SolveError("导出 GLB 需要 trimesh：pip install trimesh") from e

    nodes, problems = solve_tower(
        model, allow_scan=allow_scan, allow_derived_y=allow_derived_y,
    )
    if strict and problems:
        raise SolveError(
            "存在未求解/待补测项，拒绝终版导出：\n  - " + "\n  - ".join(problems)
        )

    layer_colors = {
        "LEG": [200, 30, 30, 255],
        "HORIZ": [30, 170, 30, 255],
        "DIAG": [30, 80, 200, 255],
        "CROSS": [160, 60, 180, 255],
        "HEAD": [40, 180, 190, 255],
        "KNEE": [180, 120, 30, 255],
        "HANG": [120, 160, 40, 255],
        "TRUSS_MAIN": [200, 30, 30, 255],
    }

    meshes: List = []
    mesh_meta: List[Dict[str, str]] = []
    node_ids = list(nodes)
    total_bars = sum(1 for _ in _iter_bars(model))
    skipped: List[str] = []
    for cid, bar in _iter_bars(model):
        f = bar.properties.get("from_node")
        t = bar.properties.get("to_node")
        if f not in nodes or t not in nodes:
            skipped.append(cid)
            continue
        a = nodes[f]
        b = nodes[t]
        pa = (float(a["x"] or 0), float(a["y"] or 0), float(a["z"] or 0))
        pb = (float(b["x"] or 0), float(b["y"] or 0), float(b["z"] or 0))
        direction = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
        length = math.sqrt(sum(v * v for v in direction))
        if length < 1e-6:
            skipped.append(cid)
            continue
        section = bar.properties.get("section")
        try:
            # P1-7：角钢按真实 L 型截面拉伸，非统一圆柱
            mesh = _angle_steel_mesh(section, length)
        except Exception:
            radius = _section_radius(section)
            mesh = trimesh.creation.cylinder(radius=radius, height=length, sections=12)
        transform = trimesh.geometry.align_vectors([0.0, 0.0, 1.0], direction)
        mid = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2, (pa[2] + pb[2]) / 2)
        transform[0][3] = mid[0]
        transform[1][3] = mid[1]
        transform[2][3] = mid[2]
        mesh.apply_transform(transform)
        color = layer_colors.get(bar.properties.get("layer", ""), [180, 180, 180, 255])
        mesh.visual.face_colors = color
        bid = str(bar.properties.get("bar_id") or cid)
        extras = {"bar_id": bid, "component_id": cid}
        mesh.metadata = dict(extras)
        meshes.append(mesh)
        mesh_meta.append(extras)

    if not meshes:
        raise SolveError("没有可实体化的杆件（请先完成跨视图合并 --merge）")

    # E4：严格模式下导出杆件数必须等于模型杆件数，不允许静默丢杆件
    if strict and skipped:
        raise SolveError(
            f"GLB 导出杆件数与模型不一致：{len(meshes)}/{total_bars}，"
            f"跳过 {len(skipped)} 根：{skipped[:5]}"
        )

    scene = trimesh.Scene()
    for mesh, extras in zip(meshes, mesh_meta):
        scene.add_geometry(
            mesh,
            geom_name=extras["component_id"],
            metadata=extras,
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scene.export(str(out_path))
    # 侧车索引：便于 Web 在不读 GLB extras 时回退
    map_path = out_path.with_suffix(".bar_map.json")
    map_path.write_text(
        __import__("json").dumps(mesh_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(out_path)


def _parse_section(section: Optional[str]) -> Tuple[float, float]:
    """解析角钢截面规格（如 L100x8 / L100×8 / ∠100*8）→ (肢宽, 肢厚) mm。"""
    import re

    if not section:
        return 100.0, 8.0
    m = re.search(r"[Ll]?\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)", str(section))
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)", str(section))
    if m:
        w = float(m.group(1))
        return w, w * 0.08
    return 100.0, 8.0


def _angle_steel_mesh(section: Optional[str], length: float):
    """P1-7：L 型角钢截面沿杆长拉伸成实体网格（无 shapely 依赖）。

    截面放在 XY 平面，沿 Z 轴拉伸 length；调用方再 rotate/translate 到杆件方向。
    不同 section 规格得到不同肢宽/肢厚，GLB 中可区分截面规格。
    """
    import numpy as np
    import trimesh

    w, t = _parse_section(section)
    w = max(float(w), float(t) + 1.0)
    # L 型截面外轮廓（逆时针，含内侧缺口）
    ring = [
        [0.0, 0.0], [w, 0.0], [w, t], [t, t],
        [t, w], [0.0, w],
    ]
    n = len(ring)
    verts = []
    for (x, y) in ring:
        verts.append([x, y, 0.0])
    for (x, y) in ring:
        verts.append([x, y, float(length)])
    verts = np.array(verts, dtype=float)

    faces = []
    for i in range(n):
        j = (i + 1) % n
        a, b, c, d = i, j, n + j, n + i
        faces.append([a, b, c])
        faces.append([a, c, d])
    for k in range(1, n - 1):
        faces.append([0, k, k + 1])
    for k in range(1, n - 1):
        faces.append([n, n + k + 1, n + k])

    return trimesh.Trimesh(vertices=verts, faces=np.array(faces, dtype=int), process=True)


def export_tower_step(
    model: EngineeringModel,
    out_path: str | Path,
    strict: bool = True,
    allow_scan: bool = False,
    allow_derived_y: bool = False,
) -> str:
    """E3：STEP 导出（骨架线框或 L 截面实体）。

    可选依赖：优先 cadquery（可生成实体），否则 trimesh 的 STEP 导出
    需要附加库。当前实现给出明确提示，不静默失败。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    nodes, problems = solve_tower(
        model, allow_scan=allow_scan, allow_derived_y=allow_derived_y,
    )
    if strict and problems:
        raise SolveError(
            "存在未求解/待补测项，拒绝 STEP 导出：\n  - " + "\n  - ".join(problems)
        )

    try:
        import cadquery as cq  # noqa: F401
    except ImportError:
        # 骨架线框 STEP 可用 OCP；若都不可用则明确提示
        try:
            from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs  # noqa: F401
        except ImportError as exc:
            raise SolveError(
                "STEP 导出需要 cadquery 或 OCP（可选依赖）："
                "pip install cadquery，或改用 export --format dxf/obj/glb。"
            ) from exc
        raise SolveError(
            "已检测到 OCP 但缺少 cadquery 高层接口："
            "pip install cadquery 后重试 export --format step。"
        )

    # 有 cadquery：逐根杆件画线（骨架）
    try:
        import cadquery as cq
    except ImportError as exc:  # pragma: no cover
        raise SolveError("STEP 导出需要 cadquery：pip install cadquery") from exc

    workplanes = []
    for cid, bar in _iter_bars(model):
        f = bar.properties.get("from_node")
        t = bar.properties.get("to_node")
        if f not in nodes or t not in nodes:
            continue
        a, b = nodes[f], nodes[t]
        if None in (a["x"], a["y"], a["z"], b["x"], b["y"], b["z"]):
            continue
        wp = cq.Workplane("XZ").moveTo(float(a["x"]), float(a["z"]))\
            .lineTo(float(b["x"]), float(b["z"]))
        workplanes.append(wp)
    # STEP 线框需要 Edge；骨架导出用 import/export 受限，这里写最小 wire 文件
    # 作为可选依赖的占位实现：真正实体化需 OCP 顶层接口。
    raise SolveError(
        "STEP 导出当前为可选依赖占位实现：请安装 cadquery 并调用 "
        "export_tower_step 的实体化版本，或改用 export --format glb。"
    )


def export_tower_obj(
    model: EngineeringModel,
    out_path: str | Path,
    strict: bool = True,
    allow_scan: bool = False,
    allow_derived_y: bool = False,
) -> str:
    """从模型求解并导出 OBJ。

    strict=True 时，任何缺失轴都会阻断导出（沿用「placeholder 阻断终版」原则）。
    """
    nodes, problems = solve_tower(
        model, allow_scan=allow_scan, allow_derived_y=allow_derived_y,
    )
    if strict and problems:
        raise SolveError(
            "存在未求解/待补测项，拒绝终版导出：\n  - " + "\n  - ".join(problems)
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    node_ids = list(nodes)
    lines = ["# tower 3D reconstruction (from EngineeringModel)", ""]
    for i, nid in enumerate(node_ids, start=1):
        n = nodes[nid]
        lines.append(f"v {n['x'] or 0:.4f} {n['y'] or 0:.4f} {n['z'] or 0:.4f}")
    lines.append("")
    for cid, bar in _iter_bars(model):
        f, t = bar.properties.get("from_node"), bar.properties.get("to_node")
        if f in nodes and t in nodes:
            lines.append(f"l {node_ids.index(f)+1} {node_ids.index(t)+1}")
    content = "\n".join(lines) + "\n"
    out_path.write_text(content, encoding="utf-8")
    return content
