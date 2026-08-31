#!/usr/bin/env python3
"""Phase 6.1：新旧模型 diff.glb 生成器（TASK_VIEWER_3D 任务 A）。

对比两个版本的 model.json（默认：冻结基线 vs 当前交付），按几何对齐匹配
物理杆件，输出三色 diff GLB + diff_report.json：

    新增（新模型有、旧模型无）  → 绿色  [40, 200, 40, 255]
    删除（旧模型有、新模型无）  → 红色  [230,  60, 60, 255]
    未变化 / 位置微调 (<tol)    → 灰色  [150, 150, 150, 120]

匹配口径（任务书约定）：
    * 范围：只用 front 面（face='f'）+ diaphragm 物理杆，排除 derived；
    * 同杆判据：两端点 3D 距离（含端点交换取优）最大者 < tol（默认 50mm）；
    * 配对策略：全对候选按 cost 升序贪心 1:1（同 component_id 的 cost≈0
      自然最先入选，等价于「stem 匹配 + 坐标校验」）。

GLB 实体化用 trimesh cylinder（diff 场景不需要 L 截面细节；参考
traceability/solve/tower_solver.py::export_tower_glb 的场景组装方式，
mesh 节点名 = component_id，viewer 按名过滤）。

用法：
    python3 scripts/generate_diff_glb.py [--old PATH] [--new PATH] [--tol 50]
                                         [--out-dir DIR] [--radius 60]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OLD = REPO_ROOT / "out/35A1-JC1-baseline/model.json"
DEFAULT_NEW = REPO_ROOT / "out/35A1-JC1-full-deliver/model.json"
DEFAULT_OUT_DIR = REPO_ROOT / "out/35A1-JC1-full-deliver"

# 任务书钉死的三色（RGBA）
DIFF_COLORS: Dict[str, List[int]] = {
    "added": [40, 200, 40, 255],
    "removed": [230, 60, 60, 255],
    "unchanged": [150, 150, 150, 120],
}

# diff 范围：front 投影物理杆 + 横隔物理杆（排除 derived 展示几何）
DIFF_FACES = ("f", "diaphragm")


# --------------------------------------------------------------------------- #
# 模型装载与 diff 范围筛选
# --------------------------------------------------------------------------- #

def _node_positions(model: dict) -> Dict[str, Tuple[float, float, float]]:
    """tower_node → 3D 坐标（mm）。坐标缺失按 0 处理（与管线导出口径一致）。"""
    pos: Dict[str, Tuple[float, float, float]] = {}
    for cid, c in model.get("components", {}).items():
        if c.get("kind") != "tower_node":
            continue
        p = c.get("properties", {})
        try:
            pos[cid] = (
                float(p.get("x") or 0.0),
                float(p.get("y") or 0.0),
                float(p.get("z") or 0.0),
            )
        except (TypeError, ValueError):
            continue
    return pos


def load_diff_scope_bars(model_path: Path) -> Dict[str, dict]:
    """读取 model.json，返回 diff 范围内的物理杆 {component_id: bar}。

    bar = {a, b: 端点坐标, face, role, origin, bar_id, source_file}
    """
    model = json.loads(Path(model_path).read_text(encoding="utf-8"))
    nodes = _node_positions(model)
    bars: Dict[str, dict] = {}
    for cid, c in model.get("components", {}).items():
        if c.get("kind") != "tower_bar":
            continue
        p = c.get("properties", {})
        if str(p.get("geometry_class")) == "derived":
            continue
        face = str(p.get("face") or "")
        if face not in DIFF_FACES:
            continue
        f, t = p.get("from_node"), p.get("to_node")
        if f not in nodes or t not in nodes:
            continue
        bars[cid] = {
            "a": nodes[f], "b": nodes[t],
            "face": face,
            "role": p.get("role"),
            "origin": p.get("geometry_origin"),
            "bar_id": p.get("bar_id"),
            "source_file": p.get("source_file"),
        }
    return bars


# --------------------------------------------------------------------------- #
# 几何匹配
# --------------------------------------------------------------------------- #

def _endpoint_cost(b1: dict, b2: dict) -> float:
    """两端点最大距离（正/反两种配对取优）。> tol 的候选对会被丢弃。"""
    d1 = max(math.dist(b1["a"], b2["a"]), math.dist(b1["b"], b2["b"]))
    d2 = max(math.dist(b1["a"], b2["b"]), math.dist(b1["b"], b2["a"]))
    return min(d1, d2)


def match_bars(
    old_bars: Dict[str, dict],
    new_bars: Dict[str, dict],
    tol_mm: float = 50.0,
) -> Tuple[List[Tuple[str, str, float]], List[str], List[str]]:
    """cost 升序贪心 1:1 配对。返回 (matched[(old_cid,new_cid,cost)], added, removed)。"""
    old_ids = sorted(old_bars)
    new_ids = sorted(new_bars)
    candidates: List[Tuple[float, str, str]] = []
    for oc in old_ids:
        ob = old_bars[oc]
        for nc in new_ids:
            cost = _endpoint_cost(ob, new_bars[nc])
            if cost <= tol_mm:
                candidates.append((cost, oc, nc))
    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    used_o: set = set()
    used_n: set = set()
    matched: List[Tuple[str, str, float]] = []
    for cost, oc, nc in candidates:
        if oc in used_o or nc in used_n:
            continue
        used_o.add(oc)
        used_n.add(nc)
        matched.append((oc, nc, cost))
    added = [k for k in new_ids if k not in used_n]
    removed = [k for k in old_ids if k not in used_o]
    return matched, added, removed


# --------------------------------------------------------------------------- #
# GLB 实体化
# --------------------------------------------------------------------------- #

def _bar_transform(pa, pb):
    """Z 轴圆柱 → 对齐 pa→pb 方向并平移到中点（4x4 齐次矩阵）。"""
    import numpy as np
    import trimesh.transformations as tf

    d = [pb[i] - pa[i] for i in range(3)]
    length = math.sqrt(sum(v * v for v in d))
    if length < 1e-9:
        return None, 0.0
    dv = np.array(d) / length
    z = np.array([0.0, 0.0, 1.0])
    axis = np.cross(z, dv)
    s = float(np.linalg.norm(axis))
    cosang = float(max(-1.0, min(1.0, float(np.dot(z, dv)))))
    m = np.eye(4)
    if s < 1e-9:
        if cosang < 0:  # 反平行：绕 X 轴翻 180°
            m = np.diag([1.0, -1.0, -1.0, 1.0])
    else:
        m = tf.rotation_matrix(math.acos(cosang), axis / s)
    m = np.array(m, dtype=float)
    m[:3, 3] = [(pa[i] + pb[i]) / 2.0 for i in range(3)]
    return m, length


def build_diff_glb(
    old_bars: Dict[str, dict],
    new_bars: Dict[str, dict],
    matched: List[Tuple[str, str, float]],
    added: List[str],
    removed: List[str],
    out_glb: Path,
    radius_mm: float = 60.0,
) -> int:
    """生成三色 diff.glb，mesh 节点名 = component_id。返回成功实体化的杆数。"""
    import trimesh

    scene = trimesh.Scene()
    n_mesh = 0

    def add(cid: str, bar: dict, color: List[int]) -> None:
        nonlocal n_mesh
        m, length = _bar_transform(bar["a"], bar["b"])
        if m is None:
            return
        cyl = trimesh.creation.cylinder(radius=radius_mm, height=length, sections=8)
        cyl.apply_transform(m)
        cyl.visual.face_colors = list(color)
        scene.add_geometry(cyl, geom_name=str(cid))
        n_mesh += 1

    for _oc, nc, _cost in matched:
        add(nc, new_bars[nc], DIFF_COLORS["unchanged"])
    for cid in added:
        add(cid, new_bars[cid], DIFF_COLORS["added"])
    for cid in removed:
        add(cid, old_bars[cid], DIFF_COLORS["removed"])

    if n_mesh == 0:
        raise SystemExit("diff 范围内没有任何可实体化的杆件（检查 --old/--new 路径）")
    out_glb = Path(out_glb)
    out_glb.parent.mkdir(parents=True, exist_ok=True)
    scene.export(str(out_glb))
    return n_mesh


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def generate_diff(
    old_path: Path,
    new_path: Path,
    tol_mm: float,
    out_dir: Path,
    radius_mm: float = 60.0,
) -> dict:
    """完整 diff：匹配 → diff.glb + diff_report.json。返回 report dict。"""
    old_bars = load_diff_scope_bars(old_path)
    new_bars = load_diff_scope_bars(new_path)
    matched, added, removed = match_bars(old_bars, new_bars, tol_mm)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    glb_path = out_dir / "diff.glb"
    n_mesh = build_diff_glb(
        old_bars, new_bars, matched, added, removed, glb_path, radius_mm)

    moved = [[oc, nc, round(c, 1)] for oc, nc, c in matched if c > 1.0]
    report = {
        "description": "新旧模型物理杆 diff（Phase 6.1；绿=新增/红=删除/灰=未变或微调）",
        "old": str(old_path),
        "new": str(new_path),
        "tol_mm": tol_mm,
        "scope": "face in (f, diaphragm)，排除 geometry_class=derived",
        "n_old_bars": len(old_bars),
        "n_new_bars": len(new_bars),
        "added": list(added),
        "removed": list(removed),
        "unchanged_count": len(matched),
        "moved_within_tol": moved,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "unchanged": len(matched),
        },
        "glb_meshes": n_mesh,
    }
    (out_dir / "diff_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="新旧 model.json → 三色 diff.glb + diff_report.json")
    ap.add_argument("--old", default=str(DEFAULT_OLD), help="旧模型（默认：冻结基线）")
    ap.add_argument("--new", default=str(DEFAULT_NEW), help="新模型（默认：当前交付）")
    ap.add_argument("--tol", type=float, default=50.0, help="端点距离容差 mm（默认 50）")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="输出目录")
    ap.add_argument("--radius", type=float, default=60.0, help="圆柱半径 mm")
    args = ap.parse_args(argv)

    for p in (args.old, args.new):
        if not Path(p).exists():
            print(f"模型不存在：{p}", file=sys.stderr)
            return 2
    report = generate_diff(
        Path(args.old), Path(args.new), args.tol, Path(args.out_dir), args.radius)
    s = report["summary"]
    print(f"diff 完成：新增 {s['added']} | 删除 {s['removed']} | "
          f"未变 {s['unchanged']}（tol={report['tol_mm']}mm，"
          f"旧 {report['n_old_bars']} 杆 / 新 {report['n_new_bars']} 杆）")
    print(f"  → {Path(args.out_dir) / 'diff.glb'}（{report['glb_meshes']} mesh）")
    print(f"  → {Path(args.out_dir) / 'diff_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
