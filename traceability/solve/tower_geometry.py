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
from typing import Callable, Dict, List, Optional, Sequence, Tuple

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
            for end in ("from", "to"):
                p = new_nodes.get(bar[end])
                if p is None:
                    continue
                best = None
                for bj, other in enumerate(new_bars):
                    if bi == bj:
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
            n_center = add_node(fm1["_C"])
            for suffix in common:
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
            n_center = add_node(fm2["_C"])
            for suffix in common:
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
            # 内十字贯通 (2 杆)
            (in_0, in_3), (in_1, in_2),
        ]

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
        z_mid = _bar_z_mid(nodes, b)
        if z_mid is None:
            kept.append(b)
            continue
        max_r = _bar_max_radial(nodes, b)
        hw = float(half_width_fn(z_mid)) if half_width_fn is not None else 0.0
        arm_hw = float(crossarm_half_width_fn(z_mid)) if crossarm_half_width_fn else 0.0

        reason: Optional[str] = None
        if crossarm_half_width_fn is not None:
            if arm_hw <= 0.0:
                reason = "no_crossarm_layer_at_z"
        elif z_mid < float(crossarm_zone_z_min_mm):
            reason = "below_crossarm_zone"

        if reason is None and hw > 0 and max_r < hw * float(crossarm_radial_ratio):
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
        nb = dict(src)
        nb.update({
            "id": nid,
            "from": ns,
            "to": ne,
            "role": src.get("role"),
            "geometry_class": inherit_cls or src.get("geometry_class"),
            "geometry_origin": "collinear_stitch",
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
    # 两段式变坡塔：上段整段被当离群剔除（覆盖 ~57%<75%）→ 拒绝拟合回退
    # monotone；JC1 真实输入：横担箱只占 ~12% → 覆盖 ~88% 通过。
    z_span_in = max(z_pts) - min(z_pts)
    z_span_fit = (max(zs) - min(zs)) if len(zs) >= 2 else 0.0
    if z_span_in > 0 and z_span_fit / z_span_in < min_z_coverage:
        if debug:
            print(f"[taper] 回退：z 覆盖率 {z_span_fit/z_span_in:.1%} < "
                  f"{min_z_coverage:.0%}（剔除段过大，疑似变坡/两段式塔身）")
        return None

    resid = [abs(h - (b0 + k * z)) for z, h in zip(zs, hs)]
    inliers = sum(1 for r in resid if r <= inlier_tol_mm)
    ratio = inliers / len(resid) if resid else 0.0
    if ratio < min_inlier_ratio:
        if debug:
            print(f"[taper] 回退：内点比例 {ratio:.1%} < {min_inlier_ratio:.0%}"
                  f"（残差 p90={sorted(resid)[int(len(resid)*0.9)]:.0f}mm "
                  f"max={max(resid):.0f}mm）疑似变坡")
        return None

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
                       "n_wide_nodes": len(grp)})

    def crossarm_half_width(z: float) -> float:
        for lyr in layers:
            if lyr["z_lo"] <= z <= lyr["z_hi"]:
                return float(lyr["arm_mm"])
        return 0.0

    return crossarm_half_width, {
        "layers": layers,
        "n_wide_nodes": len(wide),
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
        fitted_taper = _fit_taper_profile(
            z_pts, hw_pts, inlier_tol_mm=taper_max_residual_mm)
        if fitted_taper is not None:
            return fitted_taper
        # 内点比例不足（疑似变坡）/ 拟合失败 → 落到 monotone 旧路径（下方继续）

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
    dedup_bars = 0
    if dedup_collinear:
        seen: set = set()
        deduped: List[dict] = []
        for b in new_bars:
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

    for i in range(len(lv) - 1):
        z_lo, z_hi = lv[i], lv[i + 1]
        gap = z_hi - z_lo
        if gap < min_level_gap_mm or gap > max_level_gap_mm:
            continue
        if crossarm_z_max is not None and z_hi >= crossarm_z_max:
            continue  # 塔身区限定（横担区斜线语义不同）
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
                "id": f"panel_cross_{i}_{made}",
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
    add_cross_diagonals: bool = True,
) -> Tuple[NodeMap, List[dict], Dict[str, Any]]:
    """P5：底段参数化外推（DXF 无底段图纸的显式补全，紫色 derived_parametric）。

    背景（35A1-JC1 实测）：02 图最低图纸节点 z=6643，GT 底段 z ∈ [0, 6500]
    有 120 根杆（主腿 40 + 斜材 80）零覆盖。底段是规则四棱台（腿沿锥线
    hw(z) 收窄），参数化外推是唯一诚实补全：
        * 主腿：沿 ±hw(z) 锥线按 panel_step_mm 节间生成（z=0 塔脚 → z_top）
        * 交叉斜材：段内 X 交叉（对角腿位连接，与 GT 底段样式一致——
          GT 斜材全为跨腿位对角连接）
        * 节点：z=0 处 4 角塔脚节点；每层 ±hw(z) 腿节点

    语义（口径隔离关键）：
        * geometry_origin="derived_parametric_base"
        * geometry_class="derived_parametric" → 只进 parametric 口径
          （caliber_of_bar 已接线），绝不进 pure/reconstructed/level_assisted
        * evidence_status 不动（保持物理杆资格，进 physical full 口径）

    half_width_fn 必须是生产拟合函数（fit）——GT 半宽只许用于 GT 注入路径
    的既有旗标，本函数不直接读 GT（隔离红线）。

    返回 (new_nodes, new_bars, report)。new_bars 只含生成杆（调用方合并）。
    """
    import math as _m

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

    # P5.1 锥线延拓：half_width_fn 可能是 monotone 闭包（低 z 夹紧到采样
    # 下界常数——实测 35A1-JC1 底段半宽恒 2298.5）。外推不能依赖它的
    # 越界行为：leg_chain_extrapolator 用最低腿线斜率向下延拓；找不到
    # 两腿点才回退 half_width_fn。
    _extrap = leg_chain_extrapolator(nodes, bars)
    if _extrap is not None:
        _hw = _extrap
    else:
        _hw = half_width_fn  # type: ignore[assignment]

    def _leg_x(zz: float) -> float:
        return abs(float(_hw(zz)))

    # 腿节点（4 角 × 每层）：front 面只生成 (x, 0, z) 平面内两腿
    # （b/l/r 由 4-face 展开镜像——调用方在本函数后接 expand_4_face_symmetry）
    seq = 900000
    node_ids: Dict[float, Dict[str, str]] = {}
    for zz in levels:
        x = _leg_x(zz)
        node_ids[zz] = {}
        for sx in (-1.0, 1.0):
            nid = f"pbase_{seq}"
            seq += 1
            new_nodes[nid] = (round(sx * x, 2), 0.0, round(zz, 1))
            node_ids[zz][sx] = nid

    # 主腿杆（通长样式，与 GT 底段同构）：每个层起点 z_k 生成
    # (x_k, z_k) → (x_top, z_top) 通长腿杆——GT 底段实测主腿为
    # (z_k → 6500) 通长斜杆（非 1000mm 节间柱），k=0..n-1。
    # 最上层（z_k = z_top 前最后层）与 z_top 的短段也保留。
    _z_top_lvl = levels[-1]
    for k in range(len(levels) - 1):
        z0 = levels[k]
        for sx in (-1.0, 1.0):
            new_bars.append({
                "id": f"pbase_leg_{int(z0)}_{int(_z_top_lvl)}_{'p' if sx > 0 else 'n'}",
                "from": node_ids[z0][sx],
                "to": node_ids[_z_top_lvl][sx],
                "role": "LEG",
                "parametric_struct": "parametric_leg",
                "geometry_origin": "derived_parametric_base",
                "geometry_class": "derived_parametric",
                "level_source": "parametric_extrapolation",
                "evidence_status": "reconstructed",
            })

    # 段内 X 交叉斜材（相邻层对角连接，GT 底段样式）
    if add_cross_diagonals:
        for k in range(len(levels) - 1):
            z0, z1 = levels[k], levels[k + 1]
            new_bars.append({
                "id": f"pbase_x_{int(z0)}_{int(z1)}_pn",
                "from": node_ids[z0][-1.0],
                "to": node_ids[z1][1.0],
                "role": "CROSS",
                "parametric_struct": "parametric_cross",
                "geometry_origin": "derived_parametric_base",
                "geometry_class": "derived_parametric",
                "level_source": "parametric_extrapolation",
                "evidence_status": "reconstructed",
            })
            new_bars.append({
                "id": f"pbase_x_{int(z0)}_{int(z1)}_np",
                "from": node_ids[z0][1.0],
                "to": node_ids[z1][-1.0],
                "role": "CROSS",
                "parametric_struct": "parametric_cross",
                "geometry_origin": "derived_parametric_base",
                "geometry_class": "derived_parametric",
                "level_source": "parametric_extrapolation",
                "evidence_status": "reconstructed",
            })

    report = {
        "z_range": [0.0, float(z_top)],
        "levels": [round(z, 1) for z in levels],
        "leg_segments": len(levels) - 1,
        "cross_diagonals": (len(levels) - 1) * 2 if add_cross_diagonals else 0,
        "leg_topology": "through_to_ztop",
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
