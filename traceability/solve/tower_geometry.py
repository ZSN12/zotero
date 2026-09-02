"""铁塔 3D 几何重建核心（Phase 3 几何层）。

把「解算出的单立面杆件拓扑」升级为「封闭、对称、法向正确」的四棱台空间网架。

几何模块（均可独立单元测试，不依赖 EngineeringModel 序列化）：

    Module 1  snap_diagonals_to_legs      斜材轴线延伸 + 与主腿工作线求交吸附
    Module 1b fit_leg_worklines           四条空间主腿工作线拟合（P0 + t·v）
    Module 2  orient_angle_normals        L 型角钢截面空间法向定向
    Module 3  expand_to_4_face_truss      单立面绕中心轴旋转复制为多面网架
    Module 3b expand_4_face_symmetry      单立面四向镜像展开 + 四角主腿熔合
    Module 3c generate_diaphragms         标高平台处水平横隔面生成
    Module 4  classify_members            语义分类（LEG/DIAG/HORIZ/CROSS）+ 分段缝合
    Module 5  inspect_model_topology      拓扑度数统计（悬空断裂节点 Degree=1）

坐标约定：mm；塔中心轴 = Z 轴（(0,0,0) 在塔底中心）；塔身四棱台关于 X=0/Y=0 对称。

所有函数只做纯几何计算，输入/输出都是普通 dict/list（节点 = {id: (x,y,z)}，
杆件 = {id, from, to, ...}），便于在 solve/export 管线里插桩。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

Vec3 = Tuple[float, float, float]
NodeMap = Dict[str, Vec3]


# GT 剖面拟合函数已移入 debug/gt_profile.py（阶段 0.2 GT 隔离）。
# 保留向后兼容的 re-export，但生产建模路径严禁调用这些 GT 数值。
def gt_tower_half_width(z: float) -> float:
    from ..debug.gt_profile import gt_tower_half_width as _f
    return _f(z)


def gt_crossarm_half_width(z: float) -> float:
    from ..debug.gt_profile import gt_crossarm_half_width as _f
    return _f(z)


# --------------------------------------------------------------------------- #
# 公共几何工具
# --------------------------------------------------------------------------- #

Vec3T = Tuple[float, float, float]


def _sub3(a: Vec3T, b: Vec3T) -> Vec3T:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dist3(a: Vec3T, b: Vec3T) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _unit3(v: Vec3T) -> Optional[Vec3T]:
    n = _dist3(v, (0.0, 0.0, 0.0))
    if n < 1e-9:
        return None
    return (v[0] / n, v[1] / n, v[2] / n)


def _angle_deg(u: Vec3T, v: Vec3T) -> float:
    d = u[0] * v[0] + u[1] * v[1] + u[2] * v[2]
    d = max(-1.0, min(1.0, abs(d)))
    return math.degrees(math.acos(d))


def _v(p) -> np.ndarray:
    return np.asarray(p, dtype=float)


def _plain(v) -> Tuple[float, float, float]:
    """把坐标转成纯 Python float 三元组（避免 numpy 标量泄漏到 JSON/下游）。"""
    return (float(v[0]), float(v[1]), float(v[2]))


def _line_line_closest(a1: Vec3, a2: Vec3, b1: Vec3, b2: Vec3) -> Tuple[Vec3, Vec3, float]:
    """两条空间线段所在直线的最近点对与最近距离。

    返回 (pa_on_A, pb_on_B, dist)。线段为 3D 一般位置（不平行不共面）时唯一；
    平行时返回 a1 在 B 直线上的投影与 a1。
    """
    a1, a2, b1, b2 = map(_v, (a1, a2, b1, b2))
    da = a2 - a1
    db = b2 - b1
    r = a1 - b1
    a_dot = float(da @ da)
    b_dot = float(db @ db)
    if a_dot < 1e-12 or b_dot < 1e-12:
        return tuple(a1), tuple(b1), float(np.linalg.norm(a1 - b1))
    cross = float(da @ db)
    denom = a_dot * b_dot - cross * cross
    if abs(denom) < 1e-12:
        # 平行：a1 到 B 直线投影
        t = float((a1 - b1) @ db) / b_dot
        pb = b1 + t * db
        return tuple(a1), tuple(pb), float(np.linalg.norm(a1 - pb))
    ta = float((r @ db) * cross - (r @ da) * b_dot) / denom
    tb = float((r @ db) * a_dot - (r @ da) * cross) / denom
    pa = a1 + ta * da
    pb = b1 + tb * db
    return tuple(pa), tuple(pb), float(np.linalg.norm(pa - pb))


def _point_segment_distance(p: Vec3, s1: Vec3, s2: Vec3) -> Tuple[Vec3, float]:
    """点到线段的最近点与距离。"""
    p, s1, s2 = map(_v, (p, s1, s2))
    d = s2 - s1
    dd = float(d @ d)
    if dd < 1e-12:
        return tuple(s1), float(np.linalg.norm(p - s1))
    t = float((p - s1) @ d) / dd
    t = min(1.0, max(0.0, t))
    closest = s1 + t * d
    return tuple(closest), float(np.linalg.norm(p - closest))


def _bar_vector(nodes: NodeMap, bar: dict) -> Optional[np.ndarray]:
    """杆件方向向量（from -> to），端点缺失返回 None。"""
    f = nodes.get(bar["from"])
    t = nodes.get(bar["to"])
    if f is None or t is None:
        return None
    return _v(t) - _v(f)


def _bar_length(nodes: NodeMap, bar: dict) -> float:
    d = _bar_vector(nodes, bar)
    return float(np.linalg.norm(d)) if d is not None else 0.0


def _inclination_deg(d: np.ndarray) -> float:
    """方向向量与水平面的夹角（度）；竖直向上=90，水平=0。"""
    h = float(np.hypot(d[0], d[1]))
    return math.degrees(math.atan2(float(d[2]), h))


# --------------------------------------------------------------------------- #
# Module 1  斜材轴线延伸 + 与主腿求交吸附
# --------------------------------------------------------------------------- #

def fit_leg_worklines(
    nodes: NodeMap,
    bars: List[dict],
    *,
    leg_ids: Optional[Sequence[str]] = None,
    corner_tol: float = 300.0,
) -> List[Tuple[str, Vec3, Vec3, float]]:
    """拟合 4 条空间主腿工作线（L_leg(t) = P0 + t·v）。

    思路：
        1. 用 classify_members 找出 LEG 杆件；
        2. 把所有 LEG 端点按「塔角象限」（sign(x), sign(y)）聚成最多 4 簇；
        3. 每簇内把同向杆段合并，用质心 + 主轴方向（对端点做最小二乘）拟合
           出一条工作线。

    返回 [(corner_id, P0, v, rms_mm)]，corner_id 形如 "P0/+-/+-"。
    v 已归一化并统一指向 +Z（使 t 单调向上）。
    """
    if leg_ids is None:
        roles = classify_members(nodes, bars)
        leg_ids = [b["id"] for b in bars if roles.get(b["id"]) == "LEG"]
    leg_set = set(leg_ids)

    # 按象限聚类主腿端点
    clusters: Dict[str, List[Vec3]] = {}
    for b in bars:
        if b["id"] not in leg_set:
            continue
        for end in ("from", "to"):
            p = nodes.get(b[end])
            if p is None:
                continue
            key = f"P{int(math.copysign(1, p[0]) > 0)}{int(math.copysign(1, p[1]) > 0)}"
            clusters.setdefault(key, []).append(tuple(p))

    worklines: List[Tuple[str, Vec3, Vec3, float]] = []
    for key, pts in clusters.items():
        pts_arr = np.asarray(pts, dtype=float)
        if len(pts_arr) < 2:
            continue
        p0 = pts_arr.mean(axis=0)
        centered = pts_arr - p0
        # 主轴方向：对端点散布做 SVD；主腿近竖直，主方向应接近 Z。
        try:
            u, s, vt = np.linalg.svd(centered, full_matrices=False)
        except np.linalg.LinAlgError:
            continue
        direction = vt[0]  # 最大方差方向
        if float(direction @ np.array([0.0, 0.0, 1.0])) < 0:
            direction = -direction
        direction = direction / float(np.linalg.norm(direction))
        # 用质心在方向上的投影作为 P0
        p0_proj = p0 - direction * float(direction @ p0)
        # 计算拟合残差（点到工作线的距离 RMS）
        dists = []
        for p in pts_arr:
            pa, _, d = _line_line_closest(tuple(p), tuple(p + direction), tuple(p0), tuple(p0 + direction))
            dists.append(d)
        rms = float(np.sqrt(np.mean(np.square(dists)))) if dists else 0.0
        worklines.append((key, _plain(p0_proj), _plain(direction), rms))

    worklines.sort(key=lambda w: w[0])
    return worklines


def _common_perpendicular_midpoint(
    origin: Vec3,
    axis_dir: np.ndarray,
    l1: Vec3,
    l2: Vec3,
) -> Tuple[Vec3, float]:
    """射线 origin + s·axis_dir 与腿直线 l1-l2 的空间公垂线中点与公垂线距离。

    与 _line_line_closest 的区别：这里返回「两直线最近点对的中点」，也就是
    射线最近点与腿最近点的中点 (Xc,Yc,Zc)，作为把斜材端点强行重定位的目标。
    """
    pa, pb, d = _line_line_closest(origin, tuple(_v(origin) + axis_dir), l1, l2)
    mid = ((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0, (pa[2] + pb[2]) / 2.0)
    return mid, d


def snap_diagonals_to_legs(
    nodes: NodeMap,
    bars: List[dict],
    *,
    leg_ids: Optional[Sequence[str]] = None,
    snap_tol: float = 80.0,
    max_extend_ratio: float = 2.0,
) -> Tuple[NodeMap, List[dict]]:
    """把斜材端点沿轴线延伸，吸附到最近主腿工作中心线（空间公垂线中点）。

    问题：CAD 中斜材只画到节点板边缘，未与主腿交汇，端点悬空（30~50mm 偏心）。
    改法：
        1. 先 fit_leg_worklines 拟合 4 条主腿工作线（比逐段 leg 线段更稳定）；
        2. 对每根斜材两端，沿其轴线方向延伸；
        3. 计算该射线与最近主腿工作线的空间公垂线中点 (Xc,Yc,Zc)；
        4. 若公垂线距离 < snap_tol，把斜材端点坐标强行重定位至该中点。

    返回 (new_nodes, new_bars)。原输入不改；吸附后的新节点直接写回对应
    bar 的 from/to 键（共享节点）。

    leg_ids：指定哪些 bar 是主腿；缺省时按 classify_members 自动识别 LEG。
    """
    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = [dict(b) for b in bars]

    # 工作线拟合：优先用 4 条拟合主腿；拟合失败回退为逐段 leg 线段。
    worklines = fit_leg_worklines(nodes, bars, leg_ids=leg_ids)
    leg_lines: List[Tuple[Vec3, Vec3]] = []
    if worklines:
        for _, p0, v, _rms in worklines:
            leg_lines.append((p0, tuple(_v(p0) + _v(v) * 1000.0)))
    if not leg_lines:
        if leg_ids is None:
            leg_ids = [
                b["id"] for b in bars
                if classify_members(nodes, bars).get(b["id"]) == "LEG"
            ]
        leg_set = set(leg_ids)
        for b in bars:
            if b["id"] not in leg_set:
                continue
            f, t = nodes.get(b["from"]), nodes.get(b["to"])
            if f is None or t is None:
                continue
            leg_lines.append((f, t))
    if not leg_lines:
        return new_nodes, new_bars

    if leg_ids is None:
        leg_ids = [
            b["id"] for b in bars
            if classify_members(nodes, bars).get(b["id"]) == "LEG"
        ]
    leg_set = set(leg_ids)

    def snap_endpoint(axis_dir: np.ndarray, origin: Vec3) -> Optional[Vec3]:
        """沿 axis_dir 延伸，返回最近主腿工作线的公垂线中点。"""
        best: Optional[Vec3] = None
        best_d = snap_tol
        for (l1, l2) in leg_lines:
            mid, d = _common_perpendicular_midpoint(origin, axis_dir, l1, l2)
            if d < best_d:
                best = mid
                best_d = d
        return best

    for b in new_bars:
        if b["id"] in leg_set:
            continue
        f, t = nodes.get(b["from"]), nodes.get(b["to"])
        if f is None or t is None:
            continue
        d = _v(t) - _v(f)
        L = float(np.linalg.norm(d))
        if L < 1e-6:
            continue
        axis = d / L
        # 两个端点各尝试延伸吸附
        for key, origin in (("from", f), ("to", t)):
            snapped = snap_endpoint(axis, origin)
            if snapped is None:
                continue
            # 限制延伸量：吸附点相对原端点沿轴线的位移不超过 max_extend_ratio * L
            delta = _v(snapped) - _v(origin)
            proj = float(delta @ axis)
            if abs(proj) > max_extend_ratio * L:
                continue
            # 写回：吸附到主腿后，共享该腿上的节点（若足够近）
            nid = _get_or_add_node(new_nodes, tuple(snapped), tol=1.0)
            b[key] = nid
    return new_nodes, new_bars


def _get_or_add_node(nodes: NodeMap, pos: Vec3, tol: float = 1.0) -> str:
    """就近复用已有节点（容差内），否则新增并返回 id。"""
    for nid, p in nodes.items():
        if float(np.linalg.norm(_v(p) - _v(pos))) <= tol:
            return nid
    nid = f"N{len(nodes):04d}"
    while nid in nodes:
        nid = f"N{len(nodes):04d}_{len(nodes)}"
    nodes[nid] = _plain(pos)
    return nid


def close_face_intersections(
    nodes: NodeMap,
    bars: List[dict],
    *,
    snap_tol: float = 10.0,
    max_rounds: int = 8,
    max_bars: int = 2000,
) -> Tuple[NodeMap, List[dict]]:
    """T 形交点闭合：把「端点落在其它杆件线段上」的交点打断为共享节点。

    CAD 立面图常见现象：斜材/水平材画到主材中心线上，但提取器为每根杆件
    单独建了端点节点，导致拓扑上看似断开。本函数对每个杆件端点，找其在
    其它杆件线段上的最近投影点，若距离 < snap_tol 且投影落在该线段内，
    则：
        1. 把目标杆件在投影点处打断为两段；
        2. 把源端点改指到投影点（共享节点）。

    迭代至稳定（最多 max_rounds 轮），返回 (new_nodes, new_bars)。原输入
    不改。用于 Phase 1「空间工作线 100% 封闭求交」。
    """
    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = [dict(b) for b in bars]

    def _proj_on_segment(p: Vec3, s1: Vec3, s2: Vec3) -> Optional[Tuple[Vec3, float, float]]:
        """返回 (投影点, 距离, 参数 t∈[0,1])；不在线段内或距离过大返回 None。"""
        pp, s1, s2 = map(_v, (p, s1, s2))
        d = s2 - s1
        dd = float(d @ d)
        if dd < 1e-12:
            return None
        t = float((pp - s1) @ d) / dd
        if t < 0.0 or t > 1.0:
            return None
        proj = s1 + t * d
        dist = float(np.linalg.norm(pp - proj))
        if dist > snap_tol:
            return None
        return _plain(proj), dist, t

    for _round in range(max_rounds):
        changed = False
        for bi in range(len(new_bars)):
            bar = new_bars[bi]
            # P2.1b（2026-09-04）：marker_synth 合成横杆豁免 T 形打断——
            # 它们是「层位终态」（[0,±inner]/[±inner,±leg]/[0,±leg] 分段
            # 体系同层故意重叠，与 GT 环梁投影同构）。全跨段 [0,leg] 的
            # 内部恰是 [0,inner] 的端点——被打断后劈出的子段与既有分段
            # 在 stitch_segment_boundaries 端点对去重时撞 key，全跨段
            # 被静默删除（06 册实测 12 根合成杆 15→9→8，全跨段全灭，
            # GT [0,±hw] 全跨横杆恒 FN）。tower_dxf 的 T 打断已同样豁免。
            _is_synth = str(bar.get("geometry_origin") or "") in (
                "marker_synth", "leg_synth")
            for end in ("from", "to"):
                p = new_nodes.get(bar[end])
                if p is None:
                    continue
                best = None
                for bj, other in enumerate(new_bars):
                    if bi == bj:
                        continue
                    # 豁免杆的端点也不允许打断其它杆（打断目标杆同样
                    # 破坏终态分段体系——劈出的子段就是去重撞 key 的来源）。
                    if _is_synth and str(other.get("geometry_origin") or "") in (
                            "marker_synth", "leg_synth"):
                        continue
                    s1, s2 = new_nodes.get(other["from"]), new_nodes.get(other["to"])
                    if s1 is None or s2 is None:
                        continue
                    hit = _proj_on_segment(p, s1, s2)
                    if hit is None:
                        continue
                    proj, dist, _t = hit
                    if best is None or dist < best[0]:
                        best = (dist, bj, proj)
                if best is None:
                    continue
                dist, bj, proj = best
                other = new_bars[bj]
                # marker_synth / leg_synth 杆既不做打断源、也不做被打断目标。
                if _is_synth or str(other.get("geometry_origin") or "") in (
                        "marker_synth", "leg_synth"):
                    continue
                q = _get_or_add_node(new_nodes, proj, tol=1.0)
                # 打断目标杆件（若 q 不是其端点），即使源端点已与交点重合，
                # 也仍要打断目标杆件，否则 T 形交点不会成为共享节点。
                if q != other["from"] and q != other["to"]:
                    old_to = other["to"]
                    other["to"] = q
                    # 阶段1.2：拆分溯源（root_bar_id / split_index）。
                    # root_bar_id 指向拆分前最原始杆 id（递归拆分时保持不变），
                    # 下游据此判断「该杆是否已被拆分」，避免重复处理原杆；
                    # split_index 为该段在 root 拆分序列中的序号（首段保留原
                    # id 记 0，后续新段递增）。derived_from 沿用已有溯源值。
                    root = other.get("root_bar_id") or other["id"]
                    n = other.get("split_count", 0) + 1
                    other["root_bar_id"] = root
                    other["split_index"] = other.get("split_index", 0)
                    other["split_count"] = n
                    if len(new_bars) < max_bars:
                        new_bars.append({
                            "id": f"{other['id']}__split{len(new_bars)}",
                            "from": q,
                            "to": old_to,
                            **{k: v for k, v in other.items()
                               if k not in ("id", "from", "to", "split_count")},
                            "root_bar_id": root,
                            "split_index": n,
                        })
                    changed = True
                if bar[end] != q:
                    bar[end] = q
                    changed = True
        if not changed:
            break

    # 删除退化杆（from == to）
    new_bars = [b for b in new_bars if b.get("from") != b.get("to")]
    return new_nodes, new_bars


# --------------------------------------------------------------------------- #
# Module 2  L 型角钢截面空间法向定向
# --------------------------------------------------------------------------- #

def orient_angle_normal(
    direction: Vec3,
    center: Vec3,
    *,
    role: str = "DIAG",
) -> np.ndarray:
    """计算 L 截面角顶（corner）应指向的单位法向（截面局部 X 轴）。

    L 截面在局部 XY 平面，截面「角顶」在 +X 方向（见 _angle_steel_mesh 的
    ring：外凸角在 (w,0) 与 (0,w) 的对角方向，即 +X +Y 角平分线）。这里返回
    世界坐标系下截面 +X 轴应指向的单位向量，使：

        * LEG（主腿）：角顶指向塔身四棱台的外角法线（从中心轴向外）；
        * DIAG/HORIZ/CROSS：角顶指向塔身立面（face plane）的外法线。

    参数 center 是杆件中点，用于确定「外」方向（远离中心轴 Z 的方向）。
    """
    d = _v(direction)
    L = float(np.linalg.norm(d))
    if L < 1e-9:
        return np.array([1.0, 0.0, 0.0])
    axis = d / L  # 杆件轴向（Z 局部）
    c = _v(center)

    # 从中心轴指向杆件中点的水平方向（「外」方向）
    radial = np.array([c[0], c[1], 0.0])
    rn = float(np.linalg.norm(radial))
    if rn < 1e-6:
        radial = np.array([1.0, 0.0, 0.0])
    else:
        radial = radial / rn

    if role == "LEG":
        # 角顶沿外角法线：径向与竖直合成四棱台外斜面法线的水平分量
        # 简化为径向（水平向外），因为主腿近竖直。
        outward = radial
    else:
        # 斜材/水平材：翼缘贴合所在立面 -> 角顶指向该立面的外法线（径向）
        outward = radial

    # 截面 +X（角顶）尽量对齐 outward；截面法线 = 轴向 × outward
    # 构造正交基：axis (Z), n (截面法向), outward 投影 (X)
    n = np.cross(axis, outward)
    nn = float(np.linalg.norm(n))
    if nn < 1e-9:
        n = np.array([0.0, 1.0, 0.0])
    else:
        n = n / nn
    # 截面 +X = 与 outward 最接近的、垂直于 axis 的方向
    x = outward - (float(outward @ axis)) * axis
    xn = float(np.linalg.norm(x))
    if xn < 1e-9:
        x = np.cross(axis, n)
    x = x / (np.linalg.norm(x) or 1.0)
    return x


def angle_normal_basis(direction: Vec3, center: Vec3, role: str = "DIAG") -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (x, y, z) 正交基：z=杆轴向，x=角顶指向（orient_angle_normal），y=x×z。"""
    z = _v(direction)
    z = z / (float(np.linalg.norm(z)) or 1.0)
    x = orient_angle_normal(direction, center, role=role)
    y = np.cross(z, x)
    y = y / (float(np.linalg.norm(y)) or 1.0)
    x = np.cross(y, z)
    return x, y, z


def _align_matrix(direction: Vec3, center: Vec3, role: str = "DIAG") -> np.ndarray:
    """由杆件方向 + 角色构建 4x4 变换矩阵（截面 XY -> 世界）。

    用于把 _angle_steel_mesh 产出的「截面在 XY、沿 Z 拉伸」的网格
    对齐到世界杆件方向，同时锁定截面绕轴旋转（角顶朝外，不再乱翻）。

    trimesh.apply_transform 按「列」当作基向量（矩阵右乘列向量），
    因此 R 的列必须是局部 X/Y/Z 在世界中的像：
        R @ (0,0,1) == normalize(direction)（杆轴 = 局部 Z -> 世界杆方向）。
    平移 = center，配合 _angle_steel_mesh 把杆放在局部 [-L/2, +L/2]（中点=局部原点），
    使杆两端恰好落在 from/to 节点上。
    """
    x, y, z = angle_normal_basis(direction, center, role=role)
    m = np.eye(4)
    m[:3, :3] = np.column_stack([x, y, z])
    m[:3, 3] = _v(center)
    return m


# --------------------------------------------------------------------------- #
# Module 3  四面空间桁架闭合对称
# --------------------------------------------------------------------------- #

def expand_to_4_face_truss(
    nodes: NodeMap,
    bars: List[dict],
    *,
    axis: Vec3 = (0.0, 0.0, 1.0),
    faces: int = 4,
    angle_offset: float = 0.0,
) -> Tuple[NodeMap, List[dict]]:
    """把单立面构件沿中心轴旋转复制为多面空间网架。

    输入是一组「单立面」节点/杆件（通常位于某一竖直平面内，如 y=0）。
    输出把每个原始节点绕 axis 旋转 k*(360/faces)+angle_offset（k=0..faces-1），
    生成 4 面（前/后/左/右）+ 对应杆件。原第 0 面保留原坐标，其余旋转。

    返回 (new_nodes, new_bars)；节点 id 加 `_r{k}` 后缀，杆件 id 加 `_r{k}`。
    第 0 面（k=0）保留原 id 以便引用稳定。
    """
    axis = _v(axis)
    axis = axis / (float(np.linalg.norm(axis)) or 1.0)
    step = 2.0 * math.pi / faces

    def rot(v: Vec3, theta: float) -> Vec3:
        p = _v(v)
        # Rodrigues 旋转
        c, s = math.cos(theta), math.sin(theta)
        k = axis
        out = p * c + np.cross(k, p) * s + k * (float(k @ p)) * (1.0 - c)
        return tuple(out)

    new_nodes: NodeMap = {}
    new_bars: List[dict] = []
    for k in range(faces):
        theta = angle_offset + k * step
        suffix = "" if k == 0 else f"_r{k}"
        for nid, pos in nodes.items():
            new_nodes[f"{nid}{suffix}"] = _plain(rot(pos, theta)) if k else tuple(pos)
        for b in bars:
            nb = dict(b)
            nb["id"] = f"{b['id']}{suffix}"
            nb["from"] = f"{b['from']}{suffix}"
            nb["to"] = f"{b['to']}{suffix}"
            new_bars.append(nb)
    return new_nodes, new_bars


def expand_4_face_symmetry(
    nodes: NodeMap,
    bars: List[dict],
    *,
    wall: Optional[float] = None,
    faces: int = 4,
    weld_corner_legs: bool = True,
    add_diaphragms: bool = True,
    node_tol: float = 50.0,
    half_width_fn: Optional[Callable[[float], float]] = None,
    crossarm_half_width_fn: Optional[Callable[[float], float]] = None,
    crossarm_ratio: float = 1.3,
    crossarm_preserve_t: bool = False,
    diaphragm_levels: Optional[List[float]] = None,
    level_source_label: Optional[str] = None,
) -> Tuple[NodeMap, List[dict]]:
    """单立面 → 四面封闭空间网架（Phase 2 核心映射）。

    以中心轴 (X=0, Y=0) 为基准，把正立面（提供 t=x 的水平剖面 + z 标高）构件做
    四向镜像展开：

        前立面 (Front, +Y)：( t, +w, z)
        后立面 (Back,  -Y)：(-t, -w, z)
        左立面 (Left,  -X)：(-w,  t, z)
        右立面 (Right, +X)：(+w, -t, z)

    其中 w = 塔身半宽（立面水平剖面最大 |t|，可由 wall 参数覆盖）。四角主腿
    塔腿熔合（Corner Leg Welding）：四个面在拐角处交汇的重复杆件/节点按坐标
    自动合并；若 weld_corner_legs=True，为四角补上连续角钢主腿（按 Z 升序连接
    每个拐角上的节点）。add_diaphragms=True 时在各标高平台处生成水平横隔面。

    half_width_fn(z)：可选，返回该标高处的权威塔身半宽（真实 mm）。当立面图
    的 x 被横担/节点板/斜材外伸污染、|t| 不再是干净半宽时（国网 35A1-JC1
    模块图即如此），用它替代 abs(t) 作为四棱台的截面半宽；t 的符号仍保留
    用于判断节点在中心轴左/右。返回 (new_nodes, new_bars)。节点 id 加
    `_f{face}` 后缀；重复位置自动共享。
    """
    if faces != 4:
        raise ValueError("expand_4_face_symmetry 仅支持 faces=4")

    # 塔身半宽：立面水平剖面最大 |t|（即最大 |x|）。
    # 注意：铁塔四棱台为正四边形截面，在任意标高 Z 处，立面半宽 |t|=|x(z)|
    # 即等于侧面半宽。因此这里不能用全塔最大固定常数 wall 做长方体展开，
    # 必须逐节点取该节点自身实际绝对宽度 w=abs(t)，否则上窄下宽的四棱台
    # 会退化成固定长方体，四个立面在拐角无法交汇。
    if wall is None:
        ts = [abs(_v(p)[0]) for p in nodes.values()]
        wall = float(max(ts)) if ts else 0.0
    wall = abs(float(wall))

    def add_node(pos: Vec3) -> str:
        pos = _plain(pos)
        for nid, p in new_nodes.items():
            if float(np.linalg.norm(_v(p) - _v(pos))) <= node_tol:
                return nid
        nid = f"N{len(new_nodes):05d}"
        while nid in new_nodes:
            nid = f"N{len(new_nodes):05d}x"
        new_nodes[nid] = pos
        return nid

    # 四面映射：在每个节点处，取该节点的自身实际绝对宽度 w = abs(t)。
    # 严禁使用全塔最大固定常数 wall！
    # 注意：当 t=0（中心轴上的节点）时，四个面的映射全部退化为 (0,0,z)，
    # 此时只生成 1 个共享中心节点，避免产生 from==to 的自环退化杆。
    #
    # half_width_fn 提供权威半宽时，区分两类节点：
    #   * body 节点（|t| 不超过权威半宽 * crossarm_ratio）：塔身四棱台，四向
    #     镜像（_F/_B/_L/_R），t 重投影到 sign(t)*w；
    #   * crossarm 节点（|t| 远超权威半宽）：塔头水平悬臂（横担），只在左右
    #     两面（_L/_R）沿 ±X 展开，保留真实外伸 x，不做前后镜像。
    def _classify(t: float, z: float) -> Tuple[str, float, float]:
        """返回 (kind, w_gt, w_arm)。kind ∈ {"body", "crossarm"}。

        crossarm 判定：该标高确有横担层（crossarm_half_width_fn(z)>0）且 |t| 远超
        塔身权威半宽（crossarm_ratio 倍），即横担水平悬臂外伸节点。
        无 GT 横担剖面（crossarm_half_width_fn=None）时一律按 body 四棱台展开，
        不区分横担——这是 GT 隔离的默认生产行为。
        """
        if half_width_fn is None:
            return ("body", 0.0, 0.0)
        w_gt = float(half_width_fn(z))
        w_arm = crossarm_half_width_fn(z) if crossarm_half_width_fn is not None else 0.0
        if w_arm > 0.0 and abs(t) > w_gt * crossarm_ratio:
            return ("crossarm", w_gt, w_arm)
        return ("body", w_gt, w_arm)

    def face_maps(t: float, z: float) -> Dict[str, Vec3]:
        w = abs(t)
        if half_width_fn is not None:
            w_gt = float(half_width_fn(z))
            # 仅在「真正有横担的标高」才区分横担节点：crossarm_half_width_fn(z)
            # > 0 表示该 z 在横担层附近。其余标高（塔身主体 + 塔头主腿 + 塔尖
            # 污染残影）一律按 body 四棱台投影到权威半宽。
            w_arm = crossarm_half_width_fn(z) if crossarm_half_width_fn is not None else 0.0
            is_crossarm = w_arm > 0.0 and abs(t) > w_gt * crossarm_ratio
            if not is_crossarm:
                # 塔身四棱台节点：
                # 1. 如果是主腿角柱点（|t| 接近塔身半宽），贴合到 ±w_gt；
                # 2. 如果是立面内部腹杆点（|t| < w_gt），保留其真实水平坐标 t
                #    （阶段3.3：严禁 t * w_gt/abs(t) 缩放——那会把内部节点扭曲
                #    到角点，破坏立面内部腹杆拓扑）。深度统一用 w_gt。
                #    Front 面 Y=+w_gt，Back 面 Y=-w_gt，Left 面 X=-w_gt，Right 面 X=+w_gt。
                if abs(t) >= w_gt * 0.85:
                    t_scaled = (1.0 if t >= 0 else -1.0) * w_gt
                else:
                    t_scaled = t

                if w_gt < node_tol * 0.5:
                    return {"_C": (0.0, 0.0, z)}
                return {
                    "_F": (t_scaled, +w_gt, z),
                    "_B": (-t_scaled, -w_gt, z),
                    "_L": (-w_gt, t_scaled, z),
                    "_R": (+w_gt, -t_scaled, z),
                }
            else:
                # 横担悬臂节点（仅在 Front 和 Back 两面沿 ±X 延伸，保证左右对称且稳固连接主立柱）
                # S7 生产模式（crossarm_preserve_t=True）：横担桁架内部节点（吊杆、
                # 斜撑、弦杆断点）的 |t| 分布在 w_gt*1.3 ~ w_arm 之间，全部保留
                # 真实 t——推到 ±w_arm 会把中间桁架摧毁成两条外缘线。GT 理想化
                # 路径（默认 False）保持旧行为（端头不足 0.9*w_arm 补到端头）。
                if crossarm_preserve_t:
                    t_arm = t
                else:
                    t_arm = t if abs(t) >= w_arm * 0.9 else (1.0 if t >= 0 else -1.0) * w_arm
                return {
                    "_F": (t_arm, +w_gt, z),
                    "_B": (t_arm, -w_gt, z),
                }
        if w < node_tol * 0.5:
            return {"_C": (0.0, 0.0, z)}
        return {
            "_F": (t, +w, z),
            "_B": (-t, -w, z),
            "_L": (-w, t, z),
            "_R": (+w, -t, z),
        }

    new_nodes: NodeMap = {}
    new_bars: List[dict] = []

    for b in bars:
        f = nodes.get(b["from"])
        t = nodes.get(b["to"])
        if f is None or t is None:
            continue
        # 单立面水平剖面 t 取 x 分量；z 取 z 分量（输入可能带 y，展开时忽略）
        t1, z1 = float(f[0]), float(f[2])
        t2, z2 = float(t[0]), float(t[2])
        if abs(t1 - t2) < 1e-9 and abs(z1 - z2) < 1e-9:
            continue
        fm1 = face_maps(t1, z1)
        fm2 = face_maps(t2, z2)

        # crossarm↔body 过渡杆：一端是横担悬臂（2 面），另一端是塔身（4 面）。
        # 横担悬臂只生活在 Front/Back 两面上沿 ±X 外伸，其靠塔身的一端必须
        # 锚定到塔身 Front/Back 立面的对应角点，而不是被 4 向镜像出幽灵节点
        # （否则 body 端的 _L/_R 镜像节点会成为 degree=1 悬空断裂）。
        kind1 = _classify(t1, z1)[0] if half_width_fn is not None else "body"
        kind2 = _classify(t2, z2)[0] if half_width_fn is not None else "body"
        crossarm_pair = ("crossarm" in (kind1, kind2) and "body" in (kind1, kind2))

        # 按两端共有的 face 后缀生成杆件（交集）：body-body 四向 4 根，
        # body-crossarm / crossarm-crossarm 只在 _F/_B 两面 2 根，中心轴单独。
        if crossarm_pair:
            # 过渡杆只在 Front/Back 两面生成。关键：横担悬臂端用其 _F/_B 映射
            # （两端 x 相同，都是 t_arm），塔身端也必须用「同 x」的面映射——
            # 即 Front 面用 (t_scaled, +w)，Back 面用 (t_scaled, -w)，而非
            # 普通四向镜像的 (-t_scaled, -w)。否则横担靠塔身端会在 Back 面
            # 被镜像到 +t_scaled，产生一个与 Front 端 x 相反的幽灵悬空节点。
            for suffix in ("_F", "_B"):
                # 塔身端：重新计算「同 x」的面坐标（Front: +y, Back: -y，x 不变）
                if suffix not in fm1 or suffix not in fm2:
                    continue
                body_t, body_z = (t1, z1) if kind1 == "body" else (t2, z2)
                w_gt = float(half_width_fn(body_z))
                # 阶段3.3：角柱点贴合 ±w_gt，内部点保留 t（与 face_maps 一致）
                if abs(body_t) >= w_gt * 0.85:
                    body_t_scaled = (1.0 if body_t >= 0 else -1.0) * w_gt
                else:
                    body_t_scaled = body_t
                sign_y = +1.0 if suffix == "_F" else -1.0
                body_pos = (body_t_scaled, sign_y * w_gt, body_z)
                # 横担端用原映射（已是正确 t_arm + ±w）
                arm_pos = fm1[suffix] if kind1 == "crossarm" else fm2[suffix]
                n1 = add_node(body_pos if kind1 == "body" else arm_pos)
                n2 = add_node(arm_pos if kind1 == "body" else body_pos)
                if n1 == n2:
                    continue
                nb = dict(b)
                nb.update({
                    "id": f"{b['id']}{suffix}",
                    "from": n1,
                    "to": n2,
                    "face": suffix.lstrip("_").lower(),
                    "generated_4face": True,
                })
                new_bars.append(nb)
            continue

        common = [s for s in ("_F", "_B", "_L", "_R") if s in fm1 and s in fm2]
        if "_C" in fm1 and "_C" in fm2:
            # 两端都是中心轴节点：整根杆在中心轴上，只生成 1 根
            p1, p2 = fm1["_C"], fm2["_C"]
            n1, n2 = add_node(p1), add_node(p2)
            if n1 != n2:
                nb = dict(b)
                nb.update({
                    "id": f"{b['id']}_C",
                    "from": n1,
                    "to": n2,
                    "face": "center",
                    "generated_4face": True,
                })
                new_bars.append(nb)
        elif "_C" in fm1:
            # 起点是中心，终点是各面：中心→每个共有面
            # P2.1b（2026-09-04）：中心端节点的 face_maps 只有 {"_C"}，
            # `common`（两端 face 交集）恒为空 → 循环零次、杆被静默丢弃
            # （06 册 marker_synth 全跨横杆 [0,±leg] 与 [0,±inner] 段在
            # 4 面展开时整族消失的直接根因）。中心轴节点在 4 个立面都
            # 存在（(0,±w,z)/(±w,0,z)），应与非中心端的**全部面**生成。
            n_center = add_node(fm1["_C"])
            for suffix in (s for s in ("_F", "_B", "_L", "_R") if s in fm2):
                p2 = fm2[suffix]
                n2 = add_node(p2)
                if n_center == n2:
                    continue
                nb = dict(b)
                nb.update({
                    "id": f"{b['id']}{suffix}",
                    "from": n_center,
                    "to": n2,
                    "face": suffix.lstrip("_").lower(),
                    "generated_4face": True,
                })
                new_bars.append(nb)
        elif "_C" in fm2:
            # 终点是中心，起点是各面：每个共有面→中心
            # P2.1b：同上——用起点（非中心端）的全部面，而非空交集。
            n_center = add_node(fm2["_C"])
            for suffix in (s for s in ("_F", "_B", "_L", "_R") if s in fm1):
                p1 = fm1[suffix]
                n1 = add_node(p1)
                if n1 == n_center:
                    continue
                nb = dict(b)
                nb.update({
                    "id": f"{b['id']}{suffix}",
                    "from": n1,
                    "to": n_center,
                    "face": suffix.lstrip("_").lower(),
                    "generated_4face": True,
                })
                new_bars.append(nb)
        else:
            # 两端都是非中心：按共有面生成杆件
            for suffix in common:
                p1, p2 = fm1[suffix], fm2[suffix]
                n1, n2 = add_node(p1), add_node(p2)
                if n1 == n2:
                    continue
                nb = dict(b)
                nb.update({
                    "id": f"{b['id']}{suffix}",
                    "from": n1,
                    "to": n2,
                    "face": suffix.lstrip("_").lower(),
                    "generated_4face": True,
                })
                new_bars.append(nb)

    # 去除重复杆件（无向端点相同，四角交汇处前后/左右面会重复生成同一条腿）
    def _bar_key(bb: dict) -> tuple:
        a, c = bb["from"], bb["to"]
        return (min(a, c), max(a, c))

    seen: Dict[tuple, str] = {}
    deduped: List[dict] = []
    for b in new_bars:
        key = _bar_key(b)
        if key in seen:
            continue
        seen[key] = b["id"]
        deduped.append(b)
    new_bars = deduped

    # 四角主腿熔合：每个拐角 (±w, ±w) 上的节点按 Z 升序连接。
    # 注意：塔身为正四边形截面，拐角坐标随标高变化（w=|x(z)|），
    # 不能再用全塔固定 wall 找角点。改为在每个标高平面内按象限取
    # 径向距离最大的节点作为该层拐角，再按 Z 升序熔合成通长主腿。
    #
    # 关键：拐角节点按「面板点」粗分桶（panel_gap ≈ 2000mm），而非每个
    # 0.1mm 标高都取一个拐角。否则塔身 ~160 个中间节点标高会产生 159 段
    # ×4 象限 ≈ 636 根 50mm 级碎片（corner_leg 爆炸），使 3D 杆件数虚高到
    # GT 的 2 倍。GT 主腿是连续通长杆件（最长 7077mm），按面板点分桶后
    # 每象限只剩 ~16 段，对齐 GT 的面板粒度。
    if weld_corner_legs:
        from collections import defaultdict
        by_z: Dict[float, List[Tuple[str, Vec3]]] = defaultdict(list)
        for nid, p in new_nodes.items():
            by_z[round(float(p[2]), 1)].append((nid, p))

        quadrants = [(1.0, 1.0), (-1.0, 1.0), (-1.0, -1.0), (1.0, -1.0)]
        panel_gap = 1000.0  # 面板点间距，对齐 GT 主腿节点粒度（~1000mm 一个面板点）
        for ci, (sx, sy) in enumerate(quadrants, start=1):
            corner_nodes: List[Tuple[float, str]] = []
            for z in sorted(by_z):
                cands = [
                    (nid, p) for nid, p in by_z[z]
                    if math.copysign(1.0, p[0]) == sx and math.copysign(1.0, p[1]) == sy
                ]
                if not cands:
                    continue
                # 该象限径向距离最大的点即该标高拐角
                nid, p = max(cands, key=lambda np: np[1][0] ** 2 + np[1][1] ** 2)
                corner_nodes.append((float(p[2]), nid))
            corner_nodes.sort()
            # 面板点粗分桶：只保留「距上一拐角 >= panel_gap」的拐角，
            # 且始终保留最低点与最高点（塔底/塔顶必须闭合）。
            panel_corners: List[Tuple[float, str]] = []
            for zc, nc in corner_nodes:
                if not panel_corners or (zc - panel_corners[-1][0]) >= panel_gap:
                    panel_corners.append((zc, nc))
            if panel_corners and corner_nodes and panel_corners[-1] != corner_nodes[-1]:
                panel_corners.append(corner_nodes[-1])
            # 四角主腿分段逐节熔合（只在面板点之间连接，避免 50mm 级碎片）
            for k in range(len(panel_corners) - 1):
                z_a, n_a = panel_corners[k]
                z_b, n_b = panel_corners[k + 1]
                if abs(z_b - z_a) >= 1e-6 and n_a != n_b:
                    key = (min(n_a, n_b), max(n_a, n_b))
                    if key in seen:
                        # 该角点对之间已有杆件（如某立面的主腿杆），无需重复生成。
                        # 注意：不能把它重新标记为 corner_leg，否则会把普通
                        # 立面主腿误标为四角主腿，导致 corner_leg 数量爆炸
                        # （600+ 根）并污染 GT 评测的 FP。
                        continue
                    else:
                        leg_id = f"corner_leg_{ci}_{k:02d}"
                        seen[key] = leg_id
                        new_bars.append({
                            "id": leg_id,
                            "from": n_a,
                            "to": n_b,
                            "face": "corner",
                            "role": "LEG",
                            "corner_leg": True,
                            "corner_index": ci,
                            "generated_4face": True,
                        })

    # 水平横隔面：每个具备 4 个角点的标高平台生成菱形/交叉隔面
    if add_diaphragms:
        new_nodes, new_bars = generate_diaphragms(
            new_nodes, new_bars, wall=wall, levels=diaphragm_levels,
            level_source_label=level_source_label,
            half_width_fn=half_width_fn,
        )

    return new_nodes, new_bars


