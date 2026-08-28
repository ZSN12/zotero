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
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

Vec3 = Tuple[float, float, float]
NodeMap = Dict[str, Vec3]


# --------------------------------------------------------------------------- #
# 公共几何工具
# --------------------------------------------------------------------------- #

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
                    if len(new_bars) < max_bars:
                        new_bars.append({
                            "id": f"{other['id']}__split{len(new_bars)}",
                            "from": q,
                            "to": old_to,
                            **{k: v for k, v in other.items()
                               if k not in ("id", "from", "to")},
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
    """
    x, y, z = angle_normal_basis(direction, center, role=role)
    m = np.eye(4)
    m[0, :3] = x
    m[1, :3] = y
    m[2, :3] = z
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

    返回 (new_nodes, new_bars)。节点 id 加 `_f{face}` 后缀；重复位置自动共享。
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
    def face_maps(t: float, z: float) -> Dict[str, Vec3]:
        w = abs(t)
        if w < node_tol * 0.5:
            # 中心轴节点：四面共享同一点
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

        # 中心节点（_C）与非中心四面对齐时，生成 4 根杆（中心→每个面）。
        # 非中心与非中心之间按同名字面生成 4 根杆。
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
            # 起点是中心，终点是四面：生成 4 根中心→面杆
            n_center = next(nid for nid, p in new_nodes.items()
                            if float(np.linalg.norm(_v(p) - _v(fm1["_C"]))) <= node_tol)
            for suffix in ("_F", "_B", "_L", "_R"):
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
            # 终点是中心，起点是四面：生成 4 根面→中心杆
            n_center = next(nid for nid, p in new_nodes.items()
                            if float(np.linalg.norm(_v(p) - _v(fm2["_C"]))) <= node_tol)
            for suffix in ("_F", "_B", "_L", "_R"):
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
            # 两端都是非中心：按同名字面生成 4 根
            for suffix in ("_F", "_B", "_L", "_R"):
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
    if weld_corner_legs:
        from collections import defaultdict
        by_z: Dict[float, List[Tuple[str, Vec3]]] = defaultdict(list)
        for nid, p in new_nodes.items():
            by_z[round(float(p[2]), 1)].append((nid, p))

        quadrants = [(1.0, 1.0), (-1.0, 1.0), (-1.0, -1.0), (1.0, -1.0)]
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
            # 每个拐角只保留 1 根通长主腿（从最低标高到最高标高）。
            if len(corner_nodes) >= 2:
                z_a, n_a = corner_nodes[0]
                z_b, n_b = corner_nodes[-1]
                if abs(z_b - z_a) >= 1e-6:
                    key = (min(n_a, n_b), max(n_a, n_b))
                    if key in seen:
                        # 该拐角已存在 face 杆件（主腿已在输入中），
                        # 把它标记为 corner_leg / LEG。
                        existing_id = seen[key]
                        for eb in new_bars:
                            if eb["id"] == existing_id:
                                eb["role"] = "LEG"
                                eb["corner_leg"] = True
                                eb["corner_index"] = ci
                                break
                    else:
                        seen[key] = f"corner_leg_{ci}"
                        new_bars.append({
                            "id": f"corner_leg_{ci:03d}",
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
        new_nodes, new_bars = generate_diaphragms(new_nodes, new_bars, wall=wall)

    return new_nodes, new_bars


def generate_diaphragms(
    nodes: NodeMap,
    bars: List[dict],
    *,
    wall: Optional[float] = None,
    min_z_gap: float = 300.0,
    with_perimeter: bool = True,
) -> Tuple[NodeMap, List[dict]]:
    """在各标高平台处生成水平横隔面（内部横隔材）。

    平台判定：同一 z（min_z_gap 内）存在 4 个塔角节点（±w, ±w）。
    每个平台生成：
        * 4 条水平边杆（相邻角点两两相连，闭合方框）；
        * 2 条交叉水平杆（菱形对角线）。
    返回 (nodes, bars)；节点复用已有塔角节点。
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

    corner_ids_by_z: Dict[float, List[Optional[str]]] = {}
    for bz in sorted(buckets):
        cids: List[Optional[str]] = [None, None, None, None]
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
    for z, cids in sorted(corner_ids_by_z.items()):
        if any(c is None for c in cids):
            continue
        pairs: List[Tuple[int, int]] = []
        if with_perimeter:
            pairs += [(0, 1), (1, 2), (2, 3), (3, 0)]  # 四边闭合
        pairs += [(0, 2), (1, 3)]  # 交叉对角线
        for (ia, ib) in pairs:
            a, b = cids[ia], cids[ib]
            if a is None or b is None or a == b:
                continue
            key = (min(a, b), max(a, b))
            if key in existing_keys:
                continue
            existing_keys.add(key)
            new_bars.append({
                "id": f"diaphragm_{z:07.1f}_{ia}{ib}",
                "from": a,
                "to": b,
                "face": "diaphragm",
                "diaphragm": True,
                "generated_4face": True,
            })
    return nodes, new_bars


def inspect_model_topology(nodes: NodeMap, bars: List[dict]) -> Dict[str, object]:
    """拓扑度数统计（量化验收 1：悬空断裂节点 Degree=1 应为 0）。

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

    return {
        "degree_histogram": {str(k): v for k, v in sorted(hist.items())},
        "dangling_degree1": hist.get(1, 0),
        "max_degree": max(hist) if hist else 0,
        "components": components,
        "total_nodes": len(nodes),
        "total_bars": len(bars),
    }


# --------------------------------------------------------------------------- #
# Module 4  语义分类 + 分段缝合
# --------------------------------------------------------------------------- #

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
    # 立面外缘宽度（用于主腿边缘判定）：节点最大 |x|。
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
        if incl >= leg_min_incl:
            roles[b["id"]] = "LEG"
        elif incl >= corner_leg_min_incl and wall > 0 and edge >= wall * 0.7:
            # 贴近立面两缘的斜柱也是主腿（四棱台塔腿）
            roles[b["id"]] = "LEG"
        elif abs(incl) <= horiz_max_incl:
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