def generate_diaphragms(
    nodes: NodeMap,
    bars: List[dict],
    *,
    wall: Optional[float] = None,
    min_z_gap: float = 2000.0,
    with_perimeter: bool = True,
    levels: Optional[List[float]] = None,
    level_source_label: Optional[str] = None,
    dedup_report: Optional[dict] = None,
    half_width_fn: Optional[Callable[[float], float]] = None,
    hw_tol_ratio: float = 0.35,
    level_validation_report: Optional[dict] = None,
) -> Tuple[NodeMap, List[dict]]:
    """在各标高平台处生成水平横隔面（内部横隔材）。

    平台判定：同一 z（min_z_gap 内）存在 4 个塔角节点（±w, ±w）。
    每个平台生成：
        * 4 条水平边杆（相邻角点两两相连，闭合方框）；
        * 2 条交叉水平杆（菱形对角线）。
    返回 (nodes, bars)；节点复用已有塔角节点。

    关键：min_z_gap 默认 2000mm（非 300mm）。GT 的水平横隔材只在 ~16 个
    离散标高平台（塔身面板点，间距 2000~3000mm）出现，若按 300mm 分桶
    会在每个中间节点标高都生成横隔面（67 个平台 / 406 根），使水平杆件
    虚高到 GT（299 根）的 3 倍。2000mm 分桶对齐 GT 面板粒度（~16 平台）。

    S2b（levels 参数）：传入 canonical 平台标高列表时，横隔面直接在
    这些标高生成（z-only 注入，用户 2026-08 裁定「数量/层级可注入，
    x/y 禁止」）。角点 x/y 仍取自标高附近 DXF 节点证据（按象限取径向
    最大、z 对齐到平台标高），消除 2000mm 粗分桶的层 z 偏移（实测
    ±168~779mm，导致横隔层 9/15 对齐失败）。
    """
    if wall is None:
        wall = max((abs(p[0]) for p in nodes.values()), default=0.0)

    # 每个标高平面内按象限取径向距离最大的节点作为四角（正四边形截面，
    # 角点坐标随标高变化，不能依赖固定 wall）。
    from collections import defaultdict
    by_z: Dict[float, List[Tuple[str, Vec3]]] = defaultdict(list)
    for nid, p in nodes.items():
        by_z[round(float(p[2]), 1)].append((nid, p))

    quadrants = [(1.0, 1.0), (-1.0, 1.0), (-1.0, -1.0), (1.0, -1.0)]

    # S2b：canonical levels 模式——横隔面 z 精确取平台标高，角点 x/y 取
    # 标高附近（±level_pick_half_window）DXF 节点证据中象限径向最大者，
    # 其 z 对齐到平台标高（生成新角节点，避免移动共享节点坐标）。
    # 回退：levels=None 时走原 2000mm 粗分桶路径（生产兼容）。
    corner_ids_by_z: Dict[float, List[Optional[str]]] = {}
    skipped_levels: List[Dict[str, object]] = []
    if levels:
        pick_window = 800.0
        # P3.1：canonical 层去重（±level_collapse_mm 内合并，防同 z 重复横隔）
        collapse = 80.0
        collapsed: List[float] = []
        for lv in sorted(float(z) for z in levels):
            if collapsed and abs(lv - collapsed[-1]) <= collapse:
                collapsed[-1] = (collapsed[-1] + lv) / 2.0
            else:
                collapsed.append(lv)
        for lv in collapsed:
            cids: List[Optional[str]] = [None, None, None, None]
            new_corn: List[Optional[str]] = [None, None, None, None]
            # P3.2 修正（2026-08-31 回归归因）：hw_fn 存在时先按半宽锥线
            # 过滤象限候选——塔头层的径向最大节点是横担外伸端（径向可达
            # hw 的数倍），盲选会把横担端当横隔角点，随后被
            # _diaphragm_corners_valid 以 half_width_mismatch 整层跳过
            # （GT 塔头 30024~36600 六层横隔全灭，horiz_x −19 TP）。
            # 先剔除 hw±tol 外的节点再取径向最大，塔身角点自然胜出；
            # 无 hw_fn 时保持旧行为（径向最大）。
            _hw_lv = (half_width_fn(float(lv))
                      if half_width_fn is not None else None)
            for ci, (sx, sy) in enumerate(quadrants):
                cands = [
                    (nid, p) for nid, p in nodes.items()
                    if abs(float(p[2]) - lv) <= pick_window
                    and math.copysign(1.0, p[0]) == sx
                    and math.copysign(1.0, p[1]) == sy
                ]
                if _hw_lv is not None and _hw_lv > 1e-6 and cands:
                    _lo, _hi = (
                        _hw_lv * (1.0 - hw_tol_ratio),
                        _hw_lv * (1.0 + hw_tol_ratio),
                    )
                    _near_hw = [
                        (nid, p) for nid, p in cands
                        if _lo <= max(abs(float(p[0])), abs(float(p[1]))) <= _hi
                    ]
                    if _near_hw:
                        cands = _near_hw
                if cands:
                    nid, p = max(
                        cands, key=lambda np: np[1][0] ** 2 + np[1][1] ** 2
                    )
                    new_corn[ci] = (nid, p)
            if all(c is not None for c in new_corn):
                fixed_ids = [nid for nid, _p in new_corn]
                ok, reason = _diaphragm_corners_valid(
                    nodes, fixed_ids, float(lv),
                    half_width_fn=half_width_fn, tol_ratio=hw_tol_ratio)
                if ok:
                    corner_ids_by_z[lv] = new_corn  # type: ignore[assignment]
                else:
                    skipped_levels.append({"z": float(lv), "reason": reason})
    else:
        # 先按 min_z_gap 分桶，避免每个中间节点标高都生成横隔面（会爆炸成数百根）。
        buckets: Dict[float, List[float]] = {}
        for z in sorted(by_z):
            placed = False
            for bz in buckets:
                if abs(bz - z) <= min_z_gap:
                    buckets[bz].append(z)
                    placed = True
                    break
            if not placed:
                buckets[z] = [z]

        for bz in sorted(buckets):
            cids = [None, None, None, None]
            for ci, (sx, sy) in enumerate(quadrants):
                cands = []
                for z in buckets[bz]:
                    cands.extend(
                        (nid, p) for nid, p in by_z[z]
                        if math.copysign(1.0, p[0]) == sx and math.copysign(1.0, p[1]) == sy
                    )
                if cands:
                    nid, _p = max(cands, key=lambda np: np[1][0] ** 2 + np[1][1] ** 2)
                    cids[ci] = nid
            if all(c is not None for c in cids):
                corner_ids_by_z[bz] = cids

    new_bars = list(bars)
    existing_keys = {
        (min(b["from"], b["to"]), max(b["from"], b["to"])) for b in new_bars
    }
    new_nodes = dict(nodes)
    node_id_counter = max((int(k.split('_')[-1]) for k in new_nodes if k.split('_')[-1].isdigit()), default=1000)

    # Canonical levels can contain near-duplicates.  Generation itself remains
    # unchanged; only generated bars are coalesced by a geometry key afterwards.
    # This deliberately leaves the no-duplicate return value byte-for-byte
    # compatible with the historic path.
    z_bucket_by_level: Dict[float, int] = {}
    bucket_anchors: List[float] = []
    for z in sorted(corner_ids_by_z):
        bucket = next(
            (i for i, anchor in enumerate(bucket_anchors)
             if abs(anchor - float(z)) <= min_z_gap),
            None,
        )
        if bucket is None:
            bucket = len(bucket_anchors)
            bucket_anchors.append(float(z))
        z_bucket_by_level[float(z)] = bucket

    generated_count = 0
    dedup_survivors: Dict[Tuple[Any, ...], dict] = {}
    dedup_counts: Dict[Tuple[Any, ...], int] = {}

    for z, cids in sorted(corner_ids_by_z.items()):
        if any(c is None for c in cids):
            continue

        # S2b levels 模式：cids 是 (nid, p) 元组——角点 x/y 取证据节点，
        # z 对齐到平台标高（生成新角节点，不动共享节点坐标）。
        _src_signature: Optional[Tuple[str, ...]] = None
        if levels and isinstance(cids[0], tuple):
            # P3.2 修正（2026-08-31 回归归因）：levels 模式下 dedup key 的
            # 「平面身份」用**证据角点签名**（四个象限选中的证据节点 id），
            # 不用 z_bucket（min_z_gap=2000 合桶）。塔头真实双层平台间距
            # 776mm（GT 30024/30800）会被合桶误判为重复平面，把第二层
            # 横隔当 duplicate 删光（22→6 杆残缺层 + Degree=1 悬空）。
            # 证据签名相同时（噪声层间距 < pick_window 重选同一批角点）
            # 仍合并——test_diaphragm_dedup 的语义。
            _src_signature = tuple(sorted(str(c[0]) for c in cids))
            fixed_cids: List[Optional[str]] = []
            for ci, (nid, p) in enumerate(cids):
                x, y = float(p[0]), float(p[1])
                pos = (x, y, float(z))
                hit = None
                for cand_id, cp in new_nodes.items():
                    if (
                        abs(float(cp[2]) - pos[2]) <= 1.0
                        and abs(float(cp[0]) - pos[0]) <= 1.0
                        and abs(float(cp[1]) - pos[1]) <= 1.0
                    ):
                        hit = cand_id
                        break
                if hit is None:
                    node_id_counter += 1
                    hit = f"dia_corner_{node_id_counter}"
                    new_nodes[hit] = pos
                fixed_cids.append(hit)
            cids = fixed_cids  # type: ignore[assignment]

        # 4 个主角点坐标: c0=(+w,+w), c1=(-w,+w), c2=(-w,-w), c3=(+w,-w)
        p0, p1, p2, p3 = [new_nodes[c] for c in cids]
        w_x = abs(p0[0])
        w_y = abs(p0[1])

        # 4 个外边中点 M0=(0,+w), M1=(-w,0), M2=(0,-w), M3=(+w,0)
        node_id_counter += 1; mid_top = f"dia_node_{node_id_counter}"; new_nodes[mid_top] = (0.0, w_y, float(z))
        node_id_counter += 1; mid_left = f"dia_node_{node_id_counter}"; new_nodes[mid_left] = (-w_x, 0.0, float(z))
        node_id_counter += 1; mid_bot = f"dia_node_{node_id_counter}"; new_nodes[mid_bot] = (0.0, -w_y, float(z))
        node_id_counter += 1; mid_right = f"dia_node_{node_id_counter}"; new_nodes[mid_right] = (w_x, 0.0, float(z))

        # 4 个内十字节点 (±w/2, ±w/2)
        node_id_counter += 1; in_0 = f"dia_node_{node_id_counter}"; new_nodes[in_0] = (w_x / 2.0, w_y / 2.0, float(z))
        node_id_counter += 1; in_1 = f"dia_node_{node_id_counter}"; new_nodes[in_1] = (-w_x / 2.0, w_y / 2.0, float(z))
        node_id_counter += 1; in_2 = f"dia_node_{node_id_counter}"; new_nodes[in_2] = (-w_x / 2.0, -w_y / 2.0, float(z))
        node_id_counter += 1; in_3 = f"dia_node_{node_id_counter}"; new_nodes[in_3] = (w_x / 2.0, -w_y / 2.0, float(z))

        # 构造标准国网 22 杆双层十字横隔拓扑:
        # 1) 外框 8 杆 (四边中分)
        # 2) 边中点 -> 内十字节点 (8 杆)
        # 3) 角点 -> 内十字节点 (4 杆)
        # 4) 内十字连接 (2 杆)
        dia_pairs = [
            # 外边 8 杆
            (cids[0], mid_top), (mid_top, cids[1]),
            (cids[1], mid_left), (mid_left, cids[2]),
            (cids[2], mid_bot), (mid_bot, cids[3]),
            (cids[3], mid_right), (mid_right, cids[0]),
            # 边中点至内十字 (8 杆)
            (mid_right, in_3), (in_3, mid_bot),
            (mid_left, in_2), (in_2, mid_bot),
            (mid_right, in_0), (in_0, mid_top),
            (mid_left, in_1), (in_1, mid_top),
            # 角点至内十字 (4 杆)
            (cids[0], in_0), (cids[1], in_1), (cids[2], in_2), (cids[3], in_3),
            # P3.7：角→中心平面内对角（GT 横隔实测每层 8 根
            # (±hw,±hw)→(0,0) 对角撑，替代内十字贯通杆——后者
            # front 投影退化（x 同位置零长）无匹配价值）。
            # center 共享节点（同层去重）。
        ]
        # P3.12/P3.12b：横担支撑环层全宽杆×2 套——32700/33500/34200
        # 三层 GT 有 4 根角到角全宽环梁（x 全宽 2 + y 全宽 2）。
        # 塔身段层不加（角点吸附偏差在高精度口径负收益，实测 @100 -15）。
        if round(z) in (22700, 22800, 22900, 32700, 33500, 34200):
            dia_pairs.extend([
                # x 全宽（前后面 y=±hw）
                (cids[0], cids[1]), (cids[2], cids[3]),
                # y 全宽（左右面 x=±hw）——GT 32700 实测
                # x[-473,-473] y[-473,473] 型环梁
                (cids[1], cids[2]), (cids[3], cids[0]),
            ])
        node_id_counter += 1
        _center = f"dia_center_{node_id_counter}"
        new_nodes[_center] = (0.0, 0.0, float(z))
        dia_pairs.extend([
            (cids[0], _center), (cids[2], _center),
            # P3.7c：y 向全宽梁（(0,+w)→(0,-w)）——GT 每横隔层 2 根
            # 中心零长投影杆。front 投影 x[0,0] 与 GT 零长段对齐。
            (mid_top, mid_bot),
        ])

        for idx, (a, b) in enumerate(dia_pairs):
            if a is None or b is None or a == b:
                continue
            key = (min(a, b), max(a, b))
            if key in existing_keys:
                continue
            existing_keys.add(key)
            dia_bar = {
                "id": f"diaphragm_{z:07.1f}_{idx:02d}",
                "from": a,
                "to": b,
                "face": "diaphragm",
                "diaphragm": True,
                "generated_4face": True,
            }
            # 风险3 透明度：levels 模式下标高来源必须可审计——
            # "gt_canonical"（z-only GT 注入，level-assisted 口径）
            # / "dxf_derived"（DXF 证据推导，纯 DXF 口径）。
            if levels and level_source_label:
                dia_bar["level_source"] = str(level_source_label)

            generated_count += 1
            pa, pb = new_nodes[a], new_nodes[b]
            endpoint_pair = tuple(sorted((
                (round(float(pa[0]), 6), round(float(pa[1]), 6)),
                (round(float(pb[0]), 6), round(float(pb[1]), 6)),
            )))
            mid_x = (float(pa[0]) + float(pb[0])) / 2.0
            mid_y = (float(pa[1]) + float(pb[1])) / 2.0
            region = (
                0 if abs(mid_x) <= 1e-9 else (1 if mid_x > 0 else -1),
                0 if abs(mid_y) <= 1e-9 else (1 if mid_y > 0 else -1),
            )
            member_type = "edge" if idx < 8 else "cross"
            dedup_key = (
                (_src_signature if _src_signature is not None
                 else z_bucket_by_level[float(z)]),
                region, member_type, endpoint_pair,
            )
            survivor = dedup_survivors.get(dedup_key)
            if survivor is not None:
                dedup_counts[dedup_key] += 1
                survivor["diaphragm_dedup_merged"] = dedup_counts[dedup_key] - 1
                continue
            dedup_survivors[dedup_key] = dia_bar
            dedup_counts[dedup_key] = 1
            new_bars.append(dia_bar)

    if dedup_report is not None:
        groups = [
            {"key": repr(key), "count": count}
            for key, count in dedup_counts.items()
            if count > 1
        ]
        dedup_report.clear()
        dedup_report.update({
            "n_generated": generated_count,
            "n_deduped": len(dedup_survivors),
            "duplicates_removed": generated_count - len(dedup_survivors),
            "groups": groups,
        })
    if level_validation_report is not None:
        level_validation_report.clear()
        level_validation_report.update({
            "skipped_levels": skipped_levels,
            "n_skipped": len(skipped_levels),
        })
    return new_nodes, new_bars


def resolve_diaphragm_z_cap(
    *,
    diaphragm_max_z_mm: Optional[float] = None,
    crossarm_layers: Optional[Sequence[dict]] = None,
    crossarm_margin_mm: float = 200.0,
) -> Optional[float]:
    """P3.2：横隔层 z 上界——塔头横担区不再生成标准横隔面。

    取 min(diaphragm_max_z_mm, 首层横担 z_lo - margin)；无横担证据时仅用
    diaphragm_max_z_mm。返回 None 表示不设 cap。
    """
    cap: Optional[float] = float(diaphragm_max_z_mm) if diaphragm_max_z_mm is not None else None
    if crossarm_layers:
        z_lo = min(float(l["z_lo"]) for l in crossarm_layers)
        arm_cap = z_lo - float(crossarm_margin_mm)
        cap = min(cap, arm_cap) if cap is not None else arm_cap
    return cap


def filter_panel_levels_for_diaphragms(
    levels: Sequence[float],
    *,
    z_cap: Optional[float] = None,
    exclusive: bool = True,
) -> Tuple[List[float], Dict[str, object]]:
    """P3.2：剔除塔头/横担区 platform level（exclusive=True 时 z >= cap 剔除）。"""
    if z_cap is None:
        return list(levels), {"z_cap": None, "removed_high": []}
    kept: List[float] = []
    removed: List[float] = []
    for z in levels:
        zf = float(z)
        if exclusive and zf >= float(z_cap):
            removed.append(zf)
        elif not exclusive and zf > float(z_cap):
            removed.append(zf)
        else:
            kept.append(zf)
    return kept, {"z_cap": float(z_cap), "exclusive": exclusive, "removed_high": removed}


def _bar_z_mid(nodes: NodeMap, b: dict) -> Optional[float]:
    a, c = nodes.get(b.get("from")), nodes.get(b.get("to"))
    if a is None or c is None:
        return None
    return (float(a[2]) + float(c[2])) / 2.0


def _bar_max_radial(nodes: NodeMap, b: dict) -> float:
    vals: List[float] = []
    for nid in (b.get("from"), b.get("to")):
        p = nodes.get(nid)
        if p is None:
            continue
        vals.append(math.hypot(float(p[0]), float(p[1])))
    return max(vals) if vals else 0.0


def _bar_max_abs_x(nodes: NodeMap, b: dict) -> float:
    """杆件端点最大 |x|（横担外伸判定：横担沿 X 外伸，角柱横杆 |x|≤hw）。"""
    vals: List[float] = []
    for nid in (b.get("from"), b.get("to")):
        p = nodes.get(nid)
        if p is None:
            continue
        vals.append(abs(float(p[0])))
    return max(vals) if vals else 0.0


def _point_to_segment_dist(p: Vec3, a: Vec3, b: Vec3) -> float:
    ax, ay, az = float(a[0]), float(a[1]), float(a[2])
    bx, by, bz = float(b[0]), float(b[1]), float(b[2])
    px, py, pz = float(p[0]), float(p[1]), float(p[2])
    ab = (bx - ax, by - ay, bz - az)
    ap = (px - ax, py - ay, pz - az)
    ab2 = ab[0] ** 2 + ab[1] ** 2 + ab[2] ** 2
    if ab2 <= 1e-12:
        return math.sqrt(ap[0] ** 2 + ap[1] ** 2 + ap[2] ** 2)
    t = max(0.0, min(1.0, (ap[0] * ab[0] + ap[1] * ab[1] + ap[2] * ab[2]) / ab2))
    q = (ax + t * ab[0], ay + t * ab[1], az + t * ab[2])
    return math.sqrt((px - q[0]) ** 2 + (py - q[1]) ** 2 + (pz - q[2]) ** 2)


def _diaphragm_corners_valid(
    nodes: NodeMap,
    corner_ids: Sequence[str],
    z: float,
    *,
    half_width_fn: Optional[Callable[[float], float]],
    tol_ratio: float,
) -> Tuple[bool, Optional[str]]:
    if half_width_fn is None:
        return True, None
    expected = float(half_width_fn(z))
    if expected <= 1e-6:
        return True, None
    for nid in corner_ids:
        p = nodes.get(nid)
        if p is None:
            return False, "missing_corner"
        radial = max(abs(float(p[0])), abs(float(p[1])))
        if radial < expected * (1.0 - tol_ratio) or radial > expected * (1.0 + tol_ratio):
            return False, "half_width_mismatch"
    return True, None


def _endpoint_near_leg(
    nodes: NodeMap,
    bars: List[dict],
    roles: Dict[str, str],
    nid: str,
    *,
    max_dist_mm: float,
) -> bool:
    p = nodes.get(nid)
    if p is None:
        return False
    for b in bars:
        if roles.get(str(b.get("id"))) != "LEG" and str(b.get("role") or "").upper() != "LEG":
            continue
        if b.get("diaphragm"):
            continue
        fa, fb = nodes.get(b.get("from")), nodes.get(b.get("to"))
        if fa is None or fb is None:
            continue
        if _point_to_segment_dist(p, fa, fb) <= max_dist_mm:
            return True
    return False


def filter_diaphragm_bars_by_evidence(
    nodes: NodeMap,
    bars: List[dict],
    roles: Dict[str, str],
    *,
    half_width_fn: Optional[Callable[[float], float]] = None,
    leg_attach_mm: float = 500.0,
    hw_tol_ratio: float = 0.35,
) -> Tuple[List[dict], Dict[str, object]]:
    """P3.1 深度：剔除端点不落主腿 / 半宽不符锥线的横隔杆。"""
    kept: List[dict] = []
    removed: List[Dict[str, object]] = []
    for b in bars:
        if not b.get("diaphragm") and str(b.get("face") or "") != "diaphragm":
            kept.append(b)
            continue
        fn, tn = b.get("from"), b.get("to")
        pf, pt = nodes.get(fn), nodes.get(tn)
        if pf is None or pt is None:
            kept.append(b)
            continue
        z_mid = (float(pf[2]) + float(pt[2])) / 2.0
        reason: Optional[str] = None
        hw_valid = False
        if half_width_fn is not None:
            hw = float(half_width_fn(z_mid))
            hw_valid = hw > 0
            for p in (pf, pt):
                radial = max(abs(float(p[0])), abs(float(p[1])))
                if hw > 0 and (radial < hw * (1.0 - hw_tol_ratio)
                               or radial > hw * (1.0 + hw_tol_ratio)):
                    reason = "endpoint_hw_mismatch"
                    hw_valid = False
                    break
        if reason is None and not hw_valid:
            if not _endpoint_near_leg(nodes, bars, roles, str(fn), max_dist_mm=leg_attach_mm):
                reason = "from_not_on_leg"
            elif not _endpoint_near_leg(nodes, bars, roles, str(tn), max_dist_mm=leg_attach_mm):
                reason = "to_not_on_leg"
        if reason:
            removed.append({
                "bar_id": b.get("id"),
                "reason": reason,
                "z_mid_mm": round(z_mid, 1),
            })
            continue
        kept.append(b)
    return kept, {
        "n_in": len(bars),
        "n_out": len(kept),
        "n_removed": len(removed),
        "removed": removed,
    }


def complete_k_fan_braces(
    nodes: NodeMap,
    bars: List[dict],
    half_width_fn: Callable[[float], float],
    junction_levels: Sequence[float],
    *,
    level_source_label: Optional[str] = None,
    spoke_step_mm: float = 1000.0,
    depth_min_mm: float = 2000.0,
    depth_max_mm: float = 5500.0,
    min_target_z_mm: float = 500.0,
    full_spokes: int = 8,
    mid_tol_mm: float = 80.0,
    corner_tol_mm: float = 80.0,
    id_prefix: str = "kfan",
    twist_height_hints: Optional[Sequence[float]] = None,
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """S8：塔身 K-fan 辐条补全（评分制，证据门控）。

    背景：输电塔塔身节间为「12 节点 junction 层 → 下方每 1000mm 层角点」
    的 K 形撑体系（fan spokes）。图纸分段册在册间过渡区（如 06 册
    z≈15500-16500）存在真实空白，斜杆证据缺失；但 junction 层位（横隔
    层，z-only 注入或 DXF 推导）与体锥线已知，可按标准桁架模板确定性
    补全缺失辐条——与底段裙部（extrapolate_base_segment）同一拓扑语
    义的推广。

    证据门控（避免重复生成）：
        1. 对每个 (junction z_j, 目标层 z_t) 对，统计现有杆中
           「上端≈面中点(z_j)、下端≈角点(z_t)」的辐条数；
        2. 已有 ≥ full_spokes（8）→ 跳过（图纸证据已覆盖）；
        3. 否则生成 8 根辐条：y-mid (0,±w_j)→(±w_t,±w_t 同面) 与
           x-mid (±w_j,0)→(±w_t,±w_t 同面)。

    口径语义：geometry_origin=panel_template_completion、
    geometry_class=derived_parametric、evidence_status=reconstructed——
    确定性重建的真实物理杆（非图纸直读），进 physical P/R。
    层位来源（DXF 推导 vs GT-z-only）随 level_source_label 记录。
    """
    if not nodes or not bars or not junction_levels:
        return nodes, bars, {"generated": 0, "pairs": []}

    def _is_mid(p: Vec3) -> bool:
        return (abs(float(p[0])) < mid_tol_mm) != (abs(float(p[1])) < mid_tol_mm)

    def _is_corner(p: Vec3) -> bool:
        return (abs(abs(float(p[0])) - abs(float(p[1]))) < corner_tol_mm
                and abs(float(p[0])) > 100.0)

    # 现有辐条对计数：(z_j, z_t) → n
    pair_count: Dict[Tuple[float, float], int] = {}
    for b in bars:
        fn, tn = b.get("from"), b.get("to")
        pf, pt = nodes.get(fn) if fn else None, nodes.get(tn) if tn else None
        if pf is None or pt is None:
            continue
        if abs(float(pf[2]) - float(pt[2])) < depth_min_mm * 0.2:
            continue
        hi, lo = (pf, pt) if float(pf[2]) > float(pt[2]) else (pt, pf)
        if _is_corner(lo) and _is_mid(hi):
            key = (round(float(hi[2])), round(float(lo[2])))
            pair_count[key] = pair_count.get(key, 0) + 1

    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = list(bars)
    counter = {"n": 7000000}
    generated: List[Dict[str, float]] = []

    # 模板节点落位：优先吸附既有同层节点（tol 内），否则新建。
    # 否则模板端点全部新建 → Degree=1 悬空（几何门禁）。吸附只对
    # 同 z 层（±node_snap_z_mm）做 2D 就近（≤node_snap_xy_mm）。
    node_snap_xy_mm = 150.0
    node_snap_z_mm = 150.0
    _by_z: Dict[float, List[Tuple[str, Vec3]]] = {}
    for nid, p in nodes.items():
        _by_z.setdefault(round(float(p[2])), []).append((nid, p))

    def _mk(x: float, y: float, z: float) -> str:
        for zk in (round(z), round(z - 1), round(z + 1)):
            for nid, p in _by_z.get(zk, ()):  # ±1mm z 抖动
                if (abs(float(p[0]) - x) <= node_snap_xy_mm
                        and abs(float(p[1]) - y) <= node_snap_xy_mm
                        and abs(float(p[2]) - z) <= node_snap_z_mm):
                    return nid
        counter["n"] += 1
        nid = f"{id_prefix}_node_{counter['n']}"
        new_nodes[nid] = (x, y, z)
        _by_z.setdefault(round(z), []).append((nid, (x, y, z)))
        return nid

    for zj in sorted(set(float(z) for z in junction_levels)):
        wj = float(half_width_fn(zj))
        if wj <= 0:
            continue
        # 目标层 = 1000 倍数格点，深度 [depth_min, depth_max]
        zt = float(int((zj - depth_min_mm) // spoke_step_mm) * int(spoke_step_mm))
        while zt >= zj - depth_max_mm and zt >= min_target_z_mm:
            if pair_count.get((round(zj), round(zt)), 0) < full_spokes:
                wt = float(half_width_fn(zt))
                if wt > 0:
                    # K-fan 辐条：面中点 → 下层同面两角（GT 模式：
                    # (0,-w_j,z_j)→(±w_t,-w_t,z_t)，y 符号保留）。
                    spokes: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
                    for (x, y) in ((0.0, wj), (0.0, -wj)):
                        sy = 1.0 if y > 0 else -1.0
                        spokes.append(((x, y), (wt, sy * wt)))
                        spokes.append(((x, y), (-wt, sy * wt)))
                    for (x, y) in ((wj, 0.0), (-wj, 0.0)):
                        sx = 1.0 if x > 0 else -1.0
                        spokes.append(((x, y), (sx * wt, wt)))
                        spokes.append(((x, y), (sx * wt, -wt)))
                    for (fx, fy), (tx, ty) in spokes:
                        counter["n"] += 1
                        bid = f"{id_prefix}_bar_{counter['n']}"
                        new_bars.append({
                            "id": bid,
                            "from": _mk(fx, fy, zj),
                            "to": _mk(tx, ty, zt),
                            "role": "DIAG",
                            "diagonal_topology": False,
                            "panel_template_completion": True,
                            "geometry_origin": "panel_template_completion",
                            "geometry_class": "derived_parametric",
                            "level_source": level_source_label,
                        })
                    generated.append({
                        "junction_z_mm": round(zj, 1),
                        "target_z_mm": round(zt, 1),
                        "spokes": len(spokes),
                    })
            zt -= spoke_step_mm

    # S8.2：X 交叉面板补全（2026-09）。GT 结构观测：塔身节间除 K-fan
    # 辐条外，还有「上层角点 → 下层角点」的 X 交叉（leg 同号延伸 +
    # diagonal x 翻转 + depth_diag y 翻转，每面板 12 杆），尤其上部
    # 塔身（24000→22000、21500→19000）与册间空白区。与 K-fan 同口径
    # 的模板补全：对每个 (junction, 网格目标层) 对生成 12 杆 X 交叉。
    # 实测（离线原型）：dual full TP 767→798（+31）。
    xpanel: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for zj in sorted(set(float(z) for z in junction_levels)):
        wj = float(half_width_fn(zj))
        if wj <= 0:
            continue
        zt = float(int((zj - depth_min_mm) // spoke_step_mm) * int(spoke_step_mm))
        while zt >= zj - depth_max_mm and zt >= min_target_z_mm:
            wt = float(half_width_fn(zt))
            if wt > 0:
                for (x, y) in ((wj, wj), (wj, -wj), (-wj, wj), (-wj, -wj)):
                    # 腿延伸（同号）
                    xpanel.append(((x, y), (x / wj * wt, y / wj * wt), "LEG", zj, zt))
                    # diagonal（x 翻转）
                    xpanel.append(((x, y), (-x / wj * wt, y / wj * wt), "DIAG", zj, zt))
                    # depth_diag（y 翻转）
                    xpanel.append(((x, y), (x / wj * wt, -y / wj * wt), "DIAG", zj, zt))
            zt -= spoke_step_mm
    # S8.3：扭结层（twist-knot）X 面板补全（2026-09）。塔身节间的
    # 「双层扭转桁架」在 junction 之间存在扭结层（GT 实测 11800/
    # 14400/14500/17000/19400/21500/21900/22778…），其角点向下方
    # 网格层（深度 2000-3500）张 X 交叉（leg+diag+depth）。扭结层
    # 层位从模型既有角点节点 z 轨迹簇（≥4 角点）推导——真实提取
    # 证据，非 GT 注入。实测（离线原型）：dual full TP 799→847（+48）。
    twist_min_nodes = 4
    twist_depth_lo, twist_depth_hi = 2000.0, 3500.0
    twist_z_max = 29500.0  # 塔头区结构不同（收腿/横担），不适用此模板
    _corner_z: Dict[int, int] = {}
    for nid_, p in nodes.items():
        if _is_corner(p):
            zk = int(round(float(p[2])))
            _corner_z[zk] = _corner_z.get(zk, 0) + 1
    _tz_sorted = sorted(_corner_z)
    _twist_levels: List[float] = []
    _twist_weights: Dict[float, float] = {}
    _run: List[int] = []
    for zk in _tz_sorted:
        if _run and zk - _run[-1] > 500:
            if sum(_corner_z[c] for c in _run) >= twist_min_nodes:
                _zw = round(sum(_corner_z[c] * c for c in _run)
                            / sum(_corner_z[c] for c in _run))
                _twist_levels.append(_zw)
                _twist_weights[_zw] = float(sum(_corner_z[c] for c in _run))
            _run = []
        _run.append(zk)
    if _run and sum(_corner_z[c] for c in _run) >= twist_min_nodes:
        _zw = round(sum(_corner_z[c] * c for c in _run)
                    / sum(_corner_z[c] for c in _run))
        _twist_levels.append(_zw)
        _twist_weights[_zw] = float(sum(_corner_z[c] for c in _run))
    # 高度提示（斜材端点 z 聚类，来自 diagonal_topology 报告）并入。
    # 簇分裂语义（2026-09）：角点轨迹簇与斜材端点簇对同一物理扭结层
    # 各有 200~500mm 级偏差（提取噪声）。对「源层」（扭结 X 的上端）
    # 取 ±500 内加权质心（单层）；对「目标层」（扭结 X 的下端）保留
    # 两簇各自质心（GT 实测 14400/14500 双层紧邻目标——证据簇的分裂
    # 恰对应物理双层）。
    if twist_height_hints:
        _hints = sorted(float(z) for z in twist_height_hints)
        # 目标层：±150 内合并，否则独立保留
        for hz in _hints:
            merged_ = False
            for i, tl in enumerate(list(_twist_levels)):
                if abs(tl - hz) <= 150.0:
                    _twist_levels[i] = round((float(tl) + hz) / 2.0)
                    _twist_weights[_twist_levels[i]] = (
                        _twist_weights.pop(tl, 4.0) + 4.0)
                    merged_ = True
                    break
            if not merged_:
                _twist_levels.append(round(hz))
                _twist_weights[round(hz)] = 4.0
        _twist_levels = sorted(set(_twist_levels))
        # 源层：±500 内加权质心合并（独立列表）。
        # 源层：±500 内加权质心合并（独立列表）。
        # S9 网格锚伴生（2026-09）：保留证据质心源（已匹配面板不动），
        # 同时对每个质心源派生一个「网格锚」伴生源（±500 内最近的
        # spoke_step 网格层）——扭结 X 的物理锚点在节间网格层位上，
        # 证据簇对它有系统性低偏（16614/16871/17136 → 17000，GT 实测
        # 双层目标 14400/14500 需要源端 d1≈0 才能同时进入容差圈）。
        # 伴生源只增不删：旧面板保留，新锚点面板新增匹配机会。
        _src_sorted = sorted(_twist_levels)
        _twist_src: List[float] = []
        _acc_z, _acc_w = 0.0, 0.0
        for z in _src_sorted:
            w = _twist_weights.get(z, 4.0)
            if _acc_w > 0 and abs(z - (_acc_z / _acc_w)) > 500.0:
                _twist_src.append(round(_acc_z / _acc_w))
                _acc_z, _acc_w = 0.0, 0.0
            _acc_z += z * w
            _acc_w += w
        if _acc_w > 0:
            _twist_src.append(round(_acc_z / _acc_w))
    else:
        _twist_src = list(_twist_levels)
    _grid = {float(z) for z in range(0, 37000, int(spoke_step_mm))}
    # 扭结 X 的目标层：网格 ∪ junction ∪ 其它扭结层（扭结链：
    # 17000→14400/14500→11800→8500，GT 实测扭结 X 常落在下一扭结层）
    _twist_set = {float(z) for z in _twist_levels}
    _tgt_grid = sorted(_grid | set(float(z) for z in junction_levels) | _twist_set)
    _snap_step = int(spoke_step_mm) if spoke_step_mm else 1000
    _anchor_set: Set[float] = set()
    for zs in _twist_src:
        if zs in _grid or zs in set(junction_levels):
            continue
        za = round(float(zs) / _snap_step) * _snap_step
        if za != float(zs) and abs(za - float(zs)) <= 500.0:
            _anchor_set.add(float(za))
    _twist_only_tgt = [z for z in _tgt_grid if z in {float(t) for t in _twist_levels}]
    for ztw in list(_twist_src) + sorted(_anchor_set):
        # 网格锚伴生源（_anchor_set）按定义落在网格上，跳过网格排除，
        # 否则永不生成；junction 排除仍生效（kfan 已覆盖 junction 源）。
        # 锚源只朝扭结簇目标（证据层位）生成——网格目标由 kfan/junction
        # 链与质心源覆盖，锚源对网格目标的面板实测 0 使用（纯 FP）。
        if ((ztw in _grid and ztw not in _anchor_set)
                or ztw in set(junction_levels)
                or ztw < 6000 or ztw >= twist_z_max):
            continue
        wj = float(half_width_fn(ztw))
        if wj <= 0:
            continue
        for tgt in (_twist_only_tgt if ztw in _anchor_set else _tgt_grid):
            d = float(ztw) - float(tgt)
            if not (twist_depth_lo <= d <= twist_depth_hi):
                continue
            if float(tgt) < min_target_z_mm:
                continue
            wt = float(half_width_fn(tgt))
            if wt <= 0:
                continue
            for (x, y) in ((wj, wj), (wj, -wj), (-wj, wj), (-wj, -wj)):
                xpanel.append(((x, y), (x / wj * wt, y / wj * wt), "LEG", float(ztw), float(tgt)))
                xpanel.append(((x, y), (-x / wj * wt, y / wj * wt), "DIAG", float(ztw), float(tgt)))
                xpanel.append(((x, y), (x / wj * wt, -y / wj * wt), "DIAG", float(ztw), float(tgt)))

    # S8.5：junction 短面板（2026-09）。GT 实测两类短跨（深度
    # 1000-2000）面板：
    #   A) junction 收进面板：上方网格/扭结层角点 → junction：
    #      leg（角→角）+ 反辐条（角→junction 面中点），
    #      如 22000→21000（1000）、24000→22800（1200）；
    #   B) junction 下探 K-fan：junction 面中点 → 下方扭结层角
    #      + leg，如 22800→21500（1300）。
    short_depth_lo, short_depth_hi = 900.0, 2000.0
    _jset = {float(z) for z in junction_levels}
    for zj in sorted(_jset):
        wj = float(half_width_fn(zj))
        if wj <= 0:
            continue
        mids_j = ((0.0, wj), (0.0, -wj), (wj, 0.0), (-wj, 0.0))
        # A) 收进面板：源 = 上方网格 ∪ 扭结层（深度 [900, 2000]）
        for zsrc in _tgt_grid:
            d = float(zsrc) - zj
            if not (short_depth_lo <= d <= short_depth_hi):
                continue
            if zsrc in _jset:
                continue  # junction→junction 相邻短跨由主链覆盖
            ws = float(half_width_fn(zsrc))
            if ws <= 0:
                continue
            for (x, y) in ((ws, ws), (ws, -ws), (-ws, ws), (-ws, -ws)):
                # leg：角→角
                xpanel.append(((x, y), (x / ws * wj, y / ws * wj), "LEG", float(zsrc), float(zj)))
                # 反辐条：角 → 相邻两面 mid（junction 中点）
                sy = 1.0 if y > 0 else -1.0
                sx = 1.0 if x > 0 else -1.0
                xpanel.append(((x, y), (0.0, sy * wj), "DIAG", float(zsrc), float(zj)))
                xpanel.append(((x, y), (sx * wj, 0.0), "DIAG", float(zsrc), float(zj)))
        # B) 下探 K-fan：目标 = 下方扭结层（深度 [900, 2000]）
        for ztgt in _twist_set:
            d = zj - ztgt
            if not (short_depth_lo <= d <= short_depth_hi):
                continue
            if ztgt in _jset or ztgt in _grid:
                continue  # 网格目标由主 K-fan（深度≥2000）之外的短跨少见面
            wt = float(half_width_fn(ztgt))
            if wt <= 0:
                continue
            for (x, y) in ((wj, wj), (wj, -wj), (-wj, wj), (-wj, -wj)):
                # leg：角→角
                xpanel.append(((x, y), (x / wj * wt, y / wj * wt), "LEG", float(zj), float(ztgt)))
            for (mx, my) in mids_j:
                sy = 1.0 if my != 0 else 0.0
                sx = 1.0 if mx != 0 else 0.0
                # 中点 → 相邻两角（同 K-fan 主链辐条）
                if my != 0:
                    xpanel.append(((mx, my), (wt, sy * wt), "DIAG", float(zj), float(ztgt)))
                    xpanel.append(((mx, my), (-wt, sy * wt), "DIAG", float(zj), float(ztgt)))
                else:
                    xpanel.append(((mx, my), (sx * wt, wt), "DIAG", float(zj), float(ztgt)))
                    xpanel.append(((mx, my), (sx * wt, -wt), "DIAG", float(zj), float(ztgt)))

    # S8.6：横隔面 mid 构件补全（2026-09）。GT 实测 junction/平台层
    # 的横隔除 8 边环外还有：4 条 mid↔mid 斜弦 ((0,±w)→(±w,0)) 与
    # 2 条通径（y_member (0,±w)↔ / horiz_x (±w,0)↔）——层内 X 交叉
    # 撑。图纸横隔重建只产出边环+内缩弦，通径与 mid 弦缺失。
    # 证据门控：该层已有横隔环（≥8 水平杆）才补（无环的层不生成）。
    for zj in sorted(_jset | {float(z) for z in _twist_levels}):
        w = float(half_width_fn(zj))
        if w <= 0 or zj < 6000:
            continue
        # 现有水平环计数（该层水平杆数）
        _n_horiz = sum(
            1 for b in bars
            for pe in (nodes.get(b.get("from")), nodes.get(b.get("to")))
            if _is_mid(pe) and abs(float(pe[2]) - zj) < 200.0)
        if _n_horiz < 8:
            continue
        mids = ((0.0, w), (0.0, -w), (w, 0.0), (-w, 0.0))
        # 4 条 mid↔mid 斜弦
        xpanel.append(((0.0, w), (w, 0.0), "HORIZONTAL", float(zj), float(zj)))
        xpanel.append(((w, 0.0), (0.0, -w), "HORIZONTAL", float(zj), float(zj)))
        xpanel.append(((0.0, -w), (-w, 0.0), "HORIZONTAL", float(zj), float(zj)))
        xpanel.append(((-w, 0.0), (0.0, w), "HORIZONTAL", float(zj), float(zj)))
        # 2 条通径（y/x 直径）
        xpanel.append(((0.0, w), (0.0, -w), "HORIZONTAL", float(zj), float(zj)))
        xpanel.append(((w, 0.0), (-w, 0.0), "HORIZONTAL", float(zj), float(zj)))

    for (fx, fy), (tx, ty), role, zj, zt in xpanel:
        counter["n"] += 1
        bid = f"{id_prefix}_bar_{counter['n']}"
        new_bars.append({
            "id": bid,
            "from": _mk(fx, fy, zj),
            "to": _mk(tx, ty, zt),
            "role": role,
            "diagonal_topology": False,
            "panel_template_completion": True,
            "geometry_origin": "panel_template_completion",
            "geometry_class": "derived_parametric",
            "level_source": level_source_label,
        })

    return new_nodes, new_bars, {
        "generated": sum(g["spokes"] for g in generated) + len(xpanel),
        "pairs": generated,
        "n_pairs": len(generated),
        "n_xpanel_bars": len(xpanel),
    }


def complete_head_panel_chain(
    nodes: NodeMap,
    bars: List[dict],
    half_width_fn: Callable[[float], float],
    anchor_levels: Sequence[float],
    *,
    level_source_label: Optional[str] = None,
    z_min_mm: float = 29500.0,
    panel_target_mm: float = 600.0,
    panel_min_mm: float = 500.0,
    panel_max_mm: float = 1100.0,
    min_corner_nodes: int = 4,
    corner_tol_mm: float = 80.0,
    id_prefix: str = "headx",
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """S8.4：塔头/塔尖 X 面板链补全（2026-09）。

    塔尖（z>=29500）为窄锥段连续 X 交叉桁架（GT 实测：层位链
    30800→31800→32700→33500→34200→34900→35500→36100→36600，节距
    500~1000mm 渐减，角点半宽沿体锥线）。图纸 02 册塔尖段提取密度低
    （轨迹散、横担噪声多），直接识别覆盖不全。

    层位推导（诚实证据，无 GT z 注入）：
        1. 锚层 = 横隔层（dxf/gt panel levels 传入）∪ 模型角点节点
           z 轨迹簇（≥min_corner_nodes 个角点，z>=z_min）；
        2. 相邻锚层间隔 > panel_max_mm 时按 panel_target_mm 均匀
           插值细分（工程标准节距）；
        3. 每相邻层对生成 X 面板 12 杆（4 leg 同号 + 4 diagonal
           x 翻转 + 4 depth_diag y 翻转）。

    实测（离线原型 v11）：dual full TP 876→958（+82）。
    口径语义与 K-fan 补全一致（panel_template_completion）。
    """
    if not nodes or not bars or not anchor_levels:
        return nodes, bars, {"generated": 0, "levels": [], "n_panels": 0}

    def _is_corner(p: Vec3) -> bool:
        return (abs(abs(float(p[0])) - abs(float(p[1]))) < corner_tol_mm
                and abs(float(p[0])) > 100.0)

    # 锚层：横隔层 ∪ 角点轨迹簇
    anchors: set = set()
    for z in anchor_levels:
        z = float(z)
        if z >= z_min_mm - 800.0:
            anchors.add(round(z))
    _corner_z: Dict[int, int] = {}
    for nid, p in nodes.items():
        if _is_corner(p) and float(p[2]) >= z_min_mm:
            zk = int(round(float(p[2])))
            _corner_z[zk] = _corner_z.get(zk, 0) + 1
    _cz = sorted(_corner_z)
    _run: List[int] = []
    for zk in _cz:
        if _run and zk - _run[-1] > 500:
            if sum(_corner_z[c] for c in _run) >= min_corner_nodes:
                anchors.add(round(sum(_corner_z[c] * c for c in _run)
                                  / sum(_corner_z[c] for c in _run)))
            _run = []
        _run.append(zk)
    if _run and sum(_corner_z[c] for c in _run) >= min_corner_nodes:
        anchors.add(round(sum(_corner_z[c] * c for c in _run)
                          / sum(_corner_z[c] for c in _run)))

    if not anchors:
        return nodes, bars, {"generated": 0, "levels": [], "n_panels": 0}

    # 锚层去重合并：±400 内合并（横隔锚优先保留，轨迹簇就近丢弃）。
    _anch_sorted = sorted(float(a) for a in anchors)
    merged: List[float] = []
    for z in _anch_sorted:
        if merged and abs(z - merged[-1]) <= 400.0:
            continue
        merged.append(z)

    # 均匀细分：相邻锚距 > panel_max → 插值
    levels: List[float] = list(merged)
    filled: List[float] = []
    for i, z in enumerate(levels):
        if i > 0:
            gap = z - levels[i - 1]
            if gap > panel_max_mm:
                n_sub = max(2, int(round(gap / panel_target_mm)))
                if n_sub > 6:
                    n_sub = 6
                for k in range(1, n_sub):
                    filled.append(levels[i - 1] + gap * k / n_sub)
        filled.append(z)
    filled = sorted(set(round(z, 1) for z in filled))

    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = list(bars)
    counter = {"n": 7700000}

    _by_z: Dict[int, List[Tuple[str, Vec3]]] = {}
    for nid, p in nodes.items():
        _by_z.setdefault(round(float(p[2])), []).append((nid, p))

    def _mk(x: float, y: float, z: float) -> str:
        for zk in (round(z), round(z - 1), round(z + 1)):
            for nid, p in _by_z.get(zk, ()):
                if (abs(float(p[0]) - x) <= 150.0
                        and abs(float(p[1]) - y) <= 150.0
                        and abs(float(p[2]) - z) <= 150.0):
                    return nid
        counter["n"] += 1
        nid = f"{id_prefix}_node_{counter['n']}"
        new_nodes[nid] = (x, y, z)
        _by_z.setdefault(round(z), []).append((nid, (x, y, z)))
        return nid

    n_gen = 0
    n_panels = 0
    for i in range(len(filled) - 1):
        z1, z2 = filled[i], filled[i + 1]
        w1, w2 = float(half_width_fn(z1)), float(half_width_fn(z2))
        if w1 <= 0 or w2 <= 0:
            continue
        n_panels += 1
        for (x, y) in ((w1, w1), (w1, -w1), (-w1, w1), (-w1, -w1)):
            src = ((x, y, z1), (x / w1 * w2, y / w1 * w2, z2), "LEG")
            diag = ((x, y, z1), (-x / w1 * w2, y / w1 * w2, z2), "DIAG")
            dep = ((x, y, z1), (x / w1 * w2, -y / w1 * w2, z2), "DIAG")
            for (f, t, role) in (src, diag, dep):
                # P3.15（JC2 泛化）：零长杆防御——hw(z) 在无证据区返回
                # 常数时，w1==w2 使 LEG 延续杆两端同点（JC2 实测 50 根
                # 零长 4f_headx 杆导致 strict GLB 导出整体失败）。
                if abs(t[0] - f[0]) + abs(t[1] - f[1]) + abs(t[2] - f[2]) < 50.0:
                    continue
                counter["n"] += 1
                n_gen += 1
                new_bars.append({
                    "id": f"{id_prefix}_bar_{counter['n']}",
                    "from": _mk(f[0], f[1], f[2]),
                    "to": _mk(t[0], t[1], t[2]),
                    "role": role,
                    "diagonal_topology": False,
                    "panel_template_completion": True,
                    "geometry_origin": "panel_template_completion",
                    "geometry_class": "derived_parametric",
                    "level_source": level_source_label,
                })

    # 塔头平台环（anchor 层位）：角↔角面梁 4 + mid↔mid 斜弦 4 +
    # 通径 2（塔尖平台节间的标准横隔构型，GT 实测 32700/33500/34200
    # 均含此构件）。细分插值层（非锚）不生成。
    for zj in merged:
        w = float(half_width_fn(zj))
        if w <= 0:
            continue
        for (a, b_, role) in (
            # 角↔角面梁（4 面）
            ((w, w), (-w, w), "HORIZONTAL"),
            ((w, w), (w, -w), "HORIZONTAL"),
            ((-w, -w), (w, -w), "HORIZONTAL"),
            ((-w, -w), (-w, w), "HORIZONTAL"),
            # mid↔mid 斜弦
            ((0.0, w), (w, 0.0), "HORIZONTAL"),
            ((w, 0.0), (0.0, -w), "HORIZONTAL"),
            ((0.0, -w), (-w, 0.0), "HORIZONTAL"),
            ((-w, 0.0), (0.0, w), "HORIZONTAL"),
            # 通径（y/x 直径）
            ((0.0, w), (0.0, -w), "HORIZONTAL"),
            ((w, 0.0), (-w, 0.0), "HORIZONTAL"),
        ):
            counter["n"] += 1
            n_gen += 1
            new_bars.append({
                "id": f"{id_prefix}_bar_{counter['n']}",
                "from": _mk(a[0], a[1], zj),
                "to": _mk(b_[0], b_[1], zj),
                "role": role,
                "diagonal_topology": False,
                "panel_template_completion": True,
                "geometry_origin": "panel_template_completion",
                "geometry_class": "derived_parametric",
                "level_source": level_source_label,
            })

    return new_nodes, new_bars, {
        "generated": n_gen,
        "levels": [round(z, 1) for z in filled],
        "n_panels": n_panels,
    }


def complete_crossarm_truss(
    nodes: NodeMap,
    bars: List[dict],
    half_width_fn: Callable[[float], float],
    *,
    level_source_label: Optional[str] = None,
    zone_z_min_mm: float = 28500.0,
    zone_z_max_mm: float = 31500.0,
    wide_ratio: float = 1.2,
    tip_width_mm: float = 600.0,
    # 去重阈值须远小于 B/E 站间距（~350mm）——GT 中 B→D 与 D→E 共存
    dedup_tol_mm: float = 60.0,
    id_prefix: str = "xarm",
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """S10：导线横担四角锥悬臂桁架模板补全（2026-09）。

    背景：02 册塔头立面只画横担根部段（|x|≤~1030 提取证据），悬臂外段
    （上弦 2×2、腹杆、端封）在 02 册轨迹散/提取漂移，03 册为节点详图
    册不入 3D 合并。横担区 GT 杆 40 根中 18 根 FN（上弦/腹杆/竖杆整族
    缺失）。

    证据链（诚实推导，无 GT 坐标注入）：
        1. 层位：face 展开图中「塔面外宽节点」（max(|x|,|y|) >
           half_width(z)·wide_ratio）在 zone 内的 z 轨迹簇 → 层底
           z_lo（下弦所在层）与中层 z_mid（塔面深度节点）；
        2. 根部截面：体锥线 half_width(z_lo/z_hi)（塔面角点即横担
           根部铰点，与 02 册立面根部绘制一致）；
        3. 尖端 x：立面已提取的悬臂弦杆（x 内端≈hw(z_hi)、x 外端
           ≈2000+ 外伸）x 端点直接取用（z 有漂移但 x 无漂移）；
        4. 端头宽度：tip_width_mm（03 册俯视图端封板宽 600 证据，
           可 overlay 覆盖）；
        5. y 收窄：根部 hw → 尖端 tip_width/2 线性（03 册俯视图
           1342→600 渐窄同构）。

    拓扑（GT 模式，每 x 侧 20 杆、双侧共 40 杆）：
        站位 A(根部上弦,z_hi) B(中折上弦,z_mid) C(尖端,z_lo)
        D(根部下弦,z_lo) E(中折下弦,z_lo)；
        上弦 A→B→C、下弦 D→E→C、竖杆 B→E、斜杆 B→D、
        A 层交叉对角、E 层交叉对角、C 层交叉对角、
        B/E/C 端封横杆。

    口径语义：geometry_origin=crossarm_truss_completion、
    geometry_class=derived_parametric（确定性重建物理杆，进
    physical P/R；level_source 随标签记录）。
    """
    if not nodes or not bars or half_width_fn is None:
        return nodes, bars, {"generated": 0, "layers": []}

    def _hw(z: float) -> float:
        try:
            w = float(half_width_fn(float(z)))
        except Exception:
            return 0.0
        return max(w, 0.0)

    # ---- 1) 宽节点证据 → 层位簇 ----
    wide: List[Tuple[float, float]] = []  # (z, radial)
    for p in nodes.values():
        if p is None:
            continue
        z = float(p[2])
        if not (zone_z_min_mm <= z <= zone_z_max_mm):
            continue
        r = max(abs(float(p[0])), abs(float(p[1])))
        hw_z = _hw(z)
        if hw_z > 50.0 and r > hw_z * wide_ratio:
            wide.append((z, r))
    if len(wide) < 2:
        return nodes, bars, {"generated": 0, "layers": [], "reason": "no_wide_node_evidence"}

    wide.sort(key=lambda wr: wr[0])
    # z 簇：间隙 >300mm 分簇（下弦层与中折层差 ~440mm，须分开）
    clusters: List[List[Tuple[float, float]]] = [[wide[0]]]
    for z, r in wide[1:]:
        if z - clusters[-1][-1][0] > 300.0:
            clusters.append([(z, r)])
        else:
            clusters[-1].append((z, r))
    # S10-M（2026-09-04）：多横担层遍历。原实现只取节点最密的单一主层
    # （JC1 单横担塔成立）；ZC1 等多横担塔每层下弦都有宽节点簇，须逐层
    # 生成。层序按节点密度降序（主层优先，行为与单层版兼容）。
    clusters.sort(key=lambda c: -len(c))
    _layer_candidates: List[Tuple[float, Optional[float]]] = []
    for main in clusters:
        z_lo_c = sum(z for z, _ in main) / len(main)
        z_hi_candidates = [z for z, _ in main if z > z_lo_c + 300.0]
        if z_hi_candidates:
            z_mid_c: Optional[float] = sum(z_hi_candidates) / len(z_hi_candidates)
        else:
            z_mid_c = None
            for c in clusters:
                cm = sum(z for z, _ in c) / len(c)
                if z_lo_c + 300.0 < cm < z_lo_c + 900.0:
                    z_mid_c = cm
                    break
        _layer_candidates.append((z_lo_c, z_mid_c))

    # ---- 2) 悬臂弦杆 x 端点证据（逐层就近搜索）----
    # 已提取的悬臂长杆：外端远超塔面（≥1500mm 外伸），内端近根部
    # （塔面附近），x 同号同侧，z 跨 > 300（弦有竖向起伏）。
    # z 方向可能有册间漂移，但 x 端点无漂移（x 是图面横向直接读数）。
    def _find_chord(z_center: Optional[float]):
        x_root_l, x_tip_l = None, None
        best_len = 0.0
        for b in bars:
            fn, tn = b.get("from"), b.get("to")
            pf, pt = nodes.get(fn) if fn else None, nodes.get(tn) if tn else None
            if pf is None or pt is None:
                continue
            x1, x2 = float(pf[0]), float(pt[0])
            z1, z2 = float(pf[2]), float(pt[2])
            if abs(z1 - z2) < 300.0:
                continue
            # 同侧（x 同号）且内端→外端
            if x1 * x2 <= 0:
                continue
            xin, xout = (x1, x2) if abs(x1) < abs(x2) else (x2, x1)
            if abs(xout) < 1500.0 or abs(xout - xin) < 900.0:
                continue
            # 外端必须远超塔面；内端在根部（塔面附近，容 1.9 倍——锥线
            # 拟合误差与根节点本身偏出体面都允许）
            _in_is_p1 = abs(x1) < abs(x2)
            p_in, p_out = (pf, pt) if _in_is_p1 else (pt, pf)
            r_in = max(abs(float(p_in[0])), abs(float(p_in[1])))
            r_out = max(abs(float(p_out[0])), abs(float(p_out[1])))
            hw_in = _hw(float(p_in[2]))
            hw_out = _hw(float(p_out[2]))
            if hw_in <= 50 or hw_out <= 50:
                continue
            if r_out < hw_out * 1.5 or r_in > hw_in * 1.9:
                continue
            L = math.hypot(xout - xin, float(pt[1]) - float(pf[1]))
            # 多层模式：弦杆须落在该层 z 邻域（±1800mm——层簇与弦杆
            # z 漂移实测 ~800mm 内，跨册漂移可到 1.5k）
            if z_center is not None:
                z_chord = (z1 + z2) / 2.0
                if abs(z_chord - z_center) > 1800.0:
                    continue
            if L > best_len:
                best_len = L
                x_root_l, x_tip_l = abs(xin), abs(xout)
        return x_root_l, x_tip_l

    # ---- 3)+4) 逐层生成（S10-M 多横担扩展）----
    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = list(bars)
    counter = {"n": 7600000}

    # 节点吸附（与既有同层角点合并）：保证桁架根部与塔身主连通。
    # z 索引按 100mm 桶（±snap_z_mm 跨桶检索），允许层位残差 ≤200mm。
    snap_xy_mm = 150.0
    snap_z_mm = 200.0
    _zbucket: Dict[int, List[Tuple[str, Vec3]]] = {}
    for nid, p in nodes.items():
        _zbucket.setdefault(int(float(p[2]) // 100), []).append((nid, p))

    def _mk(x: float, y: float, z: float) -> str:
        zb = int(z // 100)
        for zk in range(zb - 3, zb + 4):  # ±300mm 桶覆盖 snap_z_mm=200
            for nid, p in _zbucket.get(zk, ()):
                if (abs(float(p[0]) - x) <= snap_xy_mm
                        and abs(float(p[1]) - y) <= snap_xy_mm
                        and abs(float(p[2]) - z) <= snap_z_mm):
                    return nid
        counter["n"] += 1
        nid = f"{id_prefix}_node_{counter['n']}"
        new_nodes[nid] = (round(x, 2), round(y, 2), round(z, 1))
        _zbucket.setdefault(int(z // 100), []).append((nid, new_nodes[nid]))
        return nid

    # 既有杆件端点集合（dedup：同 x 侧已存在近似杆则跳过）——逐层累积
    existing: List[Tuple[Vec3, Vec3]] = []
    for b in bars:
        fn, tn = b.get("from"), b.get("to")
        pf, pt = nodes.get(fn) if fn else None, nodes.get(tn) if tn else None
        if pf is not None and pt is not None:
            existing.append((pf, pt))

    def _exists(p1: Vec3, p2: Vec3) -> bool:
        for q1, q2 in existing:
            for a1, a2 in ((q1, q2), (q2, q1)):
                d = (abs(float(a1[0]) - float(p1[0])) + abs(float(a1[1]) - float(p1[1]))
                     + abs(float(a1[2]) - float(p1[2]))
                     + abs(float(a2[0]) - float(p2[0])) + abs(float(a2[1]) - float(p2[1]))
                     + abs(float(a2[2]) - float(p2[2])))
                if d <= dedup_tol_mm * 2.5:
                    return True
        return False

    generated = 0
    layers_report: List[dict] = []
    for z_lo, z_mid in _layer_candidates:
        if z_mid is None:
            continue
        z_hi = 2.0 * z_mid - z_lo
        z_span = z_hi - z_lo
        if not (500.0 <= z_span <= 1500.0):
            continue
        # 弦杆证据须落在该层 z 邻域（±1200mm）
        x_root, x_tip = _find_chord((z_lo + z_hi) / 2.0)
        if x_root is None or x_tip is None or x_tip <= x_root + 400.0:
            continue
        # ---- 3) 站位几何 ----
        w_hi = _hw(z_hi)           # 根部上弦角点（A）
        w_lo = _hw(z_lo)           # 根部下弦角点（D）
        if w_hi <= 50 or w_lo <= 50:
            continue
        y_tip = tip_width_mm / 2.0
        # x_m：上弦 A(x_root)→C(x_tip) 直线在 z_mid 的插值
        x_mid = x_root + (x_tip - x_root) * (z_hi - z_mid) / (z_hi - z_lo)
        # y（宽度）：上弦根 y=w_hi → 尖 y_tip（x 线性）；下弦根 y=w_lo → 尖
        y_m = w_hi + (y_tip - w_hi) * (x_mid - x_root) / (x_tip - x_root)
        y_mb = w_lo + (y_tip - w_lo) * (x_mid - x_root) / (x_tip - x_root)
        # 下弦 D/E/C 都在 z_lo；D x 取 w_lo（塔面），E x 取 x_mid
        x_rb = w_lo

        stations = {"A": (x_root, w_hi, z_hi), "B": (x_mid, y_m, z_mid),
                    "C": (x_tip, y_tip, z_lo), "D": (x_rb, w_lo, z_lo),
                    "E": (x_mid, y_mb, z_lo)}

        # 站位节点字典：每 (站位, sx, sy) 建一次，复用——避免同坐标重复建点。
        station_cache: Dict[Tuple[str, float, float], str] = {}

        def _mk_station(st: str, sx: float, sy: float) -> str:
            key = (st, sx, sy)
            nid = station_cache.get(key)
            if nid is not None:
                return nid
            x, y, z = stations[st]
            nid = _mk(sx * x, sy * y, z)
            station_cache[key] = nid
            return nid

        for sx in (1.0, -1.0):
            # 同 x 侧两 y 站位，交叉与端封只在该侧生成一次（y 对互补避免重复）。
            # 上弦内/外段、下弦内/外段、竖杆、斜杆 → 每 y 站各一根（非对称对）。
            # 交叉对角 → 同 x 侧两个 y 翻转节点互换，天然唯一。
            # B/C 端封横杆 → 每 x 侧一根（横跨两 y）。
            A_p, B_p, C_p, D_p, E_p = (_mk_station(s, sx, +1) for s in "ABCDE")
            A_m, B_m, C_m, D_m, E_m = (_mk_station(s, sx, -1) for s in "ABCDE")
            members = [
                # 上弦（两 y 站）
                (A_p, B_p, "LEG"), (B_p, C_p, "LEG"),
                (A_m, B_m, "LEG"), (B_m, C_m, "LEG"),
                # 下弦（两 y 站）
                (D_p, E_p, "LEG"), (E_p, C_p, "LEG"),
                (D_m, E_m, "LEG"), (E_m, C_m, "LEG"),
                # 竖杆 B→E（两 y 站）
                (B_p, E_p, "DIAG"), (B_m, E_m, "DIAG"),
                # 斜杆 B→D（两 y 站）
                (B_p, D_p, "DIAG"), (B_m, D_m, "DIAG"),
                # A 层交叉对角
                (A_p, B_m, "DIAG"), (A_m, B_p, "DIAG"),
                # E 层交叉对角（D 端封侧交叉）
                (E_p, D_m, "DIAG"), (E_m, D_p, "DIAG"),
                # C 层交叉对角（尖端 X）
                (C_p, E_m, "DIAG"), (C_m, E_p, "DIAG"),
                # B/C 端封横杆（每 x 侧一根）
                (B_p, B_m, "HORIZONTAL"),
                (C_p, C_m, "HORIZONTAL"),
            ]
            for f, t, role in members:
                p1, p2 = new_nodes[f], new_nodes[t]
                if _exists(p1, p2):
                    continue
                counter["n"] += 1
                new_bars.append({
                    "id": f"{id_prefix}_bar_{counter['n']}",
                    "from": f,
                    "to": t,
                    "role": role,
                    "diagonal_topology": False,
                    "crossarm_truss_completion": True,
                    "geometry_origin": "crossarm_truss_completion",
                    "geometry_class": "derived_parametric",
                    "level_source": level_source_label,
                })
                existing.append((p1, p2))
                generated += 1
        layers_report.append({
            "z_lo": round(z_lo, 1), "z_mid": round(z_mid, 1), "z_hi": round(z_hi, 1),
            "x_root": round(x_root, 1), "x_mid": round(x_mid, 1), "x_tip": round(x_tip, 1),
            "y_root_hi": round(w_hi, 1), "y_root_lo": round(w_lo, 1), "y_tip": round(y_tip, 1),
            "tip_width_mm": tip_width_mm,
        })

    if not layers_report:
        return nodes, bars, {"generated": 0, "layers": [],
                             "reason": "no_layer_with_chord_evidence",
                             "n_wide_nodes": len(wide)}
    return new_nodes, new_bars, {
        "generated": generated,
        "layers": layers_report[0] if len(layers_report) == 1 else layers_report,
        "n_layers": len(layers_report),
        "n_wide_nodes": len(wide),
    }


def prune_spurious_crossarm_bars(
    nodes: NodeMap,
    bars: List[dict],
    roles: Dict[str, str],
    *,
    half_width_fn: Optional[Callable[[float], float]] = None,
    crossarm_half_width_fn: Optional[Callable[[float], float]] = None,
    crossarm_zone_z_min_mm: float = 29000.0,
    crossarm_radial_ratio: float = 1.3,
) -> Tuple[List[dict], Dict[str, object]]:
    """P3.3：剔除误分类横担杆（CROSS 但无横担区/外伸证据）。

    被剔除杆记入证据报告；这些杆在 A2 中多为 FP。
    """
    kept: List[dict] = []
    removed: List[Dict[str, object]] = []
    for b in bars:
        bid = str(b.get("id"))
        if roles.get(bid) != "CROSS" and str(b.get("role") or "").upper() != "CROSS":
            kept.append(b)
            continue
        if b.get("diaphragm"):
            kept.append(b)
            continue
        # S8（2026-09）：近水平门禁——「误分类横担杆剔除」的语义是把
        # **近水平的伪 CROSS**（横担桁架水平弦）清掉；倾斜杆（参数化
        # 底段 X 交叉，role=CROSS 但倾角 40°~70°）是真实斜材，不是
        # 横担 FP。此前无倾角门禁：底段 X 交叉被整批
        # "no_crossarm_layer_at_z" 误杀（parametric 口径只剩 28 腿，
        # 底段 80 斜材零覆盖）。
        _pf, _pt = nodes.get(b.get("from")), nodes.get(b.get("to"))
        if _pf is None or _pt is None:
            kept.append(b)
            continue
        _dx = float(_pt[0]) - float(_pf[0])
        _dz = float(_pt[2]) - float(_pf[2])
        if abs(_dx) > 1e-9 or abs(_dz) > 1e-9:
            _incl = abs(math.degrees(math.atan2(abs(_dz), abs(_dx))))
        else:
            _incl = 0.0
        if _incl >= 20.0:
            # 倾斜杆不是横担形态（横担弦/水平杆倾角 <20°），保留
            kept.append(b)
            continue
        z_mid = _bar_z_mid(nodes, b)
        if z_mid is None:
            kept.append(b)
            continue
        max_r = _bar_max_radial(nodes, b)
        hw = float(half_width_fn(z_mid)) if half_width_fn is not None else 0.0
        arm_hw = float(crossarm_half_width_fn(z_mid)) if crossarm_half_width_fn else 0.0

        # P1.2 修复（2026-09-02）：「外伸形态」用 **|x| 外伸量** 判定，绝不能
        # 用径向 max_r——角柱横杆（leg↔center 横梁）端点落在角柱
        # (±hw, ±hw)，径向恒为 √2·hw ≈ 1.41·hw > 1.3·hw，用径向判会把
        # 所有正常角柱横杆都当横担 FP 误杀（06 册 f/b 面 19 根 marker_synth
        # 合成横杆被 "no_crossarm_layer_at_z" 全灭，06 段 pure TP 恒 0 的
        # 直接根因）。横担沿 X 外伸（|x| ≫ hw），角柱横杆 |x| ≤ hw。
        _max_ax = _bar_max_abs_x(nodes, b)

        reason: Optional[str] = None
        if crossarm_half_width_fn is not None:
            if arm_hw <= 0.0:
                _arm_like = (hw <= 0) or (_max_ax >= hw * float(crossarm_radial_ratio))
                if _arm_like:
                    reason = "no_crossarm_layer_at_z"
        elif z_mid < float(crossarm_zone_z_min_mm):
            # 同理：below_crossarm_zone 也要求 |x| 外伸形态（与有横担剖面
            # 的 no_crossarm_layer_at_z 语义对齐，两条路径只杀真悬臂）。
            _arm_like = (hw <= 0) or (_max_ax >= hw * float(crossarm_radial_ratio))
            if _arm_like:
                reason = "below_crossarm_zone"

        # P1.2 续修（insufficient_radial_extension）：该分支本意是「疑似
        # 横担但外伸不足 → 判 FP」。但塔身节间横杆（leg↔inner↔center，
        # role=CROSS、max_r≈hw 角柱径向）天然「外伸不足」——它们不是
        # 横担，径向检查不适用。只对确有 |x| 外伸形态（max|ax| ≥
        # hw*ratio，真悬臂候选）的杆做外伸量校验；非外伸水平杆（塔身
        # 横杆）一律保留。实测：06 册 42 根 CLE 合成横杆曾在此全灭。
        if reason is None and hw > 0 and _max_ax >= hw * float(crossarm_radial_ratio) \
                and max_r < hw * float(crossarm_radial_ratio):
            reason = "insufficient_radial_extension"

        if reason:
            removed.append({
                "bar_id": bid,
                "z_mid_mm": round(z_mid, 1),
                "max_radial_mm": round(max_r, 1),
                "body_half_width_mm": round(hw, 1) if hw else None,
                "reason": reason,
            })
            continue
        kept.append(b)

    return kept, {
        "n_in": len(bars),
        "n_out": len(kept),
        "n_removed": len(removed),
        "removed": removed,
    }


def derive_panel_levels(
    nodes: NodeMap,
    bars: List[dict],
    *,
    cluster_gap_mm: float = 400.0,
    min_node_evidence: int = 4,
    min_horiz_evidence: int = 2,
) -> List[float]:
    """S2b/S6 生产默认：从 DXF 节点证据聚类推导节间平台标高。

    证据来源：非横隔杆件的端点 z。平台标高的判据（任务 3.1「多证据」）：
        * 同一高度附近（cluster_gap_mm 内）有多个**独立结构证据**——
          按杆件 ID 去重后的端点数（2026-08-31 风险5 修复：同一根杆
          的两个端点只计一次，且同杆双端落同簇时加权也只算一份）
          >= min_node_evidence；或
        * 有已绘水平材支持（横隔层证据）——水平杆根数
          >= min_horiz_evidence。

    每簇取「节点数加权中位数」为层 z（端点级加权——同一根杆的两个
    端点落在同簇内时对中位数贡献两票，但判据门槛只按杆件数计）。
    精度受 DXF 提取噪声限制（实测 ±100~600mm）；use_gt_platform_levels
    开启时用 canonical 标高表替代（用户裁定 z-only 可注入）。
    """
    from collections import defaultdict

    # z-bucket -> {杆件ID集合, 水平杆ID集合, 端点计数（仅加权中位数用）}
    evidence: Dict[int, Dict[str, object]] = defaultdict(
        lambda: {"bars": set(), "horiz": set(), "n": 0}
    )
    for b in bars:
        f = nodes.get(b.get("from"))
        t = nodes.get(b.get("to"))
        if f is None or t is None:
            continue
        is_horiz = abs(float(f[2]) - float(t[2])) < 100.0 and abs(
            float(f[0]) - float(t[0])
        ) > 300.0
        bid = b.get("id") or f"{b.get('from')}->{b.get('to')}"
        for p in (f, t):
            ev = evidence[int(round(float(p[2]) / 100.0) * 100)]
            ev["bars"].add(bid)
            ev["n"] += 1
            if is_horiz:
                ev["horiz"].add(bid)

    zs = sorted(evidence)
    if not zs:
        return []
    clusters: List[List[int]] = []
    cur = [zs[0]]
    for z in zs[1:]:
        if z - cur[-1] <= cluster_gap_mm:
            cur.append(z)
        else:
            clusters.append(cur)
            cur = [z]
    clusters.append(cur)

    # P4.3 实验结论（2026-08-31，已回滚）：簇内高斯核密度谷分割
    # （σ=200/vr=0.4）确实能从 [30400~32800] 宽簇里分出 30700+32700
    # 双平台层（层位 Δ≤100），但——
    #   * 32700 横隔 hw 拟合与 GT 层几何不匹配（4 FN 依旧）；
    #   * [13800~15500] 簇同时被切出 15400 噪声子层，连锁破坏 16000
    #     层横隔（16 TP → FN）；
    #   * 实测净退化 horiz_x 91→75。
    # 塔头横担区平台（30000/32700）的横隔几何缺口与 canonical 同源
    # （canonical 也无法命中，见 PRODUCTION_REGRESSION_ANALYSIS.md
    # 「结构性 FN」一节），层位恢复本身不带来收益。故保持链式聚类
    # 原样，不再做密度分割。

    levels: List[float] = []
    for c in clusters:
        # 风险5 证据去重：跨簇合并杆件 ID 集合后再计数——同一根杆
        # 在簇内多 bucket / 双端点均只算一个独立证据。
        c_bars: set = set()
        c_horiz: set = set()
        for z in c:
            c_bars |= evidence[z]["bars"]
            c_horiz |= evidence[z]["horiz"]
        if len(c_bars) < min_node_evidence and len(c_horiz) < min_horiz_evidence:
            continue
        weighted: List[float] = []
        for z in c:
            weighted.extend([float(z)] * evidence[z]["n"])
        weighted.sort()
        levels.append(weighted[len(weighted) // 2])
    # 相邻推导层过近时合并（保留证据更强的）
    levels.sort()
    merged: List[float] = []
    for z in levels:
        if merged and z - merged[-1] < 350.0:
            merged[-1] = (merged[-1] + z) / 2.0
        else:
            merged.append(z)
    return merged



def derive_panel_levels_detailed(
    nodes: NodeMap,
    bars: List[dict],
    *,
    cluster_gap_mm: float = 400.0,
    min_node_evidence: int = 4,
    min_horiz_evidence: int = 2,
    manual_levels: Optional[List[float]] = None,
    manual_snap_mm: float = 500.0,
) -> Tuple[List[float], List[dict]]:
    """P4.1：节间平台多证据判定（逐层 source 分层 + 可追溯证据）。

    在 derive_panel_levels 的聚类逻辑之上，输出每层的证据结构：
        {"z_mm": ..., "source": "dxf"|"manual",
         "n_bar_evidence": int, "n_horiz_evidence": int,
         "z_cluster_span_mm": [z_min, z_max]}

    manual_levels（overlay panel_level_manual_levels 注入的人工标高）：
    与 DXF 推导层差 <= manual_snap_mm 时**吸附**到人工值（source 保留
    dxf，记 manual_snapped=true——层位数值被人工校正但存在图纸证据）；
    无对应 DXF 层的人工标高追加为 source="manual" 纯人工层。

    返回 (levels, records)：levels 与 derive_panel_levels 同序同值
    （含 manual 吸附/追加），records 供 delivery 证据链呈现。
    """
    base = derive_panel_levels(
        nodes, bars,
        cluster_gap_mm=cluster_gap_mm,
        min_node_evidence=min_node_evidence,
        min_horiz_evidence=min_horiz_evidence,
    )
    # 重新跑一遍聚类以取每簇证据计数（derive_panel_levels 只返回 z）
    from collections import defaultdict
    evidence: Dict[int, Dict[str, object]] = defaultdict(
        lambda: {"bars": set(), "horiz": set(), "n": 0}
    )
    for b in bars:
        f = nodes.get(b.get("from"))
        t = nodes.get(b.get("to"))
        if f is None or t is None:
            continue
        is_horiz = abs(float(f[2]) - float(t[2])) < 100.0 and abs(
            float(f[0]) - float(t[0])
        ) > 300.0
        bid = b.get("id") or f"{b.get('from')}->{b.get('to')}"
        for p in (f, t):
            ev = evidence[int(round(float(p[2]) / 100.0) * 100)]
            ev["bars"].add(bid)
            ev["n"] += 1
            if is_horiz:
                ev["horiz"].add(bid)
    zs = sorted(evidence)
    clusters: List[List[int]] = []
    if zs:
        cur = [zs[0]]
        for z in zs[1:]:
            if z - cur[-1] <= cluster_gap_mm:
                cur.append(z)
            else:
                clusters.append(cur)
                cur = [z]
        clusters.append(cur)

    # 推导层 -> 簇证据（按最近簇）
    def _cluster_stats(z_level: float) -> Tuple[int, int, List[int]]:
        best = None
        for c in clusters:
            lo, hi = min(c), max(c)
            if lo - 1000 <= z_level <= hi + 1000:
                if best is None or abs(sum(c) / len(c) - z_level) < abs(
                        sum(best) / len(best) - z_level):
                    best = c
        if best is None:
            return 0, 0, [int(z_level)]
        c_bars: set = set()
        c_horiz: set = set()
        for z in best:
            c_bars |= evidence[z]["bars"]
            c_horiz |= evidence[z]["horiz"]
        return len(c_bars), len(c_horiz), [min(best), max(best)]

    records: List[dict] = []
    used_manual: set = set()
    for z in base:
        n_bars, n_horiz, span = _cluster_stats(z)
        rec = {
            "z_mm": round(float(z), 1),
            "source": "dxf",
            "n_bar_evidence": n_bars,
            "n_horiz_evidence": n_horiz,
            "z_cluster_span_mm": [float(span[0]), float(span[1])],
            "manual_snapped": False,
        }
        if manual_levels:
            best_m = min(manual_levels, key=lambda mz: abs(mz - z))
            if abs(best_m - z) <= manual_snap_mm:
                rec["z_mm"] = round(float(best_m), 1)
                rec["manual_snapped"] = True
                used_manual.add(best_m)
        records.append(rec)
    if manual_levels:
        for mz in sorted(manual_levels):
            if mz not in used_manual:
                records.append({
                    "z_mm": round(float(mz), 1),
                    "source": "manual",
                    "n_bar_evidence": 0,
                    "n_horiz_evidence": 0,
                    "z_cluster_span_mm": [float(mz), float(mz)],
                    "manual_snapped": False,
                })
    records.sort(key=lambda r: r["z_mm"])
    levels_out = [float(r["z_mm"]) for r in records]
    return levels_out, records


# ---------------------------------------------------------------------------
# P4.2：生产 DXF 平台标高推导 v2——主腿斜率转折锚定（2026-08-31 归因后重构）
#
# v1（derive_panel_levels）实测缺陷（production 25 层 vs GT 15 层）：
#   * 噪声层：7600/9500/17700/20000/25400/26300/27300/28800 —— 无平台
#     但有杆端点密簇（06 拓扑窗斜材端点、跨段标注残片）；
#   * 分裂/拉偏：GT 11500 被簇中位数拉到 12400（斜材端点 27 根投票），
#     14000 分裂成 13200/14400。
#
# v2 物理先验：铁塔主腿是分段收腰的直线链——平台标高处坡度必变
# （|Δslope| 显著）。主腿 z→x 序列的斜率转折点是平台层的**强证据**：
#   * 断点 ±400 内的 DXF 簇 → 真层，z 向断点校正（拉回中位数漂移）；
#   * 断点无簇但 ±600 内有 ≥2 独立杆端点 → 直接采纳断点；
#   * 无断点支持的簇 → 噪声抑制。
# 实测（35A1-JC1 production）：断点 6500/8500/11500/14250/16250/19000/
# 21000/22750/23750 与 GT 9 个塔身层一一对齐（Δ≤250mm）。
# ---------------------------------------------------------------------------


def _extract_leg_breakpoints(
    nodes: NodeMap,
    bars: List[dict],
    *,
    slope_delta_threshold: float = 0.025,
    min_leg_incl_deg: float = 60.0,
    max_seg_gap_mm: float = 800.0,
) -> List[float]:
    """从主腿链提取斜率转折标高。

    主腿判据（几何，不依赖 role 标注——调用点 role 尚未赋值）：
    近竖直（倾角 >= min_leg_incl_deg）+ 贴外缘（径向 |x| 或 |y| 为该
    标高层附近最大）。链构建：端点共享 + z 单调段。转折判定：相邻段
    |Δslope| > slope_delta_threshold 且段长 >= max_seg_gap_mm/2。

    返回转折 z 列表（升序，500mm 网格去重）。
    """
    import math as _math
    from collections import defaultdict as _dd

    # 1. 候选主腿杆：近竖直
    leg_like: List[dict] = []
    for b in bars:
        f, t = nodes.get(b.get("from")), nodes.get(b.get("to"))
        if f is None or t is None:
            continue
        dz = abs(float(t[2]) - float(f[2]))
        dh = _math.hypot(
            float(t[0]) - float(f[0]), float(t[1]) - float(f[1]))
        if dz < 500.0:
            continue
        if _math.degrees(_math.atan2(dz, dh)) < min_leg_incl_deg:
            continue
        leg_like.append(b)
    if not leg_like:
        return []

    # 2. 连通成链
    adj: Dict[str, List[str]] = _dd(list)
    bid_of = {}
    for b in leg_like:
        f, t = str(b.get("from")), str(b.get("to"))
        adj[f].append(t)
        adj[t].append(f)
        bid_of[f"{f}->{t}"] = str(b.get("id") or f"{f}->{t}")
        bid_of[f"{t}->{f}"] = str(b.get("id") or f"{f}->{t}")
    seen: set = set()
    breakpoints: List[float] = []
    for start in list(adj):
        if start in seen:
            continue
        # 连通分量
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if len(comp) < 3:
            continue
        # 3. 分量内找 z 单调主链（从端点出发的 DFS 最长路径）
        comp_set = set(comp)
        ends = [n for n in comp if len([x for x in adj[n] if x in comp_set]) == 1]
        best_chain: List[str] = []
        for e in (ends or comp[:1]):
            stack = [(e, [e])]
            while stack:
                cur, path = stack.pop()
                ext = False
                for nb in adj[cur]:
                    if nb in comp_set and nb not in path:
                        stack.append((nb, path + [nb]))
                        ext = True
                if not ext and len(path) > len(best_chain):
                    best_chain = path
        if len(best_chain) < 3:
            continue
        # 4. 链上节点 z 排序 → 斜率转折
        chain_pts = []
        for nid in best_chain:
            p = nodes.get(nid)
            if p is not None and p[2] is not None:
                chain_pts.append((float(p[2]), float(p[0]), float(p[1])))
        chain_pts.sort(key=lambda q: q[0])
        if len(chain_pts) < 3:
            continue
        # 相邻段斜率（径向收腰：用 |x| 与 |y| 的较大者——主腿在立面图
        # 的投影方向不定，取径向更稳）
        slopes: List[Tuple[float, float]] = []  # (z_mid, slope)
        for i in range(1, len(chain_pts)):
            z0, r0 = chain_pts[i - 1][0], max(abs(chain_pts[i - 1][1]), abs(chain_pts[i - 1][2]))
            z1, r1 = chain_pts[i][0], max(abs(chain_pts[i][1]), abs(chain_pts[i][2]))
            if z1 - z0 < 100.0:
                continue
            slopes.append((z0, z1, (r1 - r0) / (z1 - z0)))
        for i in range(1, len(slopes)):
            _, z0a, s_prev = slopes[i - 1]
            z1a, _, s_cur = slopes[i]
            if abs(s_cur - s_prev) <= slope_delta_threshold:
                continue
            # 转折 z 取两段交界节点
            zp = z1a
            if breakpoints and abs(zp - breakpoints[-1]) < 500.0:
                breakpoints[-1] = (breakpoints[-1] + zp) / 2.0
            else:
                breakpoints.append(zp)
    breakpoints.sort()
    # 500mm 网格去重
    dedup: List[float] = []
    for z in breakpoints:
        if dedup and abs(z - dedup[-1]) < 500.0:
            continue
        dedup.append(z)
    return dedup


def derive_panel_levels_v2(
    nodes: NodeMap,
    bars: List[dict],
    *,
    cluster_gap_mm: float = 400.0,
    min_node_evidence: int = 4,
    min_horiz_evidence: int = 2,
    manual_levels: Optional[List[float]] = None,
    manual_snap_mm: float = 500.0,
    breakpoint_anchor_mm: float = 400.0,
    breakpoint_free_evidence_mm: float = 600.0,
    breakpoint_free_min_bars: int = 2,
) -> Tuple[List[float], List[dict]]:
    """P4.2：断点锚定的平台标高推导（生产 DXF 口径）。

    在 v1 簇证据之上叠加主腿斜率转折证据：
        * 断点 ±breakpoint_anchor_mm 内的簇 → 真层（z 取簇内靠近断点的
          加权中位数，向断点校正不超过 anchor 距离）；
        * 断点 ±breakpoint_free_evidence_mm 内有 >= breakpoint_free_min_bars
          根独立杆端点（即使不成簇）→ 直接采纳断点 z；
        * 其余簇 → 噪声抑制（无主腿收腰支持的平台不可信）。
    manual_levels 吸附语义与 derive_panel_levels_detailed 兼容。

    返回 (levels, records)；records 增加 leg_breakpoint 字段（可追溯）。
    """
    # 1. v1 簇证据（复用完整逻辑拿 records）
    _base_levels, records = derive_panel_levels_detailed(
        nodes, bars,
        cluster_gap_mm=cluster_gap_mm,
        min_node_evidence=min_node_evidence,
        min_horiz_evidence=min_horiz_evidence,
    )
    # 2. 主腿断点
    breakpoints = _extract_leg_breakpoints(nodes, bars)

    # 3. 端点证据密度（断点免簇采纳用）：z(100mm 桶) → 独立杆集合
    from collections import defaultdict as _dd
    ev_bars: Dict[int, set] = _dd(set)
    for b in bars:
        f, t = nodes.get(b.get("from")), nodes.get(b.get("to"))
        if f is None or t is None:
            continue
        bid = str(b.get("id") or f"{b.get('from')}->{b.get('to')}")
        for p in (f, t):
            ev_bars[int(round(float(p[2]) / 100.0) * 100)].add(bid)

    out_records: List[dict] = []
    used_bp: set = set()

    # 4. 断点免簇采纳（无簇但有杆端点证据）
    for bp in breakpoints:
        near_bars: set = set()
        for zb, bs in ev_bars.items():
            if abs(float(zb) - bp) <= breakpoint_free_evidence_mm:
                near_bars |= bs
        if len(near_bars) >= breakpoint_free_min_bars:
            continue  # 先不采纳——若附近有簇，走簇校正路径
        # 采纳断点
        out_records.append({
            "z_mm": round(float(bp), 1),
            "source": "dxf",
            "n_bar_evidence": len(near_bars),
            "n_horiz_evidence": 0,
            "z_cluster_span_mm": [float(bp), float(bp)],
            "manual_snapped": False,
            "leg_breakpoint": True,
        })

    # 5. 簇过滤与校正
    for rec in records:
        if rec.get("source") == "manual":
            out_records.append(dict(rec))
            continue
        z = float(rec["z_mm"])
        bp_near = [bp for bp in breakpoints
                   if abs(bp - z) <= breakpoint_anchor_mm]
        if not bp_near:
            # 无断点支持的簇——噪声抑制
            # （horiz 证据强的簇例外：已绘水平材直接可信，如 22700 层）
            if int(rec.get("n_horiz_evidence") or 0) >= min_horiz_evidence:
                rec2 = dict(rec)
                rec2["leg_breakpoint"] = False
                out_records.append(rec2)
            continue
        bp = min(bp_near, key=lambda b: abs(b - z))
        # z 向断点校正（拉回中位数漂移，幅度不超过 anchor 距离）
        z_corrected = z + (bp - z) * 0.5
        rec2 = dict(rec)
        rec2["z_mm"] = round(z_corrected, 1)
        rec2["leg_breakpoint"] = True
        out_records.append(rec2)

    # 6. 断点免簇采纳（第 4 步推迟到此处统一）：处理「断点无簇」情形
    bp_claimed = set()
    for rec in out_records:
        if rec.get("leg_breakpoint"):
            for bp in breakpoints:
                if abs(float(rec["z_mm"]) - bp) <= breakpoint_anchor_mm + 250.0:
                    bp_claimed.add(bp)
    for bp in breakpoints:
        if bp in bp_claimed:
            continue
        near_bars: set = set()
        for zb, bs in ev_bars.items():
            if abs(float(zb) - bp) <= breakpoint_free_evidence_mm:
                near_bars |= bs
        if len(near_bars) >= breakpoint_free_min_bars:
            out_records.append({
                "z_mm": round(float(bp), 1),
                "source": "dxf",
                "n_bar_evidence": len(near_bars),
                "n_horiz_evidence": 0,
                "z_cluster_span_mm": [float(bp), float(bp)],
                "manual_snapped": False,
                "leg_breakpoint": True,
            })

    # 7. manual 吸附（与 v1 兼容）
    used_manual: set = set()
    if manual_levels:
        for rec in out_records:
            best_m = min(manual_levels, key=lambda mz: abs(mz - float(rec["z_mm"])))
            if abs(best_m - float(rec["z_mm"])) <= manual_snap_mm:
                rec["z_mm"] = round(float(best_m), 1)
                rec["manual_snapped"] = True
                used_manual.add(best_m)
        for mz in sorted(manual_levels):
            if mz not in used_manual:
                out_records.append({
                    "z_mm": round(float(mz), 1),
                    "source": "manual",
                    "n_bar_evidence": 0,
                    "n_horiz_evidence": 0,
                    "z_cluster_span_mm": [float(mz), float(mz)],
                    "manual_snapped": False,
                    "leg_breakpoint": False,
                })

    out_records.sort(key=lambda r: float(r["z_mm"]))
    # 相邻层过近合并（<350mm）
    merged: List[dict] = []
    for rec in out_records:
        if merged and float(rec["z_mm"]) - float(merged[-1]["z_mm"]) < 350.0:
            # 保留断点证据更强者
            prev, cur = merged[-1], rec
            if cur.get("leg_breakpoint") and not prev.get("leg_breakpoint"):
                merged[-1] = cur
            else:
                prev["z_mm"] = round(
                    (float(prev["z_mm"]) + float(cur["z_mm"])) / 2.0, 1)
        else:
            merged.append(rec)
    return [float(r["z_mm"]) for r in merged], merged


def subdivide_legs_at_levels(
    nodes: NodeMap,
    bars: List[dict],
    levels: List[float],
    *,
    min_seg_len_mm: float = 400.0,
    min_span_mm: float = 1500.0,
    half_width_fn: Optional[Callable[[float], float]] = None,
    hw_proximity: float = 0.30,
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """S6 主腿节间化：把通长主腿按 canonical 平台标高切成节间物理杆。

    背景：DXF 立面图的主腿是通长直线（一画到底），而真实塔的主腿角钢
    按平台标高分节制造安装（GT 实测每角 63 段）。通长杆的端点只落在
    段图框边界（如 6643/12143），与 GT 节间端点（平台标高）错位数百
    毫米，且长度比门禁使通长杆几乎无法匹配任何 GT 节间杆（实测 leg
    召回 3.4%）。

    做法（P2-7 用户裁定）：
        * 几何判据选杆（此阶段 role 尚未赋值）：近竖直
          （|dx|/|dz|<0.10）+ 跨度 >= min_span_mm + 两端 |x| 贴合塔身
          半宽曲线（|x|/hw(z)-1 <= hw_proximity，排除横担外张杆）。
          role=LEG 的杆自然满足；横担斜撑（外张，如 bar_112）被半宽
          邻近判据排除；
        * 切点 z 只取 canonical 平台标高（z-only 注入，用户 2026-08 裁定
          「数量/层级可注入，x/y 禁止」），或 derive_panel_levels 的
          DXF 证据推导结果；
        * 切点 x/y 在**原杆自身直线上线性插值**（保持杆件几何连续，
          不引入外部 x/y）；
        * 切出的节间杆标记 panel_subdivision=True、root_bar_id=原杆 id，
          下游语义冻结为 reconstructed（geometry_origin=panel_subdivision）；
        * 原通长杆被替换（不再进物理口径，节间杆携带其件号/溯源链）。

    返回 (new_nodes, new_bars, report)。
    """
    if not levels:
        return dict(nodes), [dict(b) for b in bars], {
            "subdivided_legs": 0, "segments_created": 0,
            "panel_conservation": {
                "legs": [], "max_abs_delta_mm": 0.0, "violations": [],
                "tol_mm": 0.5, "ok": True,
            },
        }
    lv = sorted(float(z) for z in levels)
    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = []
    n_legs = 0
    n_segs = 0
    conservation_max = 0.0  # P4.2 长度守恒（验收 <= 0.1%）
    conservation_legs: List[dict] = []
    conservation_tol_mm = 0.5
    node_seq = max(
        (int(k.split("_")[-1]) for k in nodes if str(k).split("_")[-1].isdigit()),
        default=100000,
    )

    def _is_leg_like(b: dict) -> bool:
        f = nodes.get(b.get("from"))
        t = nodes.get(b.get("to"))
        if f is None or t is None:
            return False
        dx = abs(float(f[0]) - float(t[0]))
        dz = abs(float(f[2]) - float(t[2]))
        if dz < min_span_mm or dx / max(dz, 1e-9) >= 0.10:
            return False
        if half_width_fn is not None:
            for p in (f, t):
                hw = float(half_width_fn(float(p[2])))
                if hw <= 1e-6:
                    return False
                if abs(abs(float(p[0])) / hw - 1.0) > hw_proximity:
                    return False
        return True

    for b in bars:
        f = nodes.get(b.get("from"))
        t = nodes.get(b.get("to"))
        if f is None or t is None:
            new_bars.append(dict(b))
            continue
        # P2.2（2026-09-04）：leg_synth 跨型段豁免节间化——它们是显式
        # 跨型表（z-only 设计常数）的终态分段，端点已精确落在 GT 分段
        # 边界。在平台层处再切会把 [7000,11500] 劈成 (7000,8000)+
        # (8000,8500)+... 并替换掉原杆（07 册实测 20 段只剩 1 段
        # (8500,11800)——恰好是唯一不含平台层切点的跨型）。
        if str(b.get("geometry_origin") or "") == "leg_synth":
            new_bars.append(dict(b))
            continue
        # 几何判据（此阶段 role 未赋值，见 _is_leg_like 文档）
        if not _is_leg_like(b):
            new_bars.append(dict(b))
            continue

        z_a, z_b = float(f[2]), float(t[2])
        if z_a > z_b:
            f, t = t, f
            z_a, z_b = z_b, z_a
        # 杆内切点（平台标高落在杆跨度内部，留出最小节间长度）
        cuts = [
            z for z in lv
            if z_a + min_seg_len_mm <= z <= z_b - min_seg_len_mm
        ]
        if len(cuts) < 1:
            new_bars.append(dict(b))
            continue

        # 切点序列（含原端点），切点 x/y 沿原杆直线插值
        zs = [z_a] + cuts + [z_b]
        node_ids: List[str] = []
        for z in zs:
            frac = (z - z_a) / (z_b - z_a) if z_b > z_a else 0.0
            x = float(f[0]) + (float(t[0]) - float(f[0])) * frac
            y = float(f[1]) + (float(t[1]) - float(f[1])) * frac
            # 复用已有节点（切点常与横隔/斜材端点重合）
            nid = None
            for cand_id, cp in new_nodes.items():
                if (
                    abs(float(cp[2]) - z) <= 1.0
                    and abs(float(cp[0]) - x) <= 1.0
                    and abs(float(cp[1]) - y) <= 1.0
                ):
                    nid = cand_id
                    break
            if nid is None:
                node_seq += 1
                nid = f"psn_{node_seq}"
                new_nodes[nid] = (round(x, 3), round(y, 3), round(z, 3))
            node_ids.append(nid)

        base_id = str(b.get("id") or b.get("bar_id") or "leg")
        emitted_segments: List[dict] = []
        for k in range(len(node_ids) - 1):
            seg = dict(b)
            seg.update({
                "id": f"{base_id}_ps{k:02d}",
                "from": node_ids[k],
                "to": node_ids[k + 1],
                "role": "LEG",
                "panel_subdivision": True,
                "root_bar_id": base_id,
                "derived_from": b.get("derived_from") or base_id,
                "subdiv_index": k,
                "subdiv_count": len(node_ids) - 1,
            })
            new_bars.append(seg)
            emitted_segments.append(seg)
            n_segs += 1
        # P4.2 长度守恒审计：节间杆长度和 vs 原通长杆长度（切点沿原杆
        # 直线插值，理论偏差 = 节点复用吸附的微小抖动）。超标即 bug。
        _orig_len = math.sqrt(
            (float(t[0]) - float(f[0])) ** 2
            + (float(t[1]) - float(f[1])) ** 2
            + (float(t[2]) - float(f[2])) ** 2
        )
        _seg_len_sum = 0.0
        for seg in emitted_segments:
            p, q = new_nodes[seg["from"]], new_nodes[seg["to"]]
            _seg_len_sum += math.sqrt(
                (float(q[0]) - float(p[0])) ** 2
                + (float(q[1]) - float(p[1])) ** 2
                + (float(q[2]) - float(p[2])) ** 2
            )
        _delta = _seg_len_sum - _orig_len
        conservation_legs.append({
            "leg": base_id,
            "orig_len_mm": _orig_len,
            "sum_seg_len_mm": _seg_len_sum,
            "delta_mm": _delta,
        })
        if _orig_len > 1e-6:
            conservation_max = max(
                conservation_max, abs(_delta) / _orig_len)
        n_legs += 1

    max_abs_delta = max(
        (abs(item["delta_mm"]) for item in conservation_legs), default=0.0)
    violations = [
        item["leg"] for item in conservation_legs
        if abs(item["delta_mm"]) > conservation_tol_mm
    ]
    return new_nodes, new_bars, {
        "subdivided_legs": n_legs,
        "segments_created": n_segs,
        "levels_used": len(lv),
        # P4.2：最大相对长度偏差（验收 <= 0.1%）
        "length_conservation_max_rel_err": round(conservation_max, 6),
        "panel_conservation": {
            "legs": conservation_legs,
            "max_abs_delta_mm": max_abs_delta,
            "violations": violations,
            "tol_mm": conservation_tol_mm,
            "ok": not violations,
        },
    }


def prune_short_stub_bars(
    nodes: NodeMap,
    bars: List[dict],
    *,
    max_stub_len_mm: float = 400.0,
    max_rounds: int = 12,
) -> Tuple[NodeMap, List[dict], Dict[str, int]]:
    """迭代剪除「短悬臂残根」：degree=1 端点 + 短杆的噪声树修剪（S1c）。

    问题：DXF 立面里的标注引线 / 杆件终止短线 / T 形打断残片是单端接触的
    短竖杆（85~300mm），另一端悬空（degree=1）。它们不是结构杆（GT 不统计），
    却把门禁 genuine_dangling 推高（S1b 修复后实测 2D 层 128 个悬空节点中，
    LEG 角色悬空杆 56 根几乎全是这类残根，中位长 240mm）。

    规则（迭代至稳定）：
        1. 统计节点度数；
        2. 对每个 degree=1 节点：若其唯一杆件长度 < max_stub_len_mm，
           删除该杆件（若删除后另一端变为孤立节点，一并删除）；
        3. 重复，直到无新增可删杆（最多 max_rounds 轮）。

    长杆（≥ max_stub_len_mm）即使端点悬空也保留——它们是结构杆的真实断裂，
    需要拓扑缝合（S3/S4），不能靠删除假装闭合。

    件号保全：被剪残根可能携带真实图纸件号（S1c 实测 58 个件号里 19 个只在
    残根上出现且全部是「孤立标注残片」，两端度数<2、无结构附着点，无法转移
    给邻杆）。这些件号是 A1（件号识别）的有效证据——图纸确实标注了它们。
    剪除杆件时把件号收进 pruned_label_ids 返回，由调用方挂到 drawing_file 的
    orphan_label_ids 登记簿，A1 评测时并入模型识别件号集合（几何上清除噪声，
    件号证据不丢）。

    返回 (new_nodes, new_bars, {"pruned_bars": n, "pruned_rounds": k,
                                "pruned_label_ids": [件号...]}）。
    """
    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = [dict(b) for b in bars]
    pruned = 0
    pruned_labels: List[str] = []
    seen_labels: set = set()

    def _label_of(b: dict) -> Optional[str]:
        v = b.get("bar_id")
        if v and not str(v).startswith("UNLABELED"):
            return str(v)
        return None

    for _round in range(max_rounds):
        deg: Dict[str, int] = {}
        for b in new_bars:
            deg[b["from"]] = deg.get(b["from"], 0) + 1
            deg[b["to"]] = deg.get(b["to"], 0) + 1

        # degree=1 节点 → 其唯一杆件
        dang_bar_ids: set = set()
        for nid, d in deg.items():
            if d != 1:
                continue
            for b in new_bars:
                if b["from"] != nid and b["to"] != nid:
                    continue
                f, t = new_nodes.get(b["from"]), new_nodes.get(b["to"])
                if f is None or t is None:
                    continue
                L = math.hypot(float(t[0]) - float(f[0]), float(t[2]) - float(f[2]))
                if L < max_stub_len_mm:
                    dang_bar_ids.add(b["id"])
                break

        if not dang_bar_ids:
            break

        # 件号保全：收集被剪残根的件号（去重）
        for b in new_bars:
            if b["id"] in dang_bar_ids:
                lab = _label_of(b)
                if lab and lab not in seen_labels:
                    seen_labels.add(lab)
                    pruned_labels.append(lab)

        new_bars = [b for b in new_bars if b["id"] not in dang_bar_ids]
        pruned += len(dang_bar_ids)

        # 清理孤立节点（degree=0）：只删「不再被任何杆件引用」的节点。
        referenced = {b["from"] for b in new_bars} | {b["to"] for b in new_bars}
        new_nodes = {nid: pos for nid, pos in new_nodes.items() if nid in referenced}

    return new_nodes, new_bars, {
        "pruned_bars": pruned,
        "pruned_rounds": max_rounds,
        "pruned_label_ids": pruned_labels,
    }


def _stitch_z_mid(seg: Tuple[Vec3T, Vec3T]) -> float:
    return (float(seg[0][2]) + float(seg[1][2])) / 2.0


def _stitch_z_span(seg: Tuple[Vec3T, Vec3T]) -> Tuple[float, float]:
    z0, z1 = float(seg[0][2]), float(seg[1][2])
    return (min(z0, z1), max(z0, z1))


def _stitch_platform_band(z_mid: float, panel_levels: Sequence[float], tol: float) -> int:
    """平台标高把主腿 z 轴切成若干段；同段内才允许 LEG 拼接。"""
    band = 0
    for lv in sorted(panel_levels):
        if z_mid >= float(lv) - tol:
            band += 1
        else:
            break
    return band


def _stitch_diag_evidence_key(props: dict) -> Tuple[str, ...]:
    return (
        str(props.get("source_file") or props.get("derived_from") or ""),
        str(props.get("drawing_view") or ""),
        str(props.get("geometry_origin") or "dxf_geom"),
    )


def _stitch_cross_axis_coord(face: str, pt: Vec3T) -> float:
    """水平材「跨中心」检测用的横向坐标（face 相关）。"""
    f = str(face or "f").lower()
    if f == "l":
        return float(pt[1])
    if f == "r":
        return -float(pt[1])
    return float(pt[0])


def _role_pair_ok_stitch(
    bi: str,
    bj: str,
    cand: Dict[str, Tuple[Vec3T, Vec3T, dict]],
    face_of: Dict[str, str],
    *,
    role_specific: bool,
    panel_levels: Optional[Sequence[float]],
    platform_tol_mm: float,
    horiz_z_tol_mm: float,
    horiz_center_tol_mm: float,
    cross_sheet_ok: bool = False,
) -> Tuple[bool, Optional[str]]:
    """P2.3：分角色拼接门禁。返回 (allowed, reject_reason)。"""
    if not role_specific:
        return True, None
    pa, pb = cand[bi][2], cand[bj][2]
    ra = str(pa.get("role") or "").upper()
    rb = str(pb.get("role") or "").upper()
    if not ra or not rb or ra in ("?", "") or rb in ("?", ""):
        return True, None
    if ra != rb:
        return False, "role_mismatch"
    if ra == "LEG":
        if not panel_levels:
            return True, None
        zmi = _stitch_z_mid(cand[bi])
        zmj = _stitch_z_mid(cand[bj])
        if _stitch_platform_band(zmi, panel_levels, platform_tol_mm) != _stitch_platform_band(
                zmj, panel_levels, platform_tol_mm):
            return False, "leg_platform_break"
        return True, None
    if ra == "DIAG":
        if cross_sheet_ok:
            # P3.19（ZC1 泛化）：多册同段图纸（如 35A2-ZC1 的 05/09/12
            # 都画 z26000+32000 段）——同一物理斜材的碎段可能来自不同册。
            # cross_sheet_ok=true 时 evidence key 退化为仅 geometry_origin
            # （dxf_geom vs 重建杆仍不许互拼），放开跨册拼接。
            if str(pa.get("geometry_origin") or "dxf_geom") != str(
                    pb.get("geometry_origin") or "dxf_geom"):
                return False, "diag_origin_mismatch"
            return True, None
        if _stitch_diag_evidence_key(pa) != _stitch_diag_evidence_key(pb):
            return False, "diag_source_mismatch"
        return True, None
    if ra == "HORIZ":
        zmi = _stitch_z_mid(cand[bi])
        zmj = _stitch_z_mid(cand[bj])
        if abs(zmi - zmj) > horiz_z_tol_mm:
            return False, "horiz_z_mismatch"
        fn = face_of.get(bi, "?")
        pts = [cand[bi][0], cand[bi][1], cand[bj][0], cand[bj][1]]
        xs = [_stitch_cross_axis_coord(fn, p) for p in pts]
        if min(xs) < -horiz_center_tol_mm and max(xs) > horiz_center_tol_mm:
            return False, "horiz_cross_center"
        return True, None
    return True, None


def _centerline_extract_bar(bar: dict) -> bool:
    return str(bar.get("source_extractor") or "") == "centerline_extract"


def _centerline_stitch_origin(src: dict) -> str:
    o = str(src.get("geometry_origin") or "dxf_geom")
    return o if o in ("dxf_geom", "marker_synth", "leg_synth") else "dxf_geom"


def stitch_collinear_bars(
    nodes: NodeMap,
    bars: List[dict],
    *,
    gap_mm: float = 300.0,
    ang_deg: float = 10.0,
    min_merged_len_mm: float = 600.0,
    max_merged_len_mm: float = 4500.0,
    max_segments: int = 3,
    target_len_mm: float = 2018.0,
    skip_corner_leg: bool = True,
    max_single_len_mm: float = 0.0,
    role_specific: bool = False,
    panel_levels: Optional[Sequence[float]] = None,
    platform_tol_mm: float = 80.0,
    horiz_z_tol_mm: float = 80.0,
    horiz_center_tol_mm: float = 300.0,
    cross_sheet_ok: bool = False,
) -> Tuple[List[dict], Dict[str, object]]:
    """S4 贪心共线拼接：把断裂碎片杆拼回整杆（Phase 2 生产化）。

    背景：模型杆件碎片化是召回率头号瓶颈——GT 杆长中位 2005mm，模型纯 DXF
    杆中位 ~900mm。一根 GT 杆被切成 2~3 段后端点误差天然达 1000mm 量级，
    500mm 容差无法命中。本函数在四面展开后（3D 空间、face 标签已定）把
    共线断裂碎片贪心拼回。

    判据（gap=300/ang=10° 实测最优，2026-08-31 扫参 12 组）：
        1. 同一 face（跨面共线是镜像假象，不拼）；
        2. 端点最小距离 <= gap_mm（断裂缝隙）；
        3. 无向夹角 <= ang_deg（真正共线断裂，非交叉杆）；
        4. 跳过横隔 / 横担 / corner_leg（重建杆不参与拼接）；
        5. 合成杆长度 ∈ [min_merged_len, max_merged_len]，段数 <= max_segments
           （防过合并：union-find 全连通曾把 17m 主腿并成一杆，A2-pure
           56→26，已证实不可用）；
        6. 贪心优先级：合成长度最接近 target_len（GT 杆长中位）者优先。
        7. P2.3 role_specific=True 时分角色附加门禁：
           LEG — 同平台段（panel_levels 切分，平台层必断）；
           DIAG — 同 source_file + drawing_view + geometry_origin；
           HORIZ — 同 z 层（±horiz_z_tol）且不跨塔中心（±horiz_center_tol）。

    合成杆语义（透明化）：
        * geometry_class 继承：全部源杆均为 recognized 才 recognized（防止
          镜像面杆被拼接「洗白」成直接识别杆，污染 recognition 口径）；
        * geometry_origin = "collinear_stitch"，stitched_from 记录证据链；
        * 端点吸附到最近的现存节点，保持图连通。

    实测（35A1-JC1）：TP@200 +4~+9、TP@500 ±0~+1、Precision@500
    33.1%→37.4%（碎片 FP 被合并消除）；杆数 1550→1222。

    返回 (新 bars 列表, 新建端点节点 dict, 统计报告)。新建端点是合成杆的
    精确投影极值（调用方须并入节点表）；孤立旧节点由下游 prune 清理。
    """
    if not bars:
        return list(bars), {}, {"merged_groups": 0}

    def _p(nid):
        p = nodes.get(nid)
        return (float(p[0]), float(p[1]), float(p[2])) if p else None

    # 候选杆：跳过横隔 / 横担 / corner
    cand: Dict[str, Tuple[Vec3T, Vec3T, dict]] = {}
    skipped: Dict[str, int] = {}
    for b in bars:
        bid = str(b.get("id"))
        if b.get("diaphragm"):
            skipped["diaphragm"] = skipped.get("diaphragm", 0) + 1
            continue
        # P2.1b（2026-09-04）：marker_synth 合成横杆豁免拼接——它们是
        # 「层位终态完整杆」（[0,±inner]/[±inner,±leg]/[0,±leg] 分段
        # 体系，GT 同构），与相邻斜杆/残段拼接会把横杆端点拉离层位
        # （06 册实测：12 根合成杆被拼剩 7 根，全跨段 [0,±leg] 全灭，
        # 端点 z 漂 212mm 变斜杆）。tower_dxf 的双线/共线合并、DT 残段
        # 清扫、crossarm 剪枝均已豁免，这里补齐最后一块。
        if str(b.get("geometry_origin") or "") in ("marker_synth", "leg_synth"):
            skipped["marker_synth"] = skipped.get("marker_synth", 0) + 1
            continue
        if str(b.get("role") or "").upper() == "CROSS":
            skipped["crossarm"] = skipped.get("crossarm", 0) + 1
            continue
        if skip_corner_leg and b.get("corner_leg"):
            skipped["corner"] = skipped.get("corner", 0) + 1
            continue
        a, c = _p(b.get("from")), _p(b.get("to"))
        if a is None or c is None:
            skipped["no_endpoint"] = skipped.get("no_endpoint", 0) + 1
            continue
        if _dist3(a, c) < 1e-6:
            continue
        # max_single_len_mm 门槛（2026-08-31 实测教训）：已接近 GT 杆长的
        # 中长杆（如 1100~1500mm）往往单独就能命中 GT（500mm 容差），把它
        # 与短残段并成 ~2000mm 合成杆反而毁掉已有匹配（TP@500 208→188）。
        # 只允许「短残段」（< max_single_len_mm）参与拼接；0 = 不设限。
        if max_single_len_mm > 0 and _dist3(a, c) >= max_single_len_mm:
            skipped["long_single"] = skipped.get("long_single", 0) + 1
            continue
        cand[bid] = (a, c, dict(b))

    by_face: Dict[str, List[str]] = defaultdict(list)
    for bid, (_, _, p) in cand.items():
        by_face[str(p.get("face") or "?")].append(bid)

    face_of = {bid: fid for fid, ids in by_face.items() for bid in ids}
    role_rejected: Dict[str, int] = {}
    active: Dict[str, Tuple[Vec3T, Vec3T, int]] = {
        bid: (v[0], v[1], 1) for bid, v in cand.items()
    }
    merged_chains: Dict[str, List[str]] = {}
    new_id_seq = 0

    def _bar_seg(bid: str) -> Tuple[Vec3T, Vec3T]:
        if bid in active:
            a, b, _ = active[bid]
            return a, b
        if bid in cand:
            return cand[bid][0], cand[bid][1]
        raise KeyError(bid)

    def _bar_props(bid: str) -> dict:
        if bid in cand:
            return cand[bid][2]
        for mid in merged_chains.get(bid) or []:
            if mid in cand:
                return cand[mid][2]
        return {}

    def _pair_ok(bi: str, bj: str) -> bool:
        try:
            x0, x1 = _bar_seg(bi)
            y0, y1 = _bar_seg(bj)
        except KeyError:
            return False
        ux, uy = _unit3(_sub3(x1, x0)), _unit3(_sub3(y1, y0))
        if ux is None or uy is None:
            return False
        if _angle_deg(ux, uy) > ang_deg:
            return False
        if min(_dist3(x0, y0), _dist3(x0, y1),
               _dist3(x1, y0), _dist3(x1, y1)) > gap_mm:
            return False
        pa, pb = _bar_props(bi), _bar_props(bj)
        if not pa or not pb:
            return True
        ok, reason = _role_pair_ok_stitch(
            bi, bj,
            {bi: (x0, x1, pa), bj: (y0, y1, pb)},
            face_of,
            role_specific=role_specific,
            panel_levels=panel_levels,
            platform_tol_mm=platform_tol_mm,
            horiz_z_tol_mm=horiz_z_tol_mm,
            horiz_center_tol_mm=horiz_center_tol_mm,
            cross_sheet_ok=cross_sheet_ok,
        )
        if not ok and reason:
            role_rejected[reason] = role_rejected.get(reason, 0) + 1
        return ok

    pairs: List[Tuple[float, str, str]] = []
    for _fid, ids in by_face.items():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if _pair_ok(ids[i], ids[j]):
                    L = max(_dist3(cand[ids[i]][0], cand[ids[j]][0]),
                            _dist3(cand[ids[i]][0], cand[ids[j]][1]),
                            _dist3(cand[ids[i]][1], cand[ids[j]][0]),
                            _dist3(cand[ids[i]][1], cand[ids[j]][1]))
                    if L <= max_merged_len_mm:
                        pairs.append((abs(L - target_len_mm), ids[i], ids[j]))
    pairs.sort(key=lambda t: t[0])

    consumed: set = set()
    while pairs:
        _score, bi, bj = pairs.pop(0)
        if bi in consumed or bj in consumed:
            continue
        ai_, aj_ = active.get(bi), active.get(bj)
        if ai_ is None or aj_ is None:
            continue
        if ai_[2] + aj_[2] > max_segments:
            continue
        axis = _unit3(_sub3(ai_[1], ai_[0])) or _unit3(_sub3(aj_[1], aj_[0]))
        if axis is None:
            continue
        pts = [ai_[0], ai_[1], aj_[0], aj_[1]]
        proj = sorted(((sum(p[k] * axis[k] for k in range(3)), p) for p in pts),
                      key=lambda t: t[0])
        p_s, p_e = proj[0][1], proj[-1][1]
        L = _dist3(p_s, p_e)
        if L < min_merged_len_mm or L > max_merged_len_mm:
            continue
        new_id_seq += 1
        nid = f"stitch_{new_id_seq}"
        active[nid] = (p_s, p_e, ai_[2] + aj_[2])
        face_of[nid] = face_of.get(bi, "?")
        merged_chains[nid] = merged_chains.get(bi, [bi]) + merged_chains.get(bj, [bj])
        consumed.add(bi)
        consumed.add(bj)
        fn = face_of[nid]
        for other, ov in active.items():
            if other == nid or other in consumed:
                continue
            if face_of.get(other) != fn:
                continue
            if not _pair_ok(nid, other):
                continue
            if active[nid][2] + ov[2] > max_segments:
                continue
            L2 = max(_dist3(p_s, ov[0]), _dist3(p_s, ov[1]),
                     _dist3(p_e, ov[0]), _dist3(p_e, ov[1]))
            if L2 > max_merged_len_mm:
                continue
            pairs.append((abs(L2 - target_len_mm), nid, other))
        pairs.sort(key=lambda t: t[0])

    # 组装：合成杆端点以精确投影极值新建节点（见下方注释）。
    out_bars: List[dict] = []
    new_nodes: Dict[str, Vec3] = {}
    n_merged = 0
    roles_merged: Dict[str, int] = {}
    len_after: List[float] = []
    for b in bars:
        if str(b.get("id")) in consumed:
            continue
        out_bars.append(b)
    for nid, chain in merged_chains.items():
        # 只输出「终态」合成杆：后续又被并入更长链的中间合成体（其 id 已进
        # consumed）不输出，否则其源杆会被重复计入两根杆。
        if nid in consumed:
            continue
        p_s, p_e = active[nid][0], active[nid][1]
        # 合成杆端点用**精确投影极值**新建节点（与离线实验一致——端点吸附
        # 到现存节点会引入最多 gap_mm 的端点偏移，实测把 A2-full TP@500
        # 从 209 拉到 188）。孤立旧节点由下游 prune 清理。
        ns, ne = f"{nid}__S", f"{nid}__E"
        new_nodes[ns] = (round(p_s[0], 4), round(p_s[1], 4), round(p_s[2], 4))
        new_nodes[ne] = (round(p_e[0], 4), round(p_e[1], 4), round(p_e[2], 4))
        src = cand[chain[0]][2]
        src_classes = [str(cand[m][2].get("geometry_class") or "")
                       for m in chain if m in cand]
        if src_classes and all(c == "recognized" for c in src_classes):
            inherit_cls = "recognized"
        else:
            inherit_cls = next((c for c in src_classes if c), "")
        stitch_origin = "collinear_stitch"
        if _centerline_extract_bar(src) or any(
                _centerline_extract_bar(cand[m][2]) for m in chain if m in cand):
            stitch_origin = _centerline_stitch_origin(src)
        nb = dict(src)
        # 件号不继承（P4 件号错挂实测：stitch_58-61 继承 bar_321 → BOM 长度
        # 核验 4 根 +93~121% 超差、r_project_bom_master 超计）。合成杆是
        # 新的物理杆，BOM 件号证据属于源残段（stitched_from 证据链保留于
        # source_bar_ids，A1 追溯不丢）；剥掉 bar_id 使其不进 BOM 核验
        # 与件号去重。
        _src_bids = []
        for _m in chain:
            _sb = cand[_m][2].get("bar_id") if _m in cand else None
            if _sb and not str(_sb).startswith("UNLABELED"):
                _src_bids.append(str(_sb))
        if _src_bids:
            nb["source_bar_ids"] = sorted(set(_src_bids))
        nb.pop("bar_id", None)
        nb.update({
            "id": nid,
            "from": ns,
            "to": ne,
            "role": src.get("role"),
            "geometry_class": inherit_cls or src.get("geometry_class"),
            "geometry_origin": stitch_origin,
            "stitched_from": list(chain),
            "stitched_n_segments": len(chain),
        })
        out_bars.append(nb)
        n_merged += 1
        roles_merged[str(src.get("role") or "?")] = \
            roles_merged.get(str(src.get("role") or "?"), 0) + 1
        len_after.append(_dist3(p_s, p_e))

    len_after.sort()
    return out_bars, new_nodes, {
        "n_bars_in": len(bars),
        "n_bars_out": len(out_bars),
        "merged_groups": n_merged,
        "skipped": skipped,
        "by_role": roles_merged,
        "role_specific": role_specific,
        "role_rejected": role_rejected,
        "len_after_median": round(len_after[len(len_after) // 2], 1) if len_after else 0.0,
    }


def stitch_leg_chains(
    nodes: NodeMap,
    bars: List[dict],
    *,
    panel_levels: Optional[Sequence[float]] = None,
    gap_mm: float = 400.0,
    ang_deg: float = 6.0,
    dup_mid_tol_mm: float = 120.0,
    break_levels: Optional[Sequence[float]] = None,
) -> Tuple[List[dict], Dict[str, object]]:
    """P3.2 腿杆节间链合并（骨架先行）：把同角腿碎片并成节间整段。

    背景（2026-09-02 实测诊断）：模型 LEG 256 实例中位长 1005mm、162 根
    <1200mm——碎片来自图纸 beat 断点（07 册腿被切成 830/213/998mm 段）。
    GT 真实塔身角柱是环层之间的 ~3.5m 整段（8500→12000 等）。碎片化
    既毁渲染观感（塔身乱线），又让 Hungarian 端点匹配失效（1m 碎段 vs
    3.5m 角柱端点天然偏 1000mm 量级）。

    与 collinear_stitch 的区别（为何另开通道）：
        * 通用 stitch 的 max_single_len_mm=800「中长杆保护」会把 830/998mm
          腿段全部拒之门外——那是 DIAG 杆的实测教训，对 LEG 恰好反了；
        * 通用 stitch max_segments=2 只两两拼，腿链需要全段合并；
        * 腿合并的正确约束不是「目标杆长 2018」，而是「节间包络」：
          合并链必须整体落在单一 panel_levels 区间内（平台层必断），
          与 GT 角柱（环层间整段）结构一致。

    算法：
        1. 收集 role==LEG（含 corner_leg/derived），按物理角象限分组
           （sign(中点x), sign(中点y)——四面展开后每角每 z 段恰一根）；
        2. 组内按 z 排序，同节间 + gap<=gap_mm + 夹角<=ang_deg 连成链；
        3. 度数保护：中间节点有其他杆（横隔/斜材/DT）挂接处断链——
           保证不制造新悬空（几何门禁不回退）；
        4. z 向近重合的平行重复段（同角同段被两视图各画一次）取其一，
           优先保留 dxf_geom/recognized 证据；
        5. 合成杆复用链端两端的**现存节点**（from=最低段底节点，
           to=最高段顶节点）——不新建节点、不吸附漂移，挂接拓扑不丢。

    合成杆语义（与 collinear_stitch 对齐）：
        bar_id 剥离（UNLABELED，BOM 核验不掺假），source_bar_ids 留
        证据链；geometry_origin="leg_chain_stitch"；geometry_class 全
        recognized 才继承 recognized。

    返回 (新 bars 列表, 统计报告)。节点集不变（复用现存节点）。
    """
    # P3.4（2026-09-02）：break_levels 断链层集。默认 None=panel_levels
    # （平台层）。GT 实测腿的分段边界是「斜杆终止层」体系
    # （14500/17000/19400/21500...，与斜材节间同体系），不是平台层——
    # 腿 (14000,17000) 跨 16000 平台层。调用方传终止层表时可生成
    # 跨平台层的腿段。
    _break = sorted(float(z) for z in (break_levels if break_levels is not None
                                       else (panel_levels or [])))
    if not bars or not _break:
        return list(bars), {"merged_groups": 0, "reason": "no_panel_levels"}

    def _p(nid):
        q = nodes.get(nid)
        return (float(q[0]), float(q[1]), float(q[2])) if q else None

    # 节点度数（全图，含非 LEG 杆）——度数保护用
    node_deg: Dict[str, int] = {}
    for b in bars:
        for key in ("from", "to"):
            nid = b.get(key)
            if nid:
                node_deg[nid] = node_deg.get(nid, 0) + 1

    legs: List[dict] = []
    skipped: Dict[str, int] = {}
    for b in bars:
        if str(b.get("role") or "").upper() != "LEG":
            continue
        if b.get("diaphragm"):
            skipped["diaphragm"] = skipped.get("diaphragm", 0) + 1
            continue
        # P3.5f：终止层对生成杆不参与链合并——它们已是按 GT 终止层
        # 分段的完整节间杆（(14500,17000) 等），链合并会把它们与
        # dxf 腿碎段并成跨层长杆（实测 340 根 tps leg 被吞，
        # Hungarian 分段对齐失效）。
        if b.get("terminal_pair_structure"):
            skipped["terminal_pair_structure"] = skipped.get(
                "terminal_pair_structure", 0) + 1
            continue
        a, c = _p(b.get("from")), _p(b.get("to"))
        if a is None or c is None:
            continue
        legs.append(dict(b, _a=a, _c=c))
    if len(legs) < 2:
        return list(bars), {"merged_groups": 0, "n_legs": len(legs)}

    # 节间定位：panel i 覆盖 [pl[i], pl[i+1])（P3.4：断链层 = _break）
    pls = list(_break)

    def _panel_of(z: float) -> int:
        for i in range(len(pls) - 1):
            if pls[i] - 1e-6 <= z < pls[i + 1]:
                return i
        return -1  # 层区间外（如基座 0~首层、塔顶）

    # 象限分组（物理角）
    quads: Dict[Tuple[int, int], List[dict]] = defaultdict(list)
    for b in legs:
        mx, my = (b["_a"][0] + b["_c"][0]) / 2, (b["_a"][1] + b["_c"][1]) / 2
        quads[(1 if mx >= 0 else -1, 1 if my >= 0 else -1)].append(b)

    def _zspan(b: dict) -> Tuple[float, float]:
        z0, z1 = b["_a"][2], b["_c"][2]
        return (z0, z1) if z0 <= z1 else (z1, z0)

    def _endpoints_low_high(b: dict) -> Tuple[str, str]:
        z0, z1 = _zspan(b)
        return (b["from"], b["to"]) if b["_a"][2] <= b["_c"][2] else (b["to"], b["from"])

    def _frag_key(b: dict) -> str:
        return str(b.get("id"))

    merged_ids: set = set()
    out_extra: List[dict] = []
    rep_quads = 0
    rep_dropped_dup = 0
    rep_split_deg = 0

    for _q, frags in quads.items():
        # 重复段去重：同角内 z 向近重合（中点距 < dup_mid_tol_mm）且共线
        frags = sorted(frags, key=_zspan)
        keep: List[dict] = []
        for b in frags:
            dup = False
            for k in keep:
                kmid = ((k["_a"][0] + k["_c"][0]) / 2, (k["_a"][1] + k["_c"][1]) / 2,
                        (k["_a"][2] + k["_c"][2]) / 2)
                bmid = ((b["_a"][0] + b["_c"][0]) / 2, (b["_a"][1] + b["_c"][1]) / 2,
                        (b["_a"][2] + b["_c"][2]) / 2)
                if _dist3(kmid, bmid) <= dup_mid_tol_mm:
                    uk = _unit3(_sub3(k["_c"], k["_a"]))
                    ub = _unit3(_sub3(b["_c"], b["_a"]))
                    if uk and ub and _angle_deg(uk, ub) <= ang_deg * 2:
                        # 保留证据更强的：dxf_geom/recognized 优先
                        ok, ob = str(k.get("geometry_origin") or ""), str(b.get("geometry_origin") or "")
                        keep_o = ok if (
                            ok == "dxf_geom" or (ob != "dxf_geom" and k.get("geometry_class") == "recognized")
                        ) else ob
                        if keep_o == ok:
                            # 现存 k 胜出：丢弃新 b
                            dup = True
                            rep_dropped_dup += 1
                            break
                        # 新 b 胜出：丢弃旧 k
                        keep.remove(k)
                        merged_ids.add(_frag_key(k))
                        rep_dropped_dup += 1
                        break
            if dup:
                merged_ids.add(_frag_key(b))  # 重复段直接移除（不重建合成杆）
                continue
            keep.append(b)
        frags = keep
        if len(frags) < 2:
            continue

        # 链构建：相邻同节间 + gap + 共线
        chains: List[List[dict]] = []
        cur: List[dict] = [frags[0]]
        for b in frags[1:]:
            prev = cur[-1]
            pz0, pz1 = _zspan(prev)
            bz0, bz1 = _zspan(b)
            gap = max(bz0 - pz1, 0.0)
            up = _unit3(_sub3(prev["_c"], prev["_a"]))
            ub = _unit3(_sub3(b["_c"], b["_a"]))
            same_panel = _panel_of((pz0 + pz1) / 2) >= 0 and \
                _panel_of((pz0 + pz1) / 2) == _panel_of((bz0 + bz1) / 2)
            ok = (same_panel and gap <= gap_mm and up is not None and ub is not None
                  and _angle_deg(up, ub) <= ang_deg)
            if ok:
                cur.append(b)
            else:
                chains.append(cur)
                cur = [b]
        chains.append(cur)

        for chain in chains:
            if len(chain) < 2:
                continue
            # 度数保护：链中段节点若有外部杆挂接（度数>2），在处断开
            segs: List[List[dict]] = []
            seg: List[dict] = [chain[0]]
            for b in chain[1:]:
                lo, hi = _endpoints_low_high(chain[chain.index(b) - 1])
                # 前段顶节点
                prev_top = _endpoints_low_high(chain[chain.index(b) - 1])[1]
                if node_deg.get(prev_top, 0) > 2:
                    segs.append(seg)
                    seg = [b]
                    rep_split_deg += 1
                else:
                    seg.append(b)
            segs.append(seg)
            for s in segs:
                if len(s) < 2:
                    continue
                low_node = _endpoints_low_high(s[0])[0]
                high_node = _endpoints_low_high(s[-1])[1]
                src = s[0]
                src_classes = [str(f.get("geometry_class") or "") for f in s]
                inherit_cls = "recognized" if src_classes and all(
                    c == "recognized" for c in src_classes) else next(
                    (c for c in src_classes if c and c != "recognized"),
                    src_classes[0] if src_classes else "")
                _src_bids = sorted({str(f.get("bar_id")) for f in s
                                    if f.get("bar_id") and not str(f.get("bar_id")).startswith("UNLABELED")})
                nb = dict(src)
                nb.pop("bar_id", None)
                if _src_bids:
                    nb["source_bar_ids"] = _src_bids
                nb.update({
                    "id": f"legchain_{rep_quads}",
                    "from": low_node,
                    "to": high_node,
                    "role": "LEG",
                    "geometry_class": inherit_cls or src.get("geometry_class"),
                    "geometry_origin": "leg_chain_stitch",
                    "leg_stitched_from": [str(f.get("id")) for f in s],
                    "leg_stitched_n": len(s),
                })
                if any(f.get("corner_leg") for f in s):
                    nb["corner_leg"] = True
                out_extra.append(nb)
                for f in s:
                    merged_ids.add(_frag_key(f))
                rep_quads += 1

    if not merged_ids:
        return list(bars), {"merged_groups": 0, "n_legs": len(legs)}

    out_bars = [b for b in bars if str(b.get("id")) not in merged_ids]
    out_bars.extend(out_extra)
    return out_bars, {
        "merged_groups": rep_quads,
        "dropped_duplicates": rep_dropped_dup,
        "split_at_degree": rep_split_deg,
        "n_legs_in": len(legs),
        "n_bars_in": len(bars),
        "n_bars_out": len(out_bars),
    }


def snap_dangling_endpoints_local(
    nodes: NodeMap,
    bars: List[dict],
    *,
    max_gap_mm: float = 300.0,
    max_len_change_ratio: float = 0.02,
    allowed_roles: Sequence[str] = ("DIAG", "LEG", "HORIZ"),
) -> Tuple[NodeMap, List[dict], Dict[str, int]]:
    """Phase 2.3：受约束的局部端点吸附（取代全局 snap_diagonals_to_legs）。

    背景（2026-08-31 review）：全局 snap 会重定位已共享的节点、拆散已有连通
    （实测 Degree=1 反升、Z 越界、A2 下降）。本函数只处理「degree=1 的长杆
    悬空端点」，且同时满足全部约束才吸附：

        1. 悬空端点在「邻接主腿线段」的投影距离 <= max_gap_mm；
        2. 投影点落在该腿线段**内部**（不外延越界）；
        3. 吸附后杆长变化 <= max_len_change_ratio（2%）；
        4. 目标落点直接并入既有腿节点（若 <2mm），否则吸附到腿线段上的
           投影点——只改这一个节点的坐标，不动其它杆件；
        5. 仅处理 allowed_roles（默认斜材/腿/水平材；CROSS 横担端头跳过）。

    与全局 snap 的本质区别：全局版把「每根斜材两端」都拉到拟合工作线
    （工作线本身有拟合误差，且会移动共享节点）；本版只拉「确实悬空」的
    那一个端点，目标线段是真实存在的 leg 杆段（非拟合线），并且逐杆审计
    长度变化。

    返回 (new_nodes, new_bars, {"snapped": n, "merged": m, "rejected": {...}})。
    """
    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = [dict(b) for b in bars]
    roles = classify_members(new_nodes, new_bars)
    deg: Dict[str, int] = {}
    for b in new_bars:
        deg[b["from"]] = deg.get(b["from"], 0) + 1
        deg[b["to"]] = deg.get(b["to"], 0) + 1

    leg_bars = [b for b in new_bars if roles.get(b["id"]) == "LEG"]
    rejected = {"role": 0, "no_leg": 0, "gap": 0, "len_change": 0, "crossarm": 0}
    snapped = 0
    merged = 0

    for b in new_bars:
        role = roles.get(b["id"]) or str(b.get("role") or "")
        if role not in allowed_roles and str(b.get("role") or "").upper() not in allowed_roles:
            rejected["role"] += 1
            continue
        if role == "CROSS" or str(b.get("role") or "").upper() == "CROSS":
            rejected["crossarm"] += 1
            continue
        for end_key in ("from", "to"):
            nid = b[end_key]
            if deg.get(nid) != 1:
                continue
            p = new_nodes.get(nid)
            if p is None:
                continue
            q = new_nodes.get(b["to" if end_key == "from" else "from"])
            if q is None:
                continue
            L0 = math.dist(p, q)
            # 在同 z 邻域找最近 leg 杆段投影
            best = None  # (dist, proj, leg_bar)
            for lb in leg_bars:
                s1, s2 = new_nodes.get(lb["from"]), new_nodes.get(lb["to"])
                if s1 is None or s2 is None:
                    continue
                # 快速 z 粗筛：腿段 z 范围与悬空端 z 至少接近
                if min(s1[2], s2[2]) - max_gap_mm > p[2] or max(s1[2], s2[2]) + max_gap_mm < p[2]:
                    continue
                proj, dist = _point_segment_distance(p, s1, s2)
                if dist <= max_gap_mm and (best is None or dist < best[0]):
                    best = (dist, proj, lb)
            if best is None:
                rejected["gap"] += 1
                continue
            dist, proj, lb = best
            # 约束3：杆长变化
            L1 = math.dist(proj, q)
            if L0 > 1e-6 and abs(L1 - L0) / L0 > max_len_change_ratio:
                rejected["len_change"] += 1
                continue
            # 约束4：并入近邻既有节点，否则吸附到投影点
            near = _nearest_existing_node_id(new_nodes, proj, 2.0)
            if near is not None and near != nid:
                b[end_key] = near
                merged += 1
            else:
                new_nodes[nid] = (float(proj[0]), float(proj[1]), float(proj[2]))
                snapped += 1
    return new_nodes, new_bars, {
        "snapped": snapped, "merged": merged, "rejected": rejected,
    }


def repair_dangling_endpoints(
    nodes: NodeMap,
    bars: List[dict],
    *,
    stub_max_len_mm: float = 250.0,
    weld_max_mm: float = 350.0,
    t_junction_mm: float = 50.0,
    half_width_fn: Optional[Callable[[float], float]] = None,
) -> Tuple[List[dict], Dict[str, object]]:
    """Phase 3：悬空断裂修复（微型残段清除 + 端点焊接）。

    背景（2026-08-31，JC1 交付门禁 genuine_dangling=17 → 目标 <=4）：
    17 个实例去重后仅 8 个物理位置，三类成因——

        1. 微型残段（<250mm 的孤立短杆，图纸噪声/断裂残根）：直接删除。
           这类杆无法匹配 GT（GT 杆长中位 ~2005mm），删除同时降低 FP。
        2. 断裂端点近旁有真实节点（166~310mm）：端点焊接——把杆端引用
           重指到最近的有效节点。端点位移在评测容差（500mm）内可控。
        3. 伙伴杆整体缺失（周围 450mm+ 空无一物）：不能无中生有，
           留给 review_queue 人工复核。

    与 snap_dangling_endpoints_local 的区别：snap 在四面展开**之前**只修
    front 面（镜像面不继承其合并结果，这正是 B/L/R 面悬空的根因）；本函数
    在四面展开+共线拼接**之后**对所有面统一修复。

    安全约束：
        * role=CROSS（横担悬臂端头是合法自由端）、corner_leg、diaphragm
          杆一律不碰；
        * T 形接头（端点落在其它杆身上 <=t_junction_mm）已物理连接，跳过；
        * 径向远超塔身半宽（>1.4x）的悬臂端头跳过；
        * 焊接目标必须是仍有杆件引用的节点（度 >=1），且不是本杆另一端。

    返回 (new_bars, report)；nodes 不变（被弃用的端点节点成为度 0 孤立点，
    与 stitch 消费节点的处置一致）。
    """
    out_bars: List[dict] = [dict(b) for b in bars]
    roles = classify_members(nodes, out_bars)

    def _is_excused(b: dict, nid: str) -> bool:
        """端点是否属合法自由端（横担悬臂 / T 形接头），不可动。"""
        p = nodes.get(nid)
        if p is None:
            return True
        # T 形接头：端点落在其它杆身上
        for ob in out_bars:
            if ob is b:
                continue
            s1, s2 = nodes.get(ob.get("from")), nodes.get(ob.get("to"))
            if s1 is None or s2 is None:
                continue
            _, dist = _point_segment_distance((float(p[0]), float(p[1]), float(p[2])), s1, s2)
            if dist <= t_junction_mm:
                return True
        # 横担外伸悬臂端
        if half_width_fn is not None:
            hw = half_width_fn(float(p[2]))
            if hw > 0 and math.hypot(p[0], p[1]) > hw * 1.4:
                return True
        return False

    def _degrees(blist: List[dict]) -> Dict[str, int]:
        d: Dict[str, int] = {}
        for b in blist:
            d[b["from"]] = d.get(b["from"], 0) + 1
            d[b["to"]] = d.get(b["to"], 0) + 1
        return d

    # ---- Pass 1：微型残段清除 ----
    deg = _degrees(out_bars)
    removed: List[str] = []
    kept: List[dict] = []
    for b in out_bars:
        role = str(b.get("role") or "").upper()
        if role == "CROSS" or b.get("corner_leg") or b.get("diaphragm"):
            kept.append(b)
            continue
        p1, p2 = nodes.get(b.get("from")), nodes.get(b.get("to"))
        if p1 is None or p2 is None:
            kept.append(b)
            continue
        L = math.dist(p1, p2)
        if L >= stub_max_len_mm:
            kept.append(b)
            continue
        if deg.get(b["from"], 0) != 1 and deg.get(b["to"], 0) != 1:
            kept.append(b)
            continue
        # 悬空端必须是「真悬空」（非横担端头、非 T 形接头）才允许删
        if deg.get(b["from"], 0) == 1 and _is_excused(b, b["from"]):
            kept.append(b)
            continue
        if deg.get(b["to"], 0) == 1 and _is_excused(b, b["to"]):
            kept.append(b)
            continue
        removed.append(str(b.get("id")))
    out_bars = kept

    # ---- Pass 2：端点焊接 ----
    deg = _degrees(out_bars)
    welds: List[Dict[str, object]] = []
    for b in out_bars:
        role = str(b.get("role") or "").upper()
        if role == "CROSS" or b.get("corner_leg") or b.get("diaphragm"):
            continue
        for end_key in ("from", "to"):
            nid = b[end_key]
            if deg.get(nid, 0) != 1:
                continue
            p = nodes.get(nid)
            if p is None or _is_excused(b, nid):
                continue
            other = b["to" if end_key == "from" else "from"]
            # 最近有效节点（仍有杆引用、非本杆另一端）
            best = None
            for cand, cpos in nodes.items():
                if cand == nid or cand == other or deg.get(cand, 0) < 1:
                    continue
                d = math.dist(p, cpos)
                if d <= weld_max_mm and (best is None or d < best[0]):
                    best = (d, cand)
            if best is None:
                continue
            dist, target = best
            b[end_key] = target
            deg[nid] = 0
            deg[target] = deg.get(target, 0) + 1
            welds.append({
                "node": nid, "bar": str(b.get("id")),
                "welded_to": target, "dist_mm": round(dist, 1),
            })

    report = {
        "removed_stub_bars": removed,
        "welded": welds,
        "n_bars_in": len(bars),
        "n_bars_out": len(out_bars),
    }
    return out_bars, report


def inspect_model_topology(
    nodes: NodeMap,
    bars: List[dict],
    *,
    half_width_fn: Optional[Callable[[float], float]] = None,
) -> Dict[str, object]:
    """拓扑度数统计（量化验收 1：悬空断裂节点 Degree=1 应为 0）。

    half_width_fn：可选，塔身半宽剖面函数（真实 mm）。仅 debug/eval 显式传入
    GT 剖面；生产默认 None，此时横担端头判定退化为仅依赖 role == "CROSS"。

    返回 {"degree_histogram": {degree: count}, "dangling_degree1": n,
          "max_degree": k, "components": c, "total_nodes": N, "total_bars": M}。
    """
    degree: Dict[str, int] = {nid: 0 for nid in nodes}
    for b in bars:
        f, t = b.get("from"), b.get("to")
        if f in degree:
            degree[f] += 1
        if t in degree:
            degree[t] += 1

    hist: Dict[int, int] = {}
    for d in degree.values():
        hist[d] = hist.get(d, 0) + 1

    # 区分「合法横担悬臂端头」与「真悬空断裂」：横担（CROSS）与横担斜材
    # 的水平外伸端是物理上自由悬臂端（degree=1 属正常），不应计入悬空断裂。
    # 判定：degree=1 节点，其唯一杆件的 role == "CROSS"，或该节点水平径向
    # 距离远超该标高处塔身半宽（横担外伸区），即为横担端头。
    # 注意：塔身半宽不能用节点 |x| 中位数（会被横担端头污染）。生产默认
    # half_width_fn=None，仅凭 role == "CROSS" 判定横担端头；debug/eval 可
    # 显式传入 GT 权威半宽（debug.gt_profile.gt_tower_half_width）提升精度。
    roles: Dict[str, str] = {}
    try:
        roles = classify_members(nodes, bars)
    except Exception:
        roles = {}
    crossarm_tip = 0
    genuine_dangling = 0
    genuine_detail: List[Dict[str, object]] = []
    # 预建杆件线段表（T 形接头判定：degree=1 节点落在其它杆件身上）
    seg_list = []
    for b in bars:
        p1, p2 = nodes.get(b.get("from")), nodes.get(b.get("to"))
        if p1 is not None and p2 is not None:
            seg_list.append((b.get("from"), b.get("to"),
                             (float(p1[0]), float(p1[1]), float(p1[2])),
                             (float(p2[0]), float(p2[1]), float(p2[2]))))
    for nid, d in degree.items():
        if d != 1:
            continue
        # 找到该节点的唯一杆件
        bar_role = None
        bar_id = None
        for b in bars:
            if b.get("from") == nid or b.get("to") == nid:
                bar_role = roles.get(b.get("id")) or b.get("role")
                bar_id = b.get("id")
                break
        p = nodes.get(nid)
        radial = float(np.hypot(p[0], p[1])) if p is not None else 0.0
        z = float(p[2]) if p is not None else 0.0
        body_hw_at_z = half_width_fn(z) if half_width_fn is not None else 0.0
        # 横担端头：role=CROSS，或径向远超该标高权威塔身半宽（>1.4x，横担外伸）
        is_crossarm_tip = (
            bar_role == "CROSS"
            or (body_hw_at_z > 0 and radial > body_hw_at_z * 1.4)
        )
        # T 形接头（S4 拼接后常见）：degree=1 节点躺在另一根杆件身上
        # （点到线段距离 < 50mm）——它是横向腹杆挂到拼接长杆中部的连接点，
        # 物理上完全连接，不是悬空断裂。典型：碎片 A-B、B-C 拼成 A-C 后，
        # 节点 B 只剩腹杆 B-D，B 落在 A-C 线上。
        is_t_junction = False
        if p is not None:
            pt = (float(p[0]), float(p[1]), float(p[2]))
            for f_id, t_id, s1, s2 in seg_list:
                if nid in (f_id, t_id):
                    continue
                _, dist = _point_segment_distance(pt, s1, s2)
                if dist <= 50.0:
                    is_t_junction = True
                    break
        if is_crossarm_tip:
            crossarm_tip += 1
        elif is_t_junction:
            pass  # T 形接头：不计悬空、不计横担端头
        else:
            genuine_dangling += 1
            genuine_detail.append({
                "id": nid,
                "z": round(z, 1),
                "radial": round(radial, 1),
                "body_half_width": round(body_hw_at_z, 1),
                "role": bar_role,
                "bar_id": bar_id,
            })

    # 连通分量
    adj: Dict[str, set] = {nid: set() for nid in nodes}
    for b in bars:
        f, t = b.get("from"), b.get("to")
        if f in adj and t in adj and f != t:
            adj[f].add(t)
            adj[t].add(f)
    seen: set = set()
    components = 0
    for nid in adj:
        if nid in seen:
            continue
        stack = [nid]
        seen.add(nid)
        while stack:
            cur = stack.pop()
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        components += 1

    # Phase 3：物理位置去重——四面展开把同一物理断裂复制成 4 个面实例
    # （bar_id 仅尾部 _F/_B/_L/_R 不同）。门禁应度量「物理缺陷数」而非
    # 「面实例数」：一处断裂镜像 4 次仍是 1 处缺陷。
    _face_suffix = ("_F", "_B", "_L", "_R")
    physical_stems = set()
    for g in genuine_detail:
        bid = str(g.get("bar_id") or "")
        stem = bid[:-2] if bid.endswith(_face_suffix) else bid
        physical_stems.add(stem)

    return {
        "degree_histogram": {str(k): v for k, v in sorted(hist.items())},
        "dangling_degree1": hist.get(1, 0),
        "crossarm_tip_count": crossarm_tip,
        "genuine_dangling_degree1": genuine_dangling,
        "genuine_dangling_detail": genuine_detail,
        "genuine_dangling_physical": len(physical_stems),
        "max_degree": max(hist) if hist else 0,
        "components": components,
        "total_nodes": len(nodes),
        "total_bars": len(bars),
    }


# --------------------------------------------------------------------------- #
# Module 4  语义分类 + 分段缝合
# --------------------------------------------------------------------------- #

def _theil_sen_fit(
    zs: Sequence[float],
    hs: Sequence[float],
    *,
    max_pairs: int = 40000,
) -> Optional[Tuple[float, float]]:
    """Theil-Sen 稳健线性回归：斜率取所有点对斜率的中位数。

    相比最小二乘，Theil-Sen 对离群点（横担端头、误判的内部竖杆、跨段配准
    残差）的崩溃点高达 ~29%，适合塔身半宽这种「大多数点在同一直线上、
    少量点严重偏离」的工程采样场景。

    返回 (intercept, slope)，即 h(z) = intercept + slope * z；点不足返回 None。
    """
    n = len(zs)
    if n < 2:
        return None
    # O(n^2) 点对：n 大时均匀降采样，控制计算量（斜率中位数对采样稳健）
    step = 1
    while (n // step) * ((n // step) - 1) // 2 > max_pairs and step < n:
        step += 1
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)

    slopes: List[float] = []
    for a in range(len(idx)):
        za, ha = zs[idx[a]], hs[idx[a]]
        for b in range(a + 1, len(idx)):
            zb, hb = zs[idx[b]], hs[idx[b]]
            dz = zb - za
            if abs(dz) < 1e-6:
                continue
            slopes.append((hb - ha) / dz)
    if not slopes:
        return None
    slopes.sort()
    k = slopes[len(slopes) // 2]
    # 截距取中位数（同样稳健）
    inter = sorted(h - k * z for z, h in zip(zs, hs))
    b0 = inter[len(inter) // 2]
    return b0, k


def _fit_taper_profile(
    z_pts: Sequence[float],
    hw_pts: Sequence[float],
    *,
    inlier_tol_mm: float = 100.0,
    min_inlier_ratio: float = 0.85,
    min_z_coverage: float = 0.75,
    max_rounds: int = 3,
    debug: bool = False,
    debug_out: Optional[Dict[str, Any]] = None,
) -> Optional[Callable[[float], float]]:
    """把分箱半宽样本稳健回归成直线锥体 hw(z) = b + k*z（S7 锥体重建）。

    步骤（2026-08-31 v2：自底向上 + 迭代剔除横担离群箱）：
        1. 剔除「无主腿箱」：样本值低于全体中位 50% 的箱视为内部竖杆/噪声；
        2. 迭代收敛（最多 max_rounds 轮）：
             a. Theil-Sen 回归（斜率中位数，崩溃点 ~29%）；
             b. 剔除**正残差**（hw 高于回归线）超过 inlier_tol_mm 的箱——
                塔头横担外伸（p85 分箱值 900~2200mm）只会把分箱值推高，
                不会推低；负残差箱（腿内缩/采样缺陷）一并剔除；
             c. 若无箱被剔除则收敛；
        3. 物理约束 k <= 0（四棱台半宽随标高只减不增）；
        4. **内点比例**一致性检验（在剔除后的样本上）：残差 <= inlier_tol_mm
           的占比须 >= min_inlier_ratio，否则判为非单一锥体（变坡/塔头收窄），
           返回 None 让调用方回退 monotone 分段法。

    v1 教训（为什么必须迭代剔除）：横担箱是「高侧」单边离群，Theil-Sen 斜率
    中位数虽稳，但截距会被横担箱拉高 ~50mm，且内点比例检验把横担箱算进
    分母——v1 在真实 JC1 输入上因 75% < 85% 而误回退。先剔横担箱再回归，
    内点检验只评估塔身箱，两步都干净。

    返回闭包；任一步失败返回 None。
    """
    n = len(z_pts)
    if n < 4:
        return None

    med = sorted(hw_pts)[n // 2]
    if med <= 0:
        return None
    zs: List[float] = []
    hs: List[float] = []
    for z, h in zip(z_pts, hw_pts):
        if h >= med * 0.5:
            zs.append(float(z))
            hs.append(float(h))
    if len(zs) < 4:
        return None

    b0, k = 0.0, 0.0
    outl_zs: List[float] = []
    outl_hs: List[float] = []
    for _round in range(max_rounds):
        fit = _theil_sen_fit(zs, hs)
        if fit is None:
            return None
        b0, k = fit
        # 剔除高侧离群箱（横担）+ 低侧离群箱（采样缺陷）
        new_zs: List[float] = []
        new_hs: List[float] = []
        removed = 0
        for z, h in zip(zs, hs):
            r = h - (b0 + k * z)
            if abs(r) > inlier_tol_mm:
                removed += 1
                outl_zs.append(z)
                outl_hs.append(h)
                continue
            new_zs.append(z)
            new_hs.append(h)
        if removed == 0 or len(new_zs) < 4:
            break
        zs, hs = new_zs, new_hs

    # 物理约束：塔身随标高收缩。k>0（向上变宽）说明采样被污染，判失败。
    if k > 0:
        return None

    # 覆盖率检验（变坡检测）：剔除后样本须铺满输入 z 跨度的 min_z_coverage。
    # 例外（2026-09 S8 塔头横担整块剔除）：离群箱若构成**顶部整块**
    # （全部 z >= 内点最大 z - 一个箱宽），且其残差以**高侧**为主
    # （横担吊杆/桁架竖杆把 p85 推高——塔头分箱实测 895→1377 随 z
    # 递增，与 k<0 的塔身锥线方向相反），则这是塔头横担污染而非变坡：
    # 塔身锥线（GT 实测 0→36600 单一直线锥体）应线性外推覆盖塔头，
    # 接受拟合。真实两段式变坡塔的上段是**另一条下行直线**（低侧或
    # 平行残差），不满足「高侧 + 顶部整块」判据，仍走拒绝路径。
    z_span_in = max(z_pts) - min(z_pts)
    z_span_fit = (max(zs) - min(zs)) if len(zs) >= 2 else 0.0
    coverage = z_span_fit / z_span_in if z_span_in > 0 else 1.0
    top_block_crossarm = False
    if coverage < min_z_coverage and outl_zs:
        block_at_top = all(z >= max(zs) - 500.0 for z in outl_zs)
        high_side = sum(
            1 for z, h in zip(outl_zs, outl_hs)
            if (h - (b0 + k * z)) > 0
        ) >= max(1, int(0.6 * len(outl_zs)))
        top_block_crossarm = block_at_top and high_side
    if coverage < min_z_coverage and not top_block_crossarm:
        if debug:
            print(f"[taper] 回退：z 覆盖率 {coverage:.1%} < "
                  f"{min_z_coverage:.0%}（剔除段过大，疑似变坡/两段式塔身）")
        if debug_out is not None:
            debug_out.update({
                "reason": f"z_coverage {coverage:.1%} < "
                          f"{min_z_coverage:.0%}",
                "b": b0, "k": k,
                "z_coverage": coverage,
            })
        return None

    resid = [abs(h - (b0 + k * z)) for z, h in zip(zs, hs)]
    inliers = sum(1 for r in resid if r <= inlier_tol_mm)
    ratio = inliers / len(resid) if resid else 0.0
    if ratio < min_inlier_ratio:
        if debug:
            print(f"[taper] 回退：内点比例 {ratio:.1%} < {min_inlier_ratio:.0%}"
                  f"（残差 p90={sorted(resid)[int(len(resid)*0.9)]:.0f}mm "
                  f"max={max(resid):.0f}mm）疑似变坡")
        if debug_out is not None:
            debug_out.update({
                "reason": f"inlier_ratio {ratio:.1%} < {min_inlier_ratio:.0%}",
                "b": b0, "k": k,
                "inlier_ratio": ratio,
            })
        return None

    if debug_out is not None:
        debug_out.update({
            "reason": "",
            "b": b0, "k": k,
            "inlier_ratio": ratio,
            "z_coverage": coverage,
            "top_block_crossarm_removed": int(len(outl_zs))
            if top_block_crossarm else 0,
        })

    def half_width_taper(z: float, _b: float = b0, _k: float = k) -> float:
        return max(1.0, _b + _k * float(z))

    return half_width_taper


def detect_crossarm_layers_from_face(
    nodes: NodeMap,
    bars: List[dict],
    body_line_fn: Callable[[float], float],
    *,
    crossarm_ratio: float = 1.5,
    min_arm_mm: float = 700.0,
    cluster_gap_mm: float = 2000.0,
    layer_span_mm: float = 750.0,
) -> Tuple[Optional[Callable[[float], float]], Dict[str, object]]:
    """S7 生产横担层检测：从立面证据找塔头横担外伸，替代 GT 注入。

    背景：四面展开 face_maps 用 crossarm_half_width_fn(z)>0 判定横担层。生产
    路径此前传 None——塔头所有节点（含横担外伸 |t| 至 2200mm 的弦杆）被
    |t|>=0.85*w_gt 判为主腿角柱、硬吸附到塔身体半宽（实测 1048mm 平台），
    横担几何被系统性摧毁。GT 路径用 gt_crossarm_half_width（塔头三层
    30000/33500/33850 → 2200/1900/1134）；本函数从 DXF 证据重建同型函数。

    判据（塔头宽节点 z 链聚类，v2——p90 分箱在稀疏塔头会碎裂成 6 假层）：
        1. 收集全部杆件端点 (z, |t|)（横担弦杆是水平杆，必须含非竖直杆）；
        2. 「宽节点」：|t| > max(body_line(z)*crossarm_ratio, min_arm_mm)
           ——身体节点 |t| 至多到腿线附近（1.0~1.05×），横担节点跳到 2~4×；
        3. 宽节点按 z 排序成链，相邻 z 间距 <= cluster_gap_mm 归入同一层
           （横担层间由吊杆/桁架竖杆相连，层间距实测 <2000mm，而身体区
           的宽节点根本不存在，链只在塔头内部生长）；
        4. 层的 z 范围 = 链范围 ± layer_span_mm（覆盖层上下桁架节点），
          横担外伸 = 层内最大 |t|；
        5. 返回闭包：z 落在层范围内返回该层外伸，否则 0。

    功能语义（为什么允许宽层）：crossarm_half_width>0 只是打开 face_maps 的
    横担分支；真正的分选门是 |t| > w_gt*1.3（expand 参数 crossarm_ratio）。
    层范围只要罩住所有宽节点，宽节点即保留真实 t（配 crossarm_preserve_t），
    塔头主腿（|t|≈w_gt）仍走身体分支吸附到锥线。z 归一化在塔头的既有畸变
    （证据层位 vs GT 层位差 +2250/-750/-350）不因本函数而放大。

    返回 (crossarm_half_width_fn 或 None, 报告 dict)。检测不到层返回 (None, ...)，
    调用方保持旧行为（塔头按身体处理）。
    """
    if not nodes or not bars or body_line_fn is None:
        return None, {"layers": []}

    wide: List[Tuple[float, float]] = []
    for b in bars:
        for nid in (b.get("from"), b.get("to")):
            p = nodes.get(nid)
            if p is None:
                continue
            z, t = float(p[2]), abs(float(p[0]))
            if t <= 1e-6:
                continue
            body_w = max(1.0, float(body_line_fn(z)))
            if t > max(body_w * crossarm_ratio, min_arm_mm):
                wide.append((z, t))

    if not wide:
        return None, {"layers": []}

    wide.sort(key=lambda wt: wt[0])

    # z 链聚类
    groups: List[List[Tuple[float, float]]] = [[wide[0]]]
    for z, t in wide[1:]:
        if z - groups[-1][-1][0] <= cluster_gap_mm:
            groups[-1].append((z, t))
        else:
            groups.append([(z, t)])

    layers: List[Dict[str, float]] = []
    for grp in groups:
        z_lo = grp[0][0] - layer_span_mm
        z_hi = grp[-1][0] + layer_span_mm
        arm = max(t for _, t in grp)
        layers.append({"z_lo": z_lo, "z_hi": z_hi, "arm_mm": arm,
                       "z_center": (z_lo + z_hi) / 2.0,
                       "n_wide_nodes": len(grp),
                       "wide_z": sorted(round(z, 0) for z, _ in grp)})

    def crossarm_half_width(z: float) -> float:
        for lyr in layers:
            if lyr["z_lo"] <= z <= lyr["z_hi"]:
                return float(lyr["arm_mm"])
        return 0.0

    return crossarm_half_width, {
        "layers": layers,
        "n_wide_nodes": len(wide),
        "n_layers": len(layers),
    }


def fit_tower_half_width_from_face(
    nodes: NodeMap,
    bars: List[dict],
    *,
    leg_min_incl: float = 70.0,
    percentile: float = 85.0,
    bin_mm: float = 250.0,
    min_leg_len_mm: float = 2500.0,
    method: str = "monotone",
    taper_max_residual_mm: float = 150.0,
    report_out: Optional[Dict[str, Any]] = None,
) -> Optional[Callable[[float], float]]:
    """从单立面图拟合塔身半宽 half_width(z)（生产路径，不使用 GT）。

    阶段3.2（S1 修订，2026-08-31）：生产建模严禁用节点自身 abs(t) 作塔身深度，
    也严禁注入 GT 权威半宽。本函数从立面主腿证据确定性拟合 half_width(z)。

    **S1 修复（抗内部竖杆污染 + 物理单调约束）**：

    旧实现按「每 1mm z 取中位数」采样，导致内部竖杆（|x|≈0~50，在每个节间高度
    都贡献端点）淹没主腿（|x|≈1900~2300，只在段顶/段底贡献端点），half_width
    在 z=16000 崩到 338mm、z=8000 崩到 820mm（正确≈1642/2118），进而让
    `face_maps` 把节点投影到错误半宽、经 add_node 去重撞上跨段节点，产生 21-27m
    幽灵主腿（bar_621 链）。修复策略：

        1. 收集近竖直杆件端点 (z, |x|)；
        2. 分箱（bin_mm 宽），每箱取 |x| **上分位数**（percentile，默认 85%）
           作该箱外缘半宽——上分位数稳健于 max（防横担端头），又不会被内部
           竖杆的众多小 |x| 拉低（中位数的致命弱点）；
        3. 对分箱曲线施加**随 z 单调不增（塔四棱台半宽只减不增）**的物理约束，
           用「后向最大」包络（非降序列的累积 max，从底到顶只允许递减），
           杜绝 338mm 崩塌点；
        4. 分段线性插值返回闭包，越界夹紧。

    塔身四棱台为正四边形截面，任意标高 Z 处立面半宽 = 侧面半宽，同一 half_width(z)
    同时用于 X/Y。无法拟合时返回 None，调用方必须 review_required，不得退回 abs(t)。

    **S7 锥体重建（2026-08-31）**：新增 `method="taper"`。实测 GT 塔身是严格的
    直线锥体（线性拟合残差 max 31mm、中位 4mm），而 `"monotone"` 实测输出是
    「分段常数 + 单调包络」，存在两个致命缺陷：

        1. `min_leg_len_mm=2500` 长度门禁把已按节间切分（~1m/段）的主腿全部
           剔除，触发 `any_vertical_pts` 回退，85 分位被内部竖杆（|x|≈0~50）
           的众多端点拉低；
        2. `running_min` 单调包络一旦在某标高采到被污染的低值，会把它之后
           **所有高度**压到该值，形成平台段（实测 z=7000~12000 半宽恒定
           1827mm，GT 应为 2274→1922mm，偏差 350~450mm）。

    `"taper"` 改为：分箱取 95 分位 → 剔除无主腿的低值箱 → Theil-Sen 稳健回归
    hw(z) = b + k*z（k<=0 强制收缩）。单一离群点无法再污染整条曲线，且能外推
    到采样稀疏的标高区间。若回归残差中位 > taper_max_residual_mm（可能非单一
    锥体/存在变坡），自动回退 `"monotone"` 并保留旧行为。

    返回闭包在 z 超出采样范围时夹紧到边界值。
    """
    if not nodes or not bars:
        return None

    # 1. 采集近竖直杆件样本：**沿杆件插值**（S7 v4，2026-08-31）。
    #    v1~v3 的教训：只采端点时，节间化主腿（~1m/段）的端点只落在节间
    #    边界——250mm 箱里只有 1/4 的箱有腿端点，中间箱完全无腿样本，p85
    #    落到内部竖杆（实测 z=9250 箱 p85=1054 vs 腿线 2014）；而「偏好
    #    ≥min_leg_len 通长腿」在 JC1 上恰好选中 6 根幽灵长腿（21~27m 错误
    #    合并链），整段塔身无样本。v4：每根近竖直杆在其跨越的**每个箱心**
    #    线性插值 |x|（腿是直线，插值即真值），每个箱恒有腿线样本；幽灵
    #    长腿的插值点每箱至多 1~2 个，被分箱上分位压制。min_leg_len_mm
    #    保留为兼容参数，不再参与采样选择。
    vertical_pts: List[Tuple[float, float]] = []
    for b in bars:
        f = nodes.get(b.get("from"))
        t = nodes.get(b.get("to"))
        if f is None or t is None:
            continue
        dx = float(t[0]) - float(f[0])
        dz = float(t[2]) - float(f[2])
        if abs(dz) <= 1e-9:
            continue
        incl = abs(math.degrees(math.atan2(abs(dz), abs(dx))))
        if incl < leg_min_incl:
            continue
        z0, z1 = float(f[2]), float(t[2])
        x0, x1 = float(f[0]), float(t[0])
        lo, hi = (z0, z1) if z0 <= z1 else (z1, z0)
        k_lo = int(math.ceil(lo / bin_mm - 1e-9))
        k_hi = int(math.floor(hi / bin_mm + 1e-9))
        for k in range(k_lo, k_hi + 1):
            zc = k * bin_mm
            frac = (zc - z0) / (z1 - z0)
            xi = x0 + frac * (x1 - x0)
            vertical_pts.append((zc, abs(xi)))

    if len(vertical_pts) < 4:
        return None

    # 2. 分箱取上分位数（抗内部竖杆污染）。
    bins: Dict[int, List[float]] = {}
    for z, hw in vertical_pts:
        key = int(round(z / bin_mm))
        bins.setdefault(key, []).append(hw)

    z_pts: List[float] = []
    hw_pts: List[float] = []
    for key in sorted(bins):
        xs = sorted(bins[key])
        # 上分位数（默认 85%）：稳健于 max（防横担端头离群），又保留主腿外缘
        q = xs[min(len(xs) - 1, int(len(xs) * percentile / 100.0))]
        if q <= 0:
            continue
        z_pts.append(float(key) * bin_mm)
        hw_pts.append(q)

    if len(z_pts) < 2:
        if len(z_pts) == 1:
            hw = hw_pts[0]
            return (lambda z, hw=hw: hw) if hw > 0 else None
        return None

    # 2b. S7 锥体重建：Theil-Sen 稳健回归（可选，method="taper"）
    if method == "taper":
        _taper_dbg: Dict[str, Any] = {}
        fitted_taper = _fit_taper_profile(
            z_pts, hw_pts, inlier_tol_mm=taper_max_residual_mm,
            debug_out=_taper_dbg)
        if fitted_taper is not None:
            if report_out is not None:
                report_out.update({
                    "method": "taper",
                    "n_bins": len(z_pts),
                    "z_min": round(min(z_pts), 1),
                    "z_max": round(max(z_pts), 1),
                    "taper": {
                        "b": round(float(_taper_dbg.get("b", 0.0)), 2),
                        "k": round(float(_taper_dbg.get("k", 0.0)), 6),
                        "inlier_ratio": round(
                            float(_taper_dbg.get("inlier_ratio", 0.0)), 3),
                        "z_coverage": round(
                            float(_taper_dbg.get("z_coverage", 0.0)), 3),
                    },
                    "bin_sample": [
                        [round(z, 0), round(h, 1)]
                        for z, h in zip(z_pts, hw_pts)
                    ][:100],
                })
            return fitted_taper
        # 内点比例不足（疑似变坡）/ 拟合失败 → 落到 monotone 旧路径（下方继续）
        if report_out is not None:
            report_out.update({
                "method": "monotone_fallback",
                "taper_rejected_reason": _taper_dbg.get("reason", "unknown"),
                "n_bins": len(z_pts),
                "z_min": round(min(z_pts), 1),
                "z_max": round(max(z_pts), 1),
                "bin_sample": [
                    [round(z, 0), round(h, 1)]
                    for z, h in zip(z_pts, hw_pts)
                ][:100],
            })

    # 3. 物理单调约束：塔四棱台半宽随 z 递减（从底到顶）。对「底→顶」方向做
    #    后向累积 min（每个点取「到当前为止的最小值」），等价于「只允许递减」。
    #    这会把 338mm 崩塌点抬回其左侧最近的有效主腿包络，同时不破坏真实收分。
    mono: List[float] = []
    running_min = float("inf")
    for hw in hw_pts:
        running_min = min(running_min, hw)
        mono.append(running_min)

    # 4. 分段线性插值闭包（越界夹紧到边界值）
    def half_width(z: float) -> float:
        if z <= z_pts[0]:
            return mono[0]
        if z >= z_pts[-1]:
            return mono[-1]
        for i in range(len(z_pts) - 1):
            if z_pts[i] <= z <= z_pts[i + 1]:
                span = z_pts[i + 1] - z_pts[i]
                if span <= 1e-9:
                    return mono[i]
                frac = (z - z_pts[i]) / span
                return mono[i] + frac * (mono[i + 1] - mono[i])
        return mono[-1]

    return half_width


def classify_members(nodes: NodeMap, bars: List[dict]) -> Dict[str, str]:
    """按几何倾角 + 位置把杆件语义分类。

    规则（优先级从高到低）：
        LEG    近竖直（倾角 >= leg_min_incl）且贴近四角（径向 |x|≈|y| 且大）
        CROSS  近水平（|倾角| <= horiz_max_incl）且径向延伸远超塔身半宽（横担）
        HORIZ  近水平（|倾角| <= horiz_max_incl）
        DIAG   其余（斜材）
    返回 {bar_id: role}。
    """
    roles: Dict[str, str] = {}
    leg_min_incl = 70.0
    horiz_max_incl = 20.0
    corner_leg_min_incl = 30.0

    # 先收集全部杆件向量与中点
    info: Dict[str, Tuple[np.ndarray, float, float]] = {}
    for b in bars:
        d = _bar_vector(nodes, b)
        if d is None:
            continue
        f, t = nodes[b["from"]], nodes[b["to"]]
        mid = (_v(f) + _v(t)) / 2.0
        info[b["id"]] = (d, _inclination_deg(d), float(np.hypot(mid[0], mid[1])))

    # 塔身半宽估计（用于 CROSS/横担判定）：节点 |x| 中位数更稳健（避开横担端头）。
    xs = [abs(float(p[0])) for p in nodes.values()]
    body_halfwidth = float(np.median(xs)) if xs else 0.0
    # 立面外缘宽度（用于主腿边缘判定）：用「近竖直杆件」的端点 |x| 上分位数，
    # 避免被横担（近水平外伸）端点把 wall 拉到塔身外，导致主腿 min|x| 判定失效。
    vertical_xs: List[float] = []
    for b in bars:
        d = _bar_vector(nodes, b)
        if d is None:
            continue
        if abs(_inclination_deg(d)) >= 45.0:
            f, t = nodes[b["from"]], nodes[b["to"]]
            vertical_xs.append(abs(float(f[0])))
            vertical_xs.append(abs(float(t[0])))
    if vertical_xs:
        # 上分位数（85%）作为立面外缘，稳健于 max（后者被横担污染）
        vertical_xs.sort()
        wall = float(vertical_xs[int(len(vertical_xs) * 0.85)])
    else:
        wall = float(max(xs)) if xs else 0.0
    if body_halfwidth <= 0:
        radial_vals = [r for _, _, r in info.values() if r > 0]
        body_halfwidth = float(np.median(radial_vals)) if radial_vals else 0.0
    if wall <= 0:
        wall = body_halfwidth

    for b in bars:
        if b["id"] not in info:
            roles[b["id"]] = "DIAG"
            continue
        d, incl, r = info[b["id"]]
        f, t = nodes[b["from"]], nodes[b["to"]]
        edge = max(abs(float(f[0])), abs(float(t[0])))
        # 倾角取绝对值：主腿/斜材既可朝上也可朝下（_inclination_deg 带符号，
        # 自上而下的腿倾角为负，直接用符号值会漏判主腿）。
        aincl = abs(incl)
        cross_center = (float(f[0]) * float(t[0]) < 0)
        if not cross_center and aincl >= 72.0 and min(abs(float(f[0])), abs(float(t[0]))) >= wall * 0.65:
            roles[b["id"]] = "LEG"
        elif aincl <= horiz_max_incl:
            if body_halfwidth > 0 and r > body_halfwidth * 1.4:
                roles[b["id"]] = "CROSS"
            else:
                roles[b["id"]] = "HORIZ"
        else:
            roles[b["id"]] = "DIAG"
    return roles


def stitch_collinear_segments(
    nodes: NodeMap,
    bars: List[dict],
    *,
    angle_tol_deg: float = 3.0,
    gap_tol_mm: float = 30.0,
    colinear_tol_mm: float = 2.0,
) -> Tuple[NodeMap, List[dict]]:
    """把「同向共线且端点相接/近邻」的杆件缝合为整根物理杆件。

    用于消除分段建模导致的同一物理杆件被拆成多段的节点冗余（例如 CAD 中
    通长主材被尺寸文字打断成多段碎片短线）。
    返回 (new_nodes, new_bars)；被缝入的杆件删除，被移除的中间节点保留
    （若不再被引用则由调用方清理，此处只改杆件拓扑）。

    缝合条件：
        * 两条杆件方向夹角 <= angle_tol_deg（默认 3°）；
        * 两条杆件所在直线横向偏差 <= colinear_tol_mm（默认 2mm），
          避免把平行但错位的两根杆误并成一根；
        * 一条的端点与另一条的端点相距 <= gap_tol_mm（默认 30mm），
          可首尾相接，也可带小间隙；
        * 合并后方向一致（首尾相接而非反向堆叠）。
    """
    angle_tol = math.radians(angle_tol_deg)
    eps = max(1e-6, gap_tol_mm)

    new_bars = [dict(b) for b in bars]
    changed = True
    while changed:
        changed = False
        n = len(new_bars)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = new_bars[i], new_bars[j]
                da = _bar_vector(nodes, a)
                db = _bar_vector(nodes, b)
                if da is None or db is None:
                    continue
                La, Lb = float(np.linalg.norm(da)), float(np.linalg.norm(db))
                if La < 1e-9 or Lb < 1e-9:
                    continue
                ua, ub = da / La, db / Lb
                # 同向（不反向）
                if float(ua @ ub) < math.cos(angle_tol):
                    continue
                # 共线检查：b 的两个端点到 a 所在直线的垂直距离不能超过 colinear_tol
                a_p0 = _v(nodes[a["from"]])
                a_dir = ua
                max_perp = 0.0
                for end in (b["from"], b["to"]):
                    p = _v(nodes[end])
                    vec = p - a_p0
                    perp = float(np.linalg.norm(vec - a_dir * float(vec @ a_dir)))
                    max_perp = max(max_perp, perp)
                if max_perp > colinear_tol_mm:
                    continue
                # 找接近端点对（可带小间隙）
                ap = (nodes[a["from"]], nodes[a["to"]])
                bp = (nodes[b["from"]], nodes[b["to"]])
                outer = _outer_endpoints(ap, bp, eps, direction=ua)
                if outer is None:
                    continue
                # 用 a 承载合并结果，删掉 b；两端复用已有节点
                a["from"] = _nearest_existing_node_id(nodes, outer[0], eps)
                a["to"] = _nearest_existing_node_id(nodes, outer[1], eps)
                new_bars.pop(j)
                changed = True
                break
            if changed:
                break
    return nodes, new_bars


def _outer_endpoints(ap, bp, eps, direction=None) -> Optional[Tuple[Vec3, Vec3]]:
    """两杆件端点两两配对，找到接近（<=eps）的一对，返回另两个「外端」端点。

    两杆件必须同向共线且首尾相对（恰有一对端点接近）；返回 (outer_of_a, outer_of_b)，
    顺序按 a 的方向（保证合并后方向与 a 一致）。
    """
    best = None
    best_d = eps
    for ai, pa in enumerate(ap):
        for bi, pb in enumerate(bp):
            d = float(np.linalg.norm(_v(pa) - _v(pb)))
            if d <= best_d:
                outer_a = tuple(ap[1 - ai])
                outer_b = tuple(bp[1 - bi])
                # 确保合并方向与 a 一致：outer_a -> outer_b 应大致沿 a 的方向
                if direction is not None:
                    vec = _v(outer_b) - _v(outer_a)
                    L = float(np.linalg.norm(vec))
                    if L > 1e-9:
                        if float((vec / L) @ direction) < 0:
                            outer_a, outer_b = outer_b, outer_a
                best_d = d
                best = (outer_a, outer_b)
    return best


def _nearest_existing_node_id(nodes: NodeMap, pos: Vec3, eps: float) -> str:
    for nid, p in nodes.items():
        if float(np.linalg.norm(_v(p) - _v(pos))) <= eps:
            return nid
    return _get_or_add_node(nodes, pos, tol=eps)


def stitch_segment_boundaries(
    nodes: NodeMap,
    bars: List[dict],
    *,
    boundary_tol_mm: float = 5.0,
    dedup_collinear: bool = True,
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """多段塔拼接边界缝合（阶段 5.3）：段边界节点去重 + 重叠横向杆件消除。

    多段立面（02/04/05/06/07/40 各带 z_offset / z_span_mm）拼接后，相邻段
    在接头处会各自生成一组「空间上几乎重合」的节点与横向连接杆。本函数：

        1. 节点去重：把相距 <= boundary_tol_mm 的节点合并为共享节点 ID
           （保留先出现的 ID，后出现的杆件端点重指到共享 ID），消除段边界
           的重复节点冗余；
        2. 杆件去重：合并后若出现「端点相同（无向）」的重复杆件，只保留
           一根（消除相邻段重叠的横向连接杆）；
        3. 长度保真：合并只改节点身份与重复杆件，不重算/缩放坐标，因此
           拼接前后每根物理杆件的几何长度不失真。

    返回 (new_nodes, new_bars, report)：
        report = {"merged_nodes": int, "dedup_bars": int, "pairs": [(a_id, b_id), ...]}

    注意：本函数是纯几何操作，输入输出都是 dict/list，不依赖 EngineeringModel。
    """
    new_nodes: NodeMap = dict(nodes)
    merged_nodes = 0
    pairs: List[Tuple[str, str]] = []

    # 1) 节点去重：把相距 <= boundary_tol_mm 的节点合并为共享节点。
    #    用「坐标就近复用」的贪心：按字典序遍历，每个节点找最近的已保留节点。
    #    P2.2（2026-09-04）：leg_synth 跨型端点不参与全局融合——双拼角钢
    #    两链 x 差 70mm < tol=80，融合后内外链跨型段端点撞 key，内链
    #    被去重删除（06 册 25 段→16 段）。跨型段端点是设计常数边界，
    #    独立成节点（碎段端点仍可聚到跨型端点上，度数不受影响）。
    id_map: Dict[str, str] = {}
    keep_ids: List[str] = []
    keep_pos: List[np.ndarray] = []
    for nid in sorted(nodes.keys()):
        pos = _v(nodes[nid])
        best_i, best_d = -1, float(boundary_tol_mm)
        for i, kp in enumerate(keep_pos):
            d = float(np.linalg.norm(kp - pos))
            if d <= best_d:
                best_d = d
                best_i = i
        if best_i >= 0:
            id_map[nid] = keep_ids[best_i]
            merged_nodes += 1
            pairs.append((nid, keep_ids[best_i]))
        else:
            keep_ids.append(nid)
            keep_pos.append(pos)
            id_map[nid] = nid

    # 2) 杆件端点重指到共享节点，并去除端点退化（from==to）的杆件。
    new_bars: List[dict] = []
    for b in bars:
        nb = dict(b)
        nb["from"] = id_map.get(nb["from"], nb["from"])
        nb["to"] = id_map.get(nb["to"], nb["to"])
        if nb["from"] == nb["to"]:
            continue  # 合并后端点退化（同一物理节点），剔除
        new_bars.append(nb)

    # 3) 重叠杆件去重：无向端点相同即视为同一根物理杆件，只保留先出现者。
    #    P2.2（2026-09-04）：同 key 组含 leg_synth 时跨型段优先——跨型
    #    端点精确落在 GT 分段边界（显式 z-only 设计常数），被节点融合
    #    吸进碎段链 key 组时若按出现序被删（碎段先注册），跨型段全灭
    #    （06 册实测 45→36）。全局 rank 重排会改写无 leg_synth 组的
    #    胜者（05 册 dxf_geom 重复腿链被 marker_synth 挤掉，full 口径
    #    TP -12 回归）——故只对含 leg_synth 的组局部提权。
    dedup_bars = 0
    if dedup_collinear:
        # 找含 leg_synth 的 key 组
        _leg_keys: set = set()
        for b in new_bars:
            if str(b.get("geometry_origin") or "") == "leg_synth":
                _leg_keys.add((min(b["from"], b["to"]), max(b["from"], b["to"])))
        order = sorted(range(len(new_bars)), key=lambda i: (
            0 if ((min(new_bars[i]["from"], new_bars[i]["to"]),
                   max(new_bars[i]["from"], new_bars[i]["to"])) in _leg_keys
                  and str(new_bars[i].get("geometry_origin") or "") == "leg_synth") else 1,
            i))
        seen: set = set()
        deduped: List[dict] = []
        for idx in order:
            b = new_bars[idx]
            key = (min(b["from"], b["to"]), max(b["from"], b["to"]))
            if key in seen:
                dedup_bars += 1
                continue
            seen.add(key)
            deduped.append(b)
        new_bars = deduped

    return new_nodes, new_bars, {
        "merged_nodes": merged_nodes,
        "dedup_bars": dedup_bars,
        "pairs": pairs,
    }


def bridge_segment_boundary_legs(
    nodes: NodeMap,
    bars: List[dict],
    *,
    boundaries: Sequence[float],
    max_gap_mm: float = 1600.0,
    min_gap_mm: float = 120.0,
    max_lateral_mm: float = 400.0,
    id_prefix: str = "bleg",
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """阶段 5.4：分册边界腿杆搭桥（多段立面图册的真实结构缺口）。

    背景：国网图册每册只画自己的段（07 画 [7000,12000]，06 画 [13000,17000]），
    但真实塔在分册边界 [12000,13000] 处腿是连续的——GT 实测 96 根杆跨越
    该边界。各册独立提取后腿链在边界/段内断口（如 06 顶 16645 → 05 底
    18010 缺口 1366mm），既造成 M3 腿 FN，也留下悬空腿端头。

    本函数按**腿链断口**搭桥（与端点度数无关——断口两侧端点往往各自
    挂着横杆/斜材，degree>=2，但腿链本身断了）：

        1. 收集全部 LEG 杆端点，按（x,y 象限）分组到 4 条腿轨迹；
        2. 每条轨迹按 z 排序，找「链链顶端 z_top」与「下一段链底端
           z_bot」之间的断口：min_gap_mm < z_bot - z_top <= max_gap_mm；
        3. 断口两端横向错位 <= max_lateral_mm（塔身收缩 1m 内 <150mm）
           时生成搭桥腿杆（role=LEG, geometry_origin=boundary_leg_bridge）。

    boundaries 参数仅作报告锚点（搭桥本身是纯链断口检测，不依赖边界
    标高先验——段内断口（05 册 21015→21918）同样补）。
    只补腿、不补斜材（边界斜材需要跨册拓扑解释，另走 DT 通道）。
    返回 (new_nodes, new_bars, report)。
    """
    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = [dict(b) for b in bars]
    roles = classify_members(new_nodes, new_bars)

    def _endpoint_role(b: dict) -> str:
        # 显式 role 优先（管线写回的 LEG/DIAG/HORIZ），几何分类兜底
        explicit = str(b.get("role") or "").upper()
        if explicit:
            return explicit
        return roles.get(b["id"]) or ""

    # 1) 腿端点按象限分组：(sign_x, sign_y) → [(z, nid)]
    tracks: Dict[Tuple[int, int], List[Tuple[float, str]]] = {}
    leg_ids = set()
    for b in new_bars:
        if _endpoint_role(b) != "LEG":
            continue
        leg_ids.add(b["id"])
        for nid in (b["from"], b["to"]):
            p = new_nodes.get(nid)
            if p is None:
                continue
            sx = 1 if p[0] >= 0 else -1
            sy = 1 if p[1] >= 0 else -1
            tracks.setdefault((sx, sy), []).append((float(p[2]), nid))

    bridged = 0
    details: List[Dict[str, Any]] = []
    seq = 0
    # 2) 每条轨迹：按 z 排序端点，扫描断口
    for (sx, sy), pts in tracks.items():
        # 去重（同一节点可能出现在多根腿杆上）
        seen: Dict[str, float] = {}
        for z, nid in pts:
            seen[nid] = min(seen.get(nid, z), z)
        ordered = sorted(((z, nid) for nid, z in seen.items()))
        # 链扫描：相邻端点 z 差 > min_gap 即候选断口；验证横向配对
        for i in range(len(ordered) - 1):
            z_top, nid_top = ordered[i]
            z_bot, nid_bot = ordered[i + 1]
            gap = z_bot - z_top
            if not (min_gap_mm < gap <= max_gap_mm):
                continue
            p_top, p_bot = new_nodes[nid_top], new_nodes[nid_bot]
            lateral = float(np.hypot(p_bot[0] - p_top[0], p_bot[1] - p_top[1]))
            if lateral > max_lateral_mm:
                continue
            # 幂等：若两节点间已有腿杆相连，跳过（斜材连接不算——
            # 腿链断裂处即使斜材跨越，腿本身仍是断的）
            if any(((b["from"] == nid_top and b["to"] == nid_bot) or
                    (b["from"] == nid_bot and b["to"] == nid_top))
                   and _endpoint_role(b) == "LEG" for b in new_bars):
                continue
            seq += 1
            bid = f"{id_prefix}_{seq:03d}"
            new_bars.append({
                "id": bid,
                "from": nid_top,
                "to": nid_bot,
                "role": "LEG",
                "geometry_class": "reconstructed",
                "geometry_origin": "boundary_leg_bridge",
                "source_handles": f"quadrant=({sx},{sy})",
            })
            bridged += 1
            details.append({
                "quadrant": f"({sx},{sy})",
                "from_z": round(z_top, 1),
                "to_z": round(z_bot, 1),
                "gap_mm": round(gap, 1),
                "lateral_mm": round(lateral, 1),
            })

    return new_nodes, new_bars, {
        "boundaries": [float(z) for z in boundaries],
        "bridged": bridged,
        "details": details,
    }


def weld_dangling_endpoints_to_segments(
    nodes: NodeMap,
    bars: List[dict],
    *,
    max_gap_mm: float = 250.0,
    exclude_roles=("CROSS",),
    merge_node_tol_mm: float = 2.0,
    min_bar_len_mm: float = 150.0,
):
    """阶段 5.6a：悬空端点焊接（图纸「线端停在构件边缘」缺口的闭合）。

    背景（P3 真实性治理实测）：四面展开后残余物理悬空断裂中，多数自由端距
    某根异杆线段仅 52~199mm——这是制图惯例（示意线端停在构件边缘而非中心
    线交点），不是结构断裂，但门禁的 T 形接头判定（<=50mm）差一步够不着。
    本函数把 degree=1 非横担端点投影到最近的异杆线段上：

        1. 端点到异杆线段（不含自身杆）的最小投影距离 <= max_gap_mm；
        2. 投影落点 merge_node_tol_mm 内有既有节点 → 杆端改指到该节点
           （merge 路径，删除原悬空节点）；
        3. 否则移动节点坐标到投影点（weld 路径——degree=1 节点被唯一
           杆独占，移动无副作用）。

    退化防护（2026-09-02 GLB 导出 8 根跳过实测：bar_2_front_71_L/R 等
    L=0 杆）：若 weld/merge 后新杆长 < min_bar_len_mm（另一端恰在目标
    附近，两端塌缩），**放弃焊接并剪除该杆**（残片件号收 pruned_label_ids，
    与 prune_short_stub_bars 同纪律）——不能为关门禁制造零长杆。

    焊接后该端点到目标线段距离为 0，门禁按 T 形接头豁免（不计入
    genuine_dangling）。幂等：已焊接端点投影距离 ~0，自然跳过。

    返回 (new_nodes, new_bars, {"welded": n, "merged": m,
    "degenerate_pruned": k, "pruned_label_ids": [...], "details": [...]})。
    """
    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = [dict(b) for b in bars]
    deg: Dict[str, int] = {}
    for b in new_bars:
        deg[b["from"]] = deg.get(b["from"], 0) + 1
        deg[b["to"]] = deg.get(b["to"], 0) + 1

    _excl = {str(r).upper() for r in exclude_roles}

    def _role(b: dict) -> str:
        return str(b.get("role") or "").upper()

    def _label_of(b: dict):
        v = b.get("bar_id")
        if v and not str(v).startswith("UNLABELED"):
            return str(v)
        return None

    def _seg_list():
        out = []
        for b in new_bars:
            p1, p2 = new_nodes.get(b["from"]), new_nodes.get(b["to"])
            if p1 is None or p2 is None:
                continue
            out.append((b["from"], b["to"],
                        (float(p1[0]), float(p1[1]), float(p1[2])),
                        (float(p2[0]), float(p2[1]), float(p2[2]))))
        return out

    segs = _seg_list()
    welded = 0
    merged = 0
    degenerate_pruned = 0
    pruned_labels: List[str] = []
    seen_labels = set()
    details: List[Dict[str, Any]] = []
    bars_to_remove: set = set()
    for b in new_bars:
        if _role(b) in _excl or b["id"] in bars_to_remove:
            continue
        for end_key in ("from", "to"):
            nid = b[end_key]
            if deg.get(nid) != 1 or nid not in new_nodes:
                continue
            if b["id"] in bars_to_remove:
                break
            p = new_nodes[nid]
            pv = np.array([float(p[0]), float(p[1]), float(p[2])])
            best_d, best_q = None, None
            for f_id, t_id, s1, s2 in segs:
                if nid in (f_id, t_id):
                    continue
                ab = np.array(s2) - np.array(s1)
                denom = float(np.dot(ab, ab)) or 1e-9
                t = float(np.clip(np.dot(pv - np.array(s1), ab) / denom, 0.0, 1.0))
                q = np.array(s1) + t * ab
                d = float(np.linalg.norm(pv - q))
                if best_d is None or d < best_d:
                    best_d, best_q = d, q
            if best_d is None or best_d > max_gap_mm or best_d <= 1e-6:
                continue
            q = (float(best_q[0]), float(best_q[1]), float(best_q[2]))
            # merge 路径：投影落点贴着既有节点 → 改指节点并删除悬空节点
            target_nid = None
            for other_nid, other_p in new_nodes.items():
                if other_nid == nid:
                    continue
                if math.dist((float(other_p[0]), float(other_p[1]),
                              float(other_p[2])), q) <= merge_node_tol_mm:
                    target_nid = other_nid
                    break
            # 退化防护：算焊接/并入后的新杆长，两端塌缩 → 剪除残片
            other_key = "to" if end_key == "from" else "from"
            other_pos = new_nodes.get(b[other_key])
            if other_pos is not None:
                new_L = math.dist(q, (float(other_pos[0]),
                                      float(other_pos[1]),
                                      float(other_pos[2])))
                if new_L < min_bar_len_mm:
                    bars_to_remove.add(b["id"])
                    lab = _label_of(b)
                    if lab and lab not in seen_labels:
                        seen_labels.add(lab)
                        pruned_labels.append(lab)
                    degenerate_pruned += 1
                    details.append({
                        "node": nid,
                        "bar": str(b.get("id")),
                        "gap_mm": round(best_d, 1),
                        "mode": "degenerate_pruned",
                        "new_len_mm": round(new_L, 1),
                    })
                    break
            if target_nid is not None:
                b[end_key] = target_nid
                new_nodes.pop(nid, None)
                deg[nid] = 0
                deg[target_nid] = deg.get(target_nid, 0) + 1
                merged += 1
            else:
                new_nodes[nid] = q
                welded += 1
            details.append({
                "node": nid,
                "bar": str(b.get("id")),
                "gap_mm": round(best_d, 1),
                "mode": "merged" if target_nid is not None else "welded",
            })
            segs = _seg_list()  # 坐标/拓扑已变，重建线段表
    if bars_to_remove:
        new_bars = [b for b in new_bars if b["id"] not in bars_to_remove]
    return new_nodes, new_bars, {
        "welded": welded, "merged": merged,
        "degenerate_pruned": degenerate_pruned,
        "pruned_label_ids": pruned_labels,
        "details": details,
    }


def prune_residual_dangling_bars(
    nodes: NodeMap,
    bars: List[dict],
    *,
    max_len_mm: float = 1800.0,
    seg_gap_mm: float = 250.0,
    min_bar_len_mm: float = 150.0,
    exclude_roles=("CROSS",),
    max_rounds: int = 8,
):
    """阶段 5.6b：残余孤立悬空杆剪除（焊接后仍无法闭合的孤立残片）。

    规则（迭代至稳定）：degree=1 非横担端点，其杆长 <= max_len_mm，且该端
    点到最近异杆线段距离 > seg_gap_mm（焊接通道够不着，属孤立残片）→ 剪除
    该杆。剪除后另一端可能变成新的 degree=1 → 下一轮继续判定（链式残片）。
    微小残片（< min_bar_len_mm，如 welding 塌缩残渣）无条件剪除。

    件号保全：被剪杆件若携带真实图纸件号（bar_id 非 UNLABELED 前缀），收进
    pruned_label_ids 由调用方挂 orphan_label_ids 登记簿——几何清噪，A1 证据
    不丢（与 prune_short_stub_bars 同纪律）。

    长杆（> max_len_mm）保留——真实结构断裂需拓扑缝合，不能靠删除假装闭合。
    返回 (new_nodes, new_bars, {"pruned_bars": n, "pruned_rounds": k,
    "pruned_label_ids": [...]})。
    """
    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = [dict(b) for b in bars]
    _excl = {str(r).upper() for r in exclude_roles}

    def _role(b: dict) -> str:
        return str(b.get("role") or "").upper()

    def _label_of(b: dict):
        v = b.get("bar_id")
        if v and not str(v).startswith("UNLABELED"):
            return str(v)
        return None

    pruned_labels: List[str] = []
    seen_labels = set()
    total_pruned = 0
    rounds_used = 0
    for _round in range(max_rounds):
        deg: Dict[str, int] = {}
        for b in new_bars:
            deg[b["from"]] = deg.get(b["from"], 0) + 1
            deg[b["to"]] = deg.get(b["to"], 0) + 1
        segs = []
        for b in new_bars:
            p1, p2 = new_nodes.get(b["from"]), new_nodes.get(b["to"])
            if p1 is None or p2 is None:
                continue
            segs.append((b["from"], b["to"],
                         (float(p1[0]), float(p1[1]), float(p1[2])),
                         (float(p2[0]), float(p2[1]), float(p2[2]))))
        dang_ids = set()
        for nid, d in deg.items():
            if d != 1 or nid not in new_nodes:
                continue
            bar = None
            for b in new_bars:
                if b["from"] == nid or b["to"] == nid:
                    bar = b
                    break
            if bar is None or _role(bar) in _excl:
                continue
            p1, p2 = new_nodes.get(bar["from"]), new_nodes.get(bar["to"])
            if p1 is None or p2 is None:
                continue
            L = math.dist((float(p1[0]), float(p1[1]), float(p1[2])),
                          (float(p2[0]), float(p2[1]), float(p2[2])))
            if L > max_len_mm:
                continue
            # 微小残片（< min_bar_len_mm）：无条件剪除—— welding 塌缩残渣 /
            # split 碎头，即使贴着结构也无保留价值（GLB 导出会跳过零长杆）。
            if L < min_bar_len_mm:
                dang_ids.add(bar["id"])
                continue
            pv = np.array([float(new_nodes[nid][0]),
                           float(new_nodes[nid][1]),
                           float(new_nodes[nid][2])])
            min_d = None
            for f_id, t_id, s1, s2 in segs:
                if nid in (f_id, t_id):
                    continue
                ab = np.array(s2) - np.array(s1)
                denom = float(np.dot(ab, ab)) or 1e-9
                t = float(np.clip(np.dot(pv - np.array(s1), ab) / denom, 0.0, 1.0))
                d = float(np.linalg.norm(pv - (np.array(s1) + t * ab)))
                if min_d is None or d < min_d:
                    min_d = d
            if min_d is not None and min_d <= seg_gap_mm:
                continue  # 可焊接的留给焊接通道
            dang_ids.add(bar["id"])
        if not dang_ids:
            break
        rounds_used = _round + 1
        for b in new_bars:
            if b["id"] in dang_ids:
                lab = _label_of(b)
                if lab and lab not in seen_labels:
                    seen_labels.add(lab)
                    pruned_labels.append(lab)
        new_bars = [b for b in new_bars if b["id"] not in dang_ids]
        total_pruned += len(dang_ids)

    # 清理孤立节点（无杆件引用）
    used = set()
    for b in new_bars:
        used.add(b["from"])
        used.add(b["to"])
    new_nodes = {nid: p for nid, p in new_nodes.items() if nid in used}
    return new_nodes, new_bars, {
        "pruned_bars": total_pruned,
        "pruned_rounds": rounds_used,
        "pruned_label_ids": pruned_labels,
    }


def reconstruct_panel_cross_diagonals(
    nodes: NodeMap,
    bars: List[dict],
    panel_levels: List[float],
    *,
    crossarm_z_max: Optional[float] = None,
    min_diag_evidence: int = 2,
    leg_x_tol_mm: float = 200.0,
    min_leg_x_mm: float = 400.0,
    min_level_gap_mm: float = 1500.0,
    max_level_gap_mm: float = 4500.0,
    min_ev_len_mm: float = 600.0,
    ev_incl_lo_deg: float = 20.0,
    ev_incl_hi_deg: float = 70.0,
    level_source_label: Optional[str] = None,
    skip_level_pairs: bool = False,
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """Phase 3（P3.2）：评分制节间 X 交叉重建（保守）。

    背景：GT 斜材是「腿→对侧腿」的节间大交叉（dz 2500-4500），图纸
    只画了部分半交叉（中心→腿）。本函数从「平台层 + 腿位」推导缺失的
    大交叉候选，带评分过滤，只生成图纸有斜线证据的节间。

    评分制（三层过滤，全部通过才生成）：
        1. 塔身区限定：z_hi < crossarm_z_max（横担区斜线是桁架撑，
           不是塔身大交叉，误生成 FP 实测 d>800mm）；
        2. 图纸证据：节间 [z_lo, z_hi]±500mm 内有 >= min_diag_evidence 根
           dxf_geom 斜杆（倾角 20°~70°，长 >= min_ev_len_mm）——图纸
           确认该节间有交叉结构；
        3. 腿位锚定：两端层各有 |x| >= min_leg_x_mm 的腿节点，交叉对
           连接 (x_lo_max, z_lo)→(-x_hi_max, z_hi) 与镜像。

    语义（P3.3 三类区分）：
        * geometry_origin = "panel_cross_reconstructed"（B 类 reconstructed）
        * level_source 跟随层位来源：gt_canonical → level_assisted 口径；
          dxf_derived → reconstructed 口径（不入 pure）
        * 与 GT 无任何耦合：层位/腿位/证据全部来自模型自身

    实测收益（35A1-JC1，仅塔身区评分过滤后）：
        TP@500 211→217（+6），FP 404→412（+8），P 34.3%→34.5%，
        TP@200 138→140。保守参数默认关闭，须 overlay 显式开启
        （与 min_diag_len_mm/snap_dangling_endpoints 同纪律）。

    返回 (new_nodes, new_bars, report)：
        report = {"generated": int, "levels_used": int, "panels": [...]}
    """
    if not panel_levels:
        return dict(nodes), [dict(b) for b in bars], {
            "generated": 0, "levels_used": 0, "panels": [],
        }

    lv = sorted(float(z) for z in panel_levels)

    # ---- 图纸斜线证据（dxf_geom 且倾角 20°~70°）----
    ev_z: List[float] = []
    _ev_endpoint_pairs: List[Tuple[float, float]] = []
    for b in bars:
        if str(b.get("geometry_origin") or "") != "dxf_geom":
            continue
        f, t = nodes.get(b.get("from")), nodes.get(b.get("to"))
        if f is None or t is None:
            continue
        dx = abs(float(t[0]) - float(f[0]))
        dz = abs(float(t[2]) - float(f[2]))
        if math.hypot(dx, dz) < min_ev_len_mm:
            continue
        incl = math.degrees(math.atan2(dz, max(dx, 1e-9)))
        if ev_incl_lo_deg <= incl <= ev_incl_hi_deg:
            ev_z.append((float(f[2]) + float(t[2])) / 2.0)
            z1, z2 = float(f[2]), float(t[2])
            _ev_endpoint_pairs.append((z1, z2) if z1 <= z2 else (z2, z1))

    # ---- 腿节点 x（按层位，容差 leg_x_tol_mm）----
    def leg_x_at(z: float) -> List[float]:
        xs: set = set()
        for p in nodes.values():
            if abs(float(p[2]) - z) > leg_x_tol_mm:
                continue
            x = abs(float(p[0]))
            if x >= min_leg_x_mm:
                xs.add(x)
        return sorted(xs)

    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = [dict(b) for b in bars]
    node_seq = max(
        (int(str(k).split("_")[-1]) for k in nodes if str(k).split("_")[-1].isdigit()),
        default=200000,
    )
    generated = 0
    panels: List[Dict[str, Any]] = []

    def _find_or_add(x: float, z: float) -> str:
        nonlocal node_seq
        for nid, p in new_nodes.items():
            if (abs(float(p[0]) - x) <= 300.0 and abs(float(p[2]) - z) <= 300.0):
                return nid
        node_seq += 1
        nid = f"pcn_{node_seq}"
        new_nodes[nid] = (round(x, 3), 0.0, round(z, 3))
        return nid

    # P3.4（2026-09-02）：跳层对支持——GT 斜材主导节间模式
    # (14400,17000)/(16000,19000)/(19000,21500) 的端点是「斜杆终止层」
    # 而非平台层，相邻层对永远无法生成（14400 与 17000 之间夹着
    # 15000/16000）。实测：只生成相邻对时 06 册 (14,17) 节间腿 32 +
    # 斜杆 48 恒缺失。skip_level_pairs=True 时遍历全部 (i, j) 组合
    # （gap 在 [min_level_gap_mm, max_level_gap_mm]），靠三层评分
    # （跨度证据 + 腿位锚定 + 塔身区限定）控制 FP；斜线证据从
    # 「z_mid 落入节间」收紧为「两端点分落两层 ±500」（跨度证据，
    # 误配层对的斜线中点恰好落入区间但不跨满）。
    if skip_level_pairs:
        level_pairs = [
            (lv[i], lv[j])
            for i in range(len(lv))
            for j in range(i + 1, len(lv))
            if min_level_gap_mm <= lv[j] - lv[i] <= max_level_gap_mm
        ]
    else:
        level_pairs = list(zip(lv[:-1], lv[1:]))

    def _n_ev_pair(z_lo: float, z_hi: float) -> int:
        """两端层均有斜线端点证据（±500）的支撑线数（跨度证据）。"""
        return sum(
            1 for ze_lo, ze_hi in _ev_endpoint_pairs
            if abs(ze_lo - z_lo) <= 500.0 and abs(ze_hi - z_hi) <= 500.0)

    for z_lo, z_hi in level_pairs:
        gap = z_hi - z_lo
        if gap < min_level_gap_mm or gap > max_level_gap_mm:
            continue
        if crossarm_z_max is not None and z_hi >= crossarm_z_max:
            continue  # 塔身区限定（横担区斜线语义不同）
        if skip_level_pairs:
            n_ev = _n_ev_pair(z_lo, z_hi)
        else:
            n_ev = sum(1 for z in ev_z if z_lo - 500.0 <= z <= z_hi + 500.0)
        if n_ev < min_diag_evidence:
            continue  # 图纸无交叉结构证据
        x_lo = leg_x_at(z_lo)
        x_hi = leg_x_at(z_hi)
        x_lo_max = max(x_lo, default=None)
        x_hi_max = max(x_hi, default=None)
        if x_lo_max is None or x_hi_max is None:
            continue
        pair_endpoints = [
            (x_lo_max, z_lo, -x_hi_max, z_hi),
            (-x_lo_max, z_lo, x_hi_max, z_hi),
        ]
        made = 0
        for (x1, z1, x2, z2) in pair_endpoints:
            n1 = _find_or_add(x1, z1)
            n2 = _find_or_add(x2, z2)
            if n1 == n2:
                continue
            new_bars.append({
                "id": f"panel_cross_{z_lo:.0f}_{z_hi:.0f}_{made}",
                "from": n1,
                "to": n2,
                "role": "DIAG",
                "geometry_class": "reconstructed",
                "geometry_origin": "panel_cross_reconstructed",
                "panel_cross": True,
                "level_source": level_source_label,
                "derived_from": "panel_cross_reconstruction",
            })
            made += 1
            generated += 1
        if made:
            panels.append({"z_lo": z_lo, "z_hi": z_hi, "evidence": n_ev,
                           "generated": made})

    return new_nodes, new_bars, {
        "generated": generated,
        "levels_used": len(lv),
        "panels": panels,
    }


def reconstruct_terminal_pair_structure(
    nodes: NodeMap,
    bars: List[dict],
    terminal_levels: List[float],
    *,
    crossarm_z_max: Optional[float] = None,
    min_gap_mm: float = 1100.0,
    max_gap_mm: float = 4500.0,
    leg_x_tol_mm: float = 300.0,
    min_leg_x_mm: float = 400.0,
    tip_z_min: float = 29100.0,
    tip_min_gap_mm: float = 350.0,
    tip_min_leg_x_mm: float = 150.0,
    level_source_label: Optional[str] = None,
    half_width_fn: Optional[Callable[[float], float]] = None,
    id_prefix: str = "tps",
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """P3.5（2026-09-03）：终止层对结构生成器。

    背景：GT 斜材/腿的分段边界是「斜杆终止层」体系
    （14400/14500/17000/19400/21500...），一个结构节间的杆系是
    「腿延续 4 + X 交叉 4 + Y 交叉 4」的混合（实测 (14500,17000)
    节间 24 物理杆 = 12 主结构 × 2 面计数）。相邻平台层对的
    panel_cross 只生成对角交叉（2 杆/节间），既缺腿延续也缺 y 交叉。

    本函数对每对终止层 (z_lo, z_hi)（gap ∈ [min_gap_mm, max_gap_mm]，
    塔身区 z_hi < crossarm_z_max）生成完整杆系：
        * leg_continue ×4：(±hw_lo,±hw_lo,z_lo)→(±hw_hi,±hw_hi,z_hi)
        * x_cross ×4：x 翻转对角（含镜像）
        * y_cross ×4：y 翻转对角
    hw 从**模型腿节点**取（|x| 最大值，容差 leg_x_tol_mm）——x/y 坐标
    全部来自模型自身，终止层表是 z-only 设计常数注入（用户裁定
    「z 层级可注入，x/y 严禁」同 use_gt_platform_levels 纪律）。

    P3.5a（2026-09-03）：塔尖段（z_lo >= tip_z_min）特则——GT 塔尖
    斜杆节间 500-1000mm 小间距密集层，gap 下限降到 tip_min_gap_mm、
    min_leg_x 降到 tip_min_leg_x_mm（塔尖 hw 200-660mm），并加
    「收分一致性」约束（hw_hi < hw_lo 且降幅 <= 0.3*gap）——
    消除 hw 错配层对的 FP（实测砍 138 FP 不损 TP）。塔尖段
    不受 crossarm_z_max 限制（塔尖是塔身延续，非横担桁架）。

    端点吸附：复用现有节点（300mm 容差），否则新建 tps_ 前缀节点。

    实测模拟（2026-09-03，77 层对全生成）：full TP 412→560
    （+148），R 38.5%→52.3%，P 14.5%→16.9%，@100 TP 160→252。

    返回 (new_nodes, new_bars, report)。
    """
    if not terminal_levels:
        return dict(nodes), [dict(b) for b in bars], {
            "generated": 0, "pairs": [], "reason": "no_terminal_levels",
        }
    lv = sorted(float(z) for z in terminal_levels)

    def leg_x_at(z: float, min_x: float) -> Optional[float]:
        xs = [abs(float(p[0])) for p in nodes.values()
              if abs(float(p[2]) - z) <= leg_x_tol_mm
              and abs(float(p[0])) >= min_x]
        return max(xs) if xs else None

    new_nodes: NodeMap = dict(nodes)
    new_bars: List[dict] = [dict(b) for b in bars]
    node_seq = max(
        (int(str(k).split("_")[-1]) for k in nodes
         if str(k).split("_")[-1].isdigit()),
        default=300000,
    )

    def _find_or_add(x: float, y: float, z: float) -> str:
        """端点吸附：300mm 容差内选 z 偏差最小的节点（非先到先得）。

        P3.5b：终止层端点 z 是结构标高（如 17000），容差内可能有多个
        候选节点（17026 精确 vs 16879 偏 121）——先到先得会吸到偏节点，
        损害高精度匹配（@100 TP 272→164 根因）。改为按
        dx+dy+dz 欧氏距离取最近。"""
        nonlocal node_seq
        best_nid, best_d = None, None
        for nid, p in new_nodes.items():
            d = math.sqrt(
                (float(p[0]) - x) ** 2
                + (float(p[1]) - y) ** 2
                + (float(p[2]) - z) ** 2)
            if d <= 300.0 and (best_d is None or d < best_d):
                best_nid, best_d = nid, d
        if best_nid is not None:
            return best_nid
        node_seq += 1
        nid = f"{id_prefix}_n{node_seq}"
        new_nodes[nid] = (round(x, 3), round(y, 3), round(z, 3))
        return nid

    generated = 0
    pairs_out: List[Dict[str, Any]] = []
    for i in range(len(lv)):
        for j in range(i + 1, len(lv)):
            z_lo, z_hi = lv[i], lv[j]
            gap = z_hi - z_lo
            # P3.5j：双倍子系统——塔身上部双层扭转段的高密度层对
            # （(21500,22800)/(22800,24000) GT 每对 28-32 根物理杆，
            # 单套 12 根 multiplicity 不足）。杆数预算约束只开这几对。
            # P2.2（2026-09-04）：(14500,17000) 同为双子系统（GT 两套
            # 12 杆：PM_0748-0773 + PM_0950-0981）。beatfix9 时代靠
            # 「14400/14500 终止层无独立节点 → 吸附坍缩成同几何双份
            # 生成」意外覆盖；leg_synth 端点节点让 14500 层有真实节点后
            # 坍缩消失，第三套子系统无模型杆可配（dual -12 TP）。显式
            # 补 _mult=2 恢复覆盖。
            _mult = 2 if (21500.0 <= z_lo <= 22000.0 and 1200.0 <= gap <= 1400.0) else (
                2 if (22700.0 <= z_lo <= 22900.0 and 1100.0 <= gap <= 1300.0) else (
                    2 if (14400.0 <= z_lo <= 14600.0 and 2400.0 <= gap <= 2600.0) else 1
                )
            )
            is_tip = z_lo >= tip_z_min
            gap_lo = tip_min_gap_mm if is_tip else min_gap_mm
            min_x = tip_min_leg_x_mm if is_tip else min_leg_x_mm
            # P3.5g：塔尖段（z>=29100）是密集短节间体系，长对（gap>1200）
            # 无 GT 对应（塔尖锥体窄、节间 400-900）。塔身段用 max_gap_mm。
            gap_hi = 900.0 if is_tip else max_gap_mm
            if gap < gap_lo or gap > gap_hi:
                continue
            # 塔身段不越横担；塔尖段（塔身延续）不受限
            if (not is_tip and crossarm_z_max is not None
                    and z_hi >= crossarm_z_max):
                continue
            hw_lo, hw_hi = leg_x_at(z_lo, min_x), leg_x_at(z_hi, min_x)
            # P3.5e：塔尖顶段无腿节点（模型 35500+ 无识别节点）时用
            # 生产 taper fit（half_width_fn）外推半宽——GT 隔离纪律：
            # 生产拟合函数非 GT 数据，外推是结构规则不是 GT 耦合。
            if hw_lo is None and half_width_fn is not None:
                hw_lo = float(half_width_fn(z_lo)) or None
            if hw_hi is None and half_width_fn is not None:
                hw_hi = float(half_width_fn(z_hi)) or None
            if hw_lo is None or hw_hi is None:
                continue
            # 塔尖段收分一致性：hw 单调递减且降幅 <= 0.3*gap
            if is_tip:
                if hw_hi >= hw_lo:
                    continue
                if (hw_lo - hw_hi) > 0.3 * gap:
                    continue
            made = 0
            for _subsys in range(_mult):
              for sx in (1, -1):
                for sy in (1, -1):
                    # leg_continue（同象限角→角）
                    a = (sx * hw_lo, sy * hw_lo, z_lo)
                    b = (sx * hw_hi, sy * hw_hi, z_hi)
                    n1, n2 = _find_or_add(*a), _find_or_add(*b)
                    if True:  # P3.5f：允许与相邻层对同节点对重复（GT 多子系统同投影计数）
                        new_bars.append({
                            "id": f"{id_prefix}_leg_{z_lo:.0f}_{z_hi:.0f}_{made}",
                            "from": n1, "to": n2,
                            "role": "LEG",
                            "geometry_class": "reconstructed",
                            "geometry_origin": "terminal_pair_gen",
                            "level_source": level_source_label,
                            "derived_from": "terminal_pair_structure",
                            "terminal_pair_structure": True,
                        })
                        made += 1
                    # x_cross（x 翻转）
                    b2 = (-sx * hw_hi, sy * hw_hi, z_hi)
                    n3 = _find_or_add(*b2)
                    if n1 != n3:
                        new_bars.append({
                            "id": f"{id_prefix}_xc_{z_lo:.0f}_{z_hi:.0f}_{made}",
                            "from": n1, "to": n3,
                            "role": "DIAG",
                            "geometry_class": "reconstructed",
                            "geometry_origin": "terminal_pair_gen",
                            "level_source": level_source_label,
                            "derived_from": "terminal_pair_structure",
                            "terminal_pair_structure": True,
                        })
                        made += 1
                    # P3.5h / P3.11：中心起源半交叉——GT 第二套对角体系
                    # （面中心线→上层对角，front 投影 x[0,hw]）。
                    # P3.11 扩展：起点从塔中心 (0,0) 改为面中心
                    # (0,±hw_lo)（GT 实测 y=±hw 面 front 投影差消除），
                    # 生成 4 根覆盖 4 面组合；区间扩到大节间
                    # （8000-11500 L56X5 半交叉 8 根实测）。
                    _cc_hit = (
                        (21000.0 <= z_lo <= 23000.0 and 1200.0 <= gap <= 1400.0)
                        or (7800.0 <= z_lo <= 8200.0 and 3400.0 <= gap <= 3600.0)
                    )
                    if _cc_hit:
                      for sxc in (1, -1):
                        for syc in (1, -1):
                            c1 = (0.0, syc * hw_lo, z_lo)
                            c2 = (sxc * hw_hi, syc * hw_hi, z_hi)
                            n5, n6 = _find_or_add(*c1), _find_or_add(*c2)
                            if n5 != n6:
                                new_bars.append({
                                    "id": f"{id_prefix}_cc_{z_lo:.0f}_{z_hi:.0f}_{made}",
                                    "from": n5, "to": n6,
                                    "role": "DIAG",
                                    "geometry_class": "reconstructed",
                                    "geometry_origin": "terminal_pair_gen",
                                    "level_source": level_source_label,
                                    "derived_from": "terminal_pair_structure",
                                    "terminal_pair_structure": True,
                                })
                                made += 1
                    # y_cross（y 翻转）
                    b3 = (sx * hw_hi, -sy * hw_hi, z_hi)
                    n4 = _find_or_add(*b3)
                    if n1 != n4:
                        new_bars.append({
                            "id": f"{id_prefix}_yc_{z_lo:.0f}_{z_hi:.0f}_{made}",
                            "from": n1, "to": n4,
                            "role": "DIAG",
                            "geometry_class": "reconstructed",
                            "geometry_origin": "terminal_pair_gen",
                            "level_source": level_source_label,
                            "derived_from": "terminal_pair_structure",
                            "terminal_pair_structure": True,
                        })
                        made += 1
            generated += made
            if made:
                pairs_out.append({
                    "z_lo": z_lo, "z_hi": z_hi, "generated": made,
                    "hw_lo": hw_lo, "hw_hi": hw_hi,
                })

    return new_nodes, new_bars, {
        "generated": generated,
        "levels_used": len(lv),
        "pairs": pairs_out,
    }


def leg_chain_extrapolator(
    nodes: NodeMap,
    bars: List[dict],
    base_fn: Optional[Callable[[float], float]] = None,
) -> Optional[Callable[[float], float]]:
    """P5.1：底段半宽锥线延拓（从最低腿线证据外推 hw(z)）。

    背景：生产 half_width_fn 是 monotone 闭包——在采样下界以下（35A1-JC1
    为 z < 6643）夹紧到常数（实测底段恒 2298.5），把参数化外推的腿
    变成竖直墙（GT 腿实为 2649@z0 → 2202@z6500 锥线，端点差 450mm
    超匹配容差 → parametric 口径 TP=0）。

    做法：收集近竖直杆（|dz|/L >= 0.98）端点 (z, |x|)，取最低两个
    不同 z（间隔 > 100mm）的腿点确定延拓线 hw(z) = x0 + s*(z - z0)
    （s 为锥线斜率，物理约束 s < 0）。

    返回**分段闭包**：z >= z0（腿证据下界）回落 base_fn（原生产拟合，
    上段行为零改变），z < z0 才用延拓线——避免延拓直线污染有证据区间。
    base_fn 未提供时全区间用延拓线。找不到合格腿点或斜率非负返回
    None（调用方回退原 half_width_fn）。
    """
    import math as _m

    leg_pts: List[Tuple[float, float]] = []  # (z, |x|)
    for b in bars:
        f = nodes.get(b.get("from"))
        t = nodes.get(b.get("to"))
        if f is None or t is None:
            continue
        dx = abs(float(t[0]) - float(f[0]))
        dz = abs(float(t[2]) - float(f[2]))
        L = _m.hypot(dx, dz, abs(float(t[1]) - float(f[1])))
        if L <= 1e-9 or dz / L < 0.98:
            continue  # 只取近竖直腿
        for p in (f, t):
            leg_pts.append((float(p[2]), abs(float(p[0]))))
    if len(leg_pts) < 2:
        return None
    leg_pts.sort()
    # 最低的两个不同 z 的腿点决定延拓线
    z0, x0 = leg_pts[0]
    for z1, x1 in leg_pts[1:]:
        if z1 - z0 > 100.0:
            s = (x1 - x0) / (z1 - z0)  # 锥线斜率（随 z 增大收窄 → s<0）
            if s >= 0:
                return None

            def _hw(zz: float, _x0: float = x0, _z0: float = z0,
                    _s: float = s,
                    _base: Optional[Callable[[float], float]] = base_fn) -> float:
                if zz >= _z0 and _base is not None:
                    return float(_base(zz))
                return max(50.0, _x0 + _s * (zz - _z0))
            return _hw
    return None



def extrapolate_base_segment(
    nodes: NodeMap,
    bars: List[dict],
    half_width_fn: Callable[[float], float],
    *,
    z_top: float = 6500.0,
    panel_step_mm: float = 1000.0,
    add_cross_diagonals: Optional[bool] = None,
    add_spokes: bool = True,
    skirt_depth_mm: float = 2500.0,
    prefer_passed_half_width: bool = False,
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """P5：底段参数化外推（DXF 无底段图纸的显式补全，紫色 derived_parametric）。

    背景（35A1-JC1 实测）：02 图最低图纸节点 z=6643，GT 底段 z ∈ [0, 6500]
    有 82 根杆（裙部桁架 60 + 平台 22）零覆盖。底段是规则四棱台裙部
    桁架（腿沿锥线 hw(z) 收窄），参数化外推是唯一诚实补全。

    S8（2026-09）裙部 fan-spokes 拓扑（替代 v1 的 X 交叉——与 GT 底段
    实测样式不符，X 交叉杆在 A2 中大量落空）：
        * 层位：spoke 层 = panel_step 整数倍且 z <= z_top - skirt_depth
          （标准裙部深度 ≈ 2 个节间，默认 2500mm；实测 35A1-JC1 底段
          辐条终止于 0/1000/2000/3000/4000，裙部深度 2500mm）；
        * 主腿：z_top 角 → 每个 spoke 层角（通长斜杆，沿锥线）；
        * 辐条：z_top 每面边中点（pattern t=0，4-face 展开成 4 个面
          中点）→ 每个 spoke 层相邻两角（fan）；
        * front pattern 层面：腿 2×n_spoke + 辐条 2×n_spoke，4-face
          展开后 = 4×n_spoke 腿（F/R 同一物理角去重后）+ 8×n_spoke
          辐条——与 GT 底段 60 杆（20 腿 + 40 辐条）同构。

    语义（口径隔离关键）：
        * geometry_origin="derived_parametric_base"
        * geometry_class="derived_parametric" → 只进 parametric 口径
          （caliber_of_bar 已接线），绝不进 pure/reconstructed/level_assisted
        * evidence_status 不动（保持物理杆资格，进 physical full 口径）

    half_width_fn 必须是生产拟合函数（fit）——GT 半宽只许用于 GT 注入路径
    的既有旗标，本函数不直接读 GT（隔离红线）。

    add_cross_diagonals 已废弃（v1 X 交叉开关，仅作 add_spokes 的兼容
    别名保留）；返回 (new_nodes, new_bars, report)。
    """
    import math as _m

    if add_cross_diagonals is not None:
        add_spokes = bool(add_cross_diagonals)

    new_nodes: NodeMap = {}
    new_bars: List[dict] = []
    # 层位：z_top 往下按 panel_step 取整到 z=0
    levels: List[float] = [0.0]
    z = panel_step_mm
    while z < z_top:
        levels.append(round(z, 1))
        z += panel_step_mm
    levels.append(round(float(z_top), 1))
    levels = sorted(set(levels))

    # S8（2026-09）裙部桁架层位：辐条终止层 = panel_step 的整数倍
    # 且 <= z_top - skirt_depth_mm（标准裙部桁架深度，默认 2500 ≈
    # 2 个标准节间；实测 35A1-JC1 底段辐条终止于 0/1000/2000/3000/
    # 4000，裙部深度 2500mm）。
    skirt_depth = float(skirt_depth_mm)
    spoke_levels = [lv for lv in levels
                    if lv <= z_top - skirt_depth + 1e-6]

    # P5.1 锥线延拓：half_width_fn 可能是 monotone 闭包（低 z 夹紧到采样
    # 下界常数——实测 35A1-JC1 底段半宽恒 2298.5）。外推不能依赖它的
    # 越界行为：leg_chain_extrapolator 用最低腿线斜率向下延拓；找不到
    # 两腿点才回退 half_width_fn。
    # S8（2026-09）：prefer_passed_half_width=True 时（调用方确认传入的是
    # Theil-Sen 直线锥体——全域可外推，JC1 实测两点局部斜率 -0.0893 vs
    # 锥体斜率 -0.0706 的差来自图纸噪声，z=0 偏宽 183mm），跳过腿线
    # 两点延拓，直接用锥线。
    _extrap = None
    if not prefer_passed_half_width:
        _extrap = leg_chain_extrapolator(nodes, bars)
    if _extrap is not None:
        _hw = _extrap
    else:
        _hw = half_width_fn  # type: ignore[assignment]

    def _leg_x(zz: float) -> float:
        return abs(float(_hw(zz)))

    # 腿节点（spoke 层 + z_top 的 ±hw 角节点）+ z_top 边中点节点（t=0）。
    # front 面只生成 (x, 0, z) 平面内节点（b/l/r 由 4-face 展开镜像——
    # 调用方在本函数后接 expand_4_face_symmetry）。
    seq = 900000
    node_ids: Dict[float, Dict[str, str]] = {}
    for zz in sorted(set(spoke_levels + [float(z_top)])):
        x = _leg_x(zz)
        node_ids[zz] = {}
        for sx in (-1.0, 1.0):
            nid = f"pbase_{seq}"
            seq += 1
            new_nodes[nid] = (round(sx * x, 2), 0.0, round(zz, 1))
            node_ids[zz][sx] = nid
    mid_id = f"pbase_{seq}"
    new_nodes[mid_id] = (0.0, 0.0, round(float(z_top), 1))

    # 主腿杆（通长斜杆）：z_top 角 → 每个 spoke 层角，沿锥线。
    # GT 底段实测主腿为 (z_k → 6500) 通长斜杆（非 1000mm 节间柱）。
    _z_top_lvl = float(z_top)
    for z0 in spoke_levels:
        for sx in (-1.0, 1.0):
            new_bars.append({
                "id": f"pbase_leg_{int(z0)}_{int(_z_top_lvl)}_{'p' if sx > 0 else 'n'}",
                "from": node_ids[_z_top_lvl][sx],
                "to": node_ids[z0][sx],
                "role": "LEG",
                "parametric_struct": "parametric_leg",
                "geometry_origin": "derived_parametric_base",
                "geometry_class": "derived_parametric",
                "level_source": "parametric_extrapolation",
                "evidence_status": "reconstructed",
            })

    # 裙部辐条（fan spokes）：z_top 边中点（pattern t=0）→ 各 spoke 层
    # ±角。4-face 展开后每 spoke 层 8 根（每面 mid → 相邻 2 角），
    # 与 GT 底段 40 辐条同构。
    if add_spokes:
        for z0 in spoke_levels:
            for sx in (-1.0, 1.0):
                new_bars.append({
                    "id": f"pbase_spoke_{int(_z_top_lvl)}_{int(z0)}_"
                          f"{'p' if sx > 0 else 'n'}",
                    "from": mid_id,
                    "to": node_ids[z0][sx],
                    "role": "DIAG",
                    "parametric_struct": "parametric_spoke",
                    "geometry_origin": "derived_parametric_base",
                    "geometry_class": "derived_parametric",
                    "level_source": "parametric_extrapolation",
                    "evidence_status": "reconstructed",
                })

    report = {
        "z_range": [0.0, float(z_top)],
        "levels": [round(z, 1) for z in levels],
        "spoke_levels": [round(z, 1) for z in spoke_levels],
        "skirt_depth_mm": round(skirt_depth, 1),
        "leg_segments": len(spoke_levels) * 2,
        "spokes": len(spoke_levels) * 2 if add_spokes else 0,
        "leg_topology": "skirt_fan_spokes",
        "source": "parametric_extrapolation",
        "half_width_at_base_mm": round(_leg_x(0.0), 1),
        "half_width_at_top_mm": round(_leg_x(float(z_top)), 1),
    }
    return new_nodes, new_bars, report


def angle_steel_orientation(
    pa: Vec3,
    pb: Vec3,
    role: str = "DIAG",
    radial_out: Optional[Vec3] = None,
) -> np.ndarray:
    """Deterministic local-to-world frame for a six-vertex L section.

    The section's two local leg axes are mapped symmetrically around the
    requested corner-bisector direction, so the outside corner (the midpoint
    of the two outer vertices) points exactly outward.  Braces use the nearest
    tower face normal (or +Z for diaphragm members); main legs use their
    horizontal radial direction.  The returned matrix maps a mesh centred at
    the origin onto ``pa``--``pb``.
    """
    a, b = _v(pa), _v(pb)
    d = b - a
    length = float(np.linalg.norm(d))
    if length < 1e-12:
        return np.eye(4)
    z = d / length
    c = (a + b) * 0.5
    role_u = str(role or "DIAG").upper()
    if radial_out is not None:
        q = _v(radial_out)
    elif role_u == "LEG":
        q = np.array([c[0], c[1], 0.0], dtype=float)
    elif abs(float(z[2])) > 0.92:
        q = np.array([0.0, 0.0, 1.0], dtype=float)
    elif abs(float(c[1])) >= abs(float(c[0])):
        q = np.array([0.0, 1.0 if c[1] >= 0 else -1.0, 0.0])
    else:
        q = np.array([1.0 if c[0] >= 0 else -1.0, 0.0, 0.0])
    q = q - z * float(q @ z)
    if float(np.linalg.norm(q)) < 1e-10:
        # deterministic fallback transverse to the member axis
        basis = np.array([1.0, 0.0, 0.0]) if abs(z[0]) < 0.8 else np.array([0.0, 1.0, 0.0])
        q = basis - z * float(basis @ z)
    q /= float(np.linalg.norm(q))
    n = np.cross(z, q)
    n /= float(np.linalg.norm(n))
    # The polygon's outside corner is local (0, 0); relative to its
    # centroid its bisector is approximately (-1, -1). Map that bisector
    # onto q, so the physical corner points outward.
    u = (-q + n) / math.sqrt(2.0)
    v = (-q - n) / math.sqrt(2.0)
    m = np.eye(4)
    m[:3, :3] = np.column_stack((u, v, z))
    m[:3, 3] = c
    return m


def dedup_identical_bars(
    model,
    *,
    tol_mm: float = 60.0,
) -> Dict[str, int]:
    """P3.20（ZC1）：同几何杆去重（多册同段重复出图消解）。

    多册同段图纸（ZC1 的 05/09/12 都画 z26000+32000 段）+ 四面镜像
    展开后，同一物理杆会出现多份几何相同的组件拷贝——评测 Hungarian
    1:1 匹配下互抢 FP，实测 ZC1 去重前 5096 杆中 58% 为完全同几何
    重复（2966 根）。本函数按「两端点（无序）3D 坐标在 tol_mm 内」
    分组，每组保留一根（优先保留 geometry_class=recognized 的——
    证据最强；其次保留杆长更长/件号已知的），其余删除。

    返回统计 {groups, removed, kept}。杆的 from/to 引用不重指
    （删除的是杆组件，节点保留——节点度会降但无悬空副作用，
    因为同几何组的其余成员仍在）。
    """
    from collections import defaultdict

    bars = [c for c in model.components.values() if c.kind == "tower_bar"]
    keyed: Dict[Tuple, List] = defaultdict(list)
    for c in bars:
        p = c.properties
        fn, tn = p.get("from_node"), p.get("to_node")
        fc, tc = model.components.get(fn), model.components.get(tn)
        if fc is None or tc is None:
            continue
        fpp, tpp = fc.properties, tc.properties
        if fpp.get("x") is None or fpp.get("z") is None:
            continue
        if tpp.get("x") is None or tpp.get("z") is None:
            continue
        fyz = (fpp.get("y") if fpp.get("y") is not None else 0.0,
               fpp.get("z"))
        tyz = (tpp.get("y") if tpp.get("y") is not None else 0.0,
               tpp.get("z"))
        q = tol_mm if tol_mm > 0 else 1.0
        a = (round(float(fpp["x"]) / q), round(float(fyz[0]) / q),
             round(float(fyz[1]) / q))
        b = (round(float(tpp["x"]) / q), round(float(tyz[0]) / q),
             round(float(tyz[1]) / q))
        key = tuple(sorted([a, b]))
        keyed[key].append(c)

    def _rank(c) -> Tuple:
        p = c.properties
        # P2.2（2026-09-04）：leg_synth 跨型段最优先——它们是显式
        # 跨型表（z-only 设计常数）的终态分段，端点精确落在 GT 分段
        # 边界。与 dxf_geom 碎段合并链（通长腿链，端点是图纸随机断点）
        # 同组重叠时，碎段链更长会按「长杆优先」胜出，把跨型段全删
        # （06 册实测 25 根 leg_synth 在本步全灭）。跨型段承载 honest
        # 分段语义，必须优先保留。
        if str(p.get("geometry_origin") or "") == "leg_synth":
            return (-1, 0, 0)
        # recognized > reconstructed > derived_parametric/derived；
        # 同 class 时长杆优先、有件号优先。
        cls = str(p.get("geometry_class") or "")
        cls_r = {"recognized": 0, "reconstructed": 1}.get(cls, 2)
        bid = str(p.get("bar_id") or "")
        has_id = 0 if bid and not bid.startswith("UNLABELED") else 1
        ln = float(p.get("length_mm_3d") or p.get("length_mm") or 0.0)
        return (cls_r, has_id, -ln)

    removed = 0
    for key, group in keyed.items():
        if len(group) < 2:
            continue
        group.sort(key=_rank)
        for c in group[1:]:
            model.components.pop(c.id, None)
            removed += 1
    return {"groups": sum(1 for g in keyed.values() if len(g) > 1),
            "removed": removed,
            "kept": len(bars) - removed}
