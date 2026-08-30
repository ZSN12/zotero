"""tower_geometry 四个几何模块的单元测试。

覆盖：
    M1 snap_diagonals_to_legs  斜材轴线延伸 + 主腿求交吸附
    M2 orient_angle_normal     L 角钢截面法向定向
    M3 expand_to_4_face_truss  四面空间桁架闭合对称
    M4 classify_members + stitch_collinear_segments  语义分类 + 分段缝合
"""

from __future__ import annotations

import math
import unittest
from pathlib import Path

import numpy as np

from traceability.solve import tower_geometry as g


class SnapDiagonalsToLegsTest(unittest.TestCase):
    """M1：斜材端点延伸吸附到主腿工作中心线。"""

    def test_diag_snaps_to_vertical_leg(self):
        nodes = {
            "L1": (100.0, 0.0, 0.0), "L2": (100.0, 0.0, 100.0),
            "D1": (0.0, 0.0, 50.0), "D2": (95.0, 0.0, 60.0),
        }
        bars = [
            {"id": "leg", "from": "L1", "to": "L2"},
            {"id": "diag", "from": "D1", "to": "D2"},
        ]
        nn, nb = g.snap_diagonals_to_legs(nodes, bars, leg_ids=["leg"], snap_tol=800.0)
        diag = next(b for b in nb if b["id"] == "diag")
        # 吸附后的端点应落在主腿直线 x=100 上
        to = nn[diag["to"]]
        self.assertAlmostEqual(to[0], 100.0, places=2)
        # 主腿不被改动
        self.assertEqual(nn["L1"], (100.0, 0.0, 0.0))

    def test_far_diag_not_snapped(self):
        # 斜材离腿超过 snap_tol，不应吸附
        nodes = {
            "L1": (100.0, 0.0, 0.0), "L2": (100.0, 0.0, 100.0),
            "D1": (0.0, 5000.0, 50.0), "D2": (95.0, 5000.0, 60.0),
        }
        bars = [
            {"id": "leg", "from": "L1", "to": "L2"},
            {"id": "diag", "from": "D1", "to": "D2"},
        ]
        nn, nb = g.snap_diagonals_to_legs(nodes, bars, leg_ids=["leg"], snap_tol=800.0)
        diag = next(b for b in nb if b["id"] == "diag")
        self.assertIn(diag["to"], ("D2",))
        self.assertEqual(nn["D2"], (95.0, 5000.0, 60.0))


class OrientAngleNormalTest(unittest.TestCase):
    """M2：L 角钢截面法向定向（角顶朝外，正交基）。"""

    def test_leg_corner_points_outward(self):
        x, y, z = g.angle_normal_basis((0.0, 0.0, 1.0), (100.0, 100.0, 50.0), role="LEG")
        # 角顶 (x) 应指向径向 (1,1,0)/√2
        outward = np.array([1.0, 1.0, 0.0]) / math.sqrt(2.0)
        self.assertGreater(float(x @ outward), 0.99)
        # 正交且单位
        self.assertAlmostEqual(float(x @ y), 0.0, places=8)
        self.assertAlmostEqual(float(np.linalg.norm(x)), 1.0, places=8)
        self.assertAlmostEqual(float(np.linalg.norm(z)), 1.0, places=8)

    def test_diag_normal_perpendicular_to_axis(self):
        d = (1.0, 0.0, 1.0)
        x, y, z = g.angle_normal_basis(d, (500.0, 0.0, 500.0), role="DIAG")
        # 截面 x/y 都垂直于杆轴向
        axis = np.array(d) / np.linalg.norm(d)
        self.assertAlmostEqual(float(x @ axis), 0.0, places=8)
        self.assertAlmostEqual(float(y @ axis), 0.0, places=8)

    def test_align_matrix_has_unit_basis(self):
        m = g._align_matrix((1.0, 2.0, 3.0), (10.0, 20.0, 30.0), role="DIAG")
        for i in range(3):
            col = m[:3, i]
            self.assertAlmostEqual(float(np.linalg.norm(col)), 1.0, places=6)

    def test_align_matrix_maps_bar_axis_to_direction(self):
        # 根因 A 回归：trimesh 按列当基向量，R 的列必须是局部 X/Y/Z 的世界像。
        # 局部 Z（杆轴）经 R 变换后必须等于 normalize(direction)。
        d = np.array([3.0, 0.0, 4.0])
        center = (50.0, 0.0, 100.0)
        m = g._align_matrix(tuple(d), center, role="DIAG")
        axis_local = np.array([0.0, 0.0, 1.0])
        axis_world = m[:3, :3] @ axis_local  # 列基向量右乘
        self.assertTrue(np.allclose(axis_world, d / np.linalg.norm(d), atol=1e-6))

    def test_align_matrix_bar_ends_meet_nodes(self):
        # 根因 A + 网格原点：杆 mesh 局部 Z ∈ [-L/2,+L/2]，平移对准中点，
        # 两端必须落在 from/to 节点（偏差 < 1mm）。
        from traceability.solve.tower_solver import _angle_steel_mesh

        pa = np.array([1200.0, -900.0, 5000.0])
        pb = np.array([2400.0, 300.0, 13000.0])
        d = pb - pa
        L = float(np.linalg.norm(d))
        mid = (pa + pb) / 2.0
        mesh = _angle_steel_mesh("L90X6", L)
        # 网格沿局部 Z 居中
        self.assertAlmostEqual(float(mesh.bounds[0][2]), -L / 2.0, places=3)
        self.assertAlmostEqual(float(mesh.bounds[1][2]), L / 2.0, places=3)
        m = g._align_matrix(tuple(d), tuple(mid), role="DIAG")
        mesh.apply_transform(m)
        axis_world = m[:3, :3] @ np.array([0.0, 0.0, 1.0])
        e0 = mid - (L / 2.0) * axis_world
        e1 = mid + (L / 2.0) * axis_world
        self.assertLess(float(np.linalg.norm(e0 - pa)), 1.0)
        self.assertLess(float(np.linalg.norm(e1 - pb)), 1.0)

    def test_gt_bar_ends_meet_nodes_sample(self):
        # 用 GT 抽 20 根：导出实体后杆端与节点偏差 < 1mm（验收标准）。
        import json
        from traceability.solve.tower_solver import _angle_steel_mesh

        gt = json.loads(
            (Path(__file__).resolve().parent.parent / "examples/gt/35A1-JC1_ground_truth.json")
            .read_text(encoding="utf-8")
        )
        nodes = gt["nodes"]
        worst = 0.0
        for bar in gt["bars"][:20]:
            f = nodes.get(bar["from"])
            t = nodes.get(bar["to"])
            if f is None or t is None:
                continue
            pa = np.array(f)
            pb = np.array(t)
            d = pb - pa
            L = float(np.linalg.norm(d))
            if L < 1e-6:
                continue
            mid = (pa + pb) / 2.0
            mesh = _angle_steel_mesh(bar.get("section"), L)
            m = g._align_matrix(tuple(d), tuple(mid), role="DIAG")
            mesh.apply_transform(m)
            axis = m[:3, :3] @ np.array([0.0, 0.0, 1.0])
            e0 = mid - (L / 2.0) * axis
            e1 = mid + (L / 2.0) * axis
            worst = max(worst, float(np.linalg.norm(e0 - pa)), float(np.linalg.norm(e1 - pb)))
        self.assertLess(worst, 1.0)


class ExpandTo4FaceTest(unittest.TestCase):
    """M3：单立面 -> 四面空间桁架闭合对称。"""

    def test_four_faces(self):
        nodes = {"A": (100.0, 0.0, 0.0), "B": (50.0, 0.0, 50.0)}
        bars = [{"id": "m", "from": "A", "to": "B"}]
        nn, nb = g.expand_to_4_face_truss(nodes, bars)
        self.assertEqual(len(nn), 8)   # 2 节点 × 4 面
        self.assertEqual(len(nb), 4)   # 1 杆 × 4 面
        # 第 0 面保留原坐标
        self.assertEqual(nn["A"], (100.0, 0.0, 0.0))
        # 第 1 面 = 绕 Z 转 90°
        a1 = np.array(nn["A_r1"])
        self.assertAlmostEqual(float(a1[0]), 0.0, places=6)
        self.assertAlmostEqual(float(a1[1]), 100.0, places=6)
        # 第 2 面 = 180°，第 3 面 = 270°
        self.assertAlmostEqual(nn["A_r2"][0], -100.0, places=6)
        self.assertAlmostEqual(nn["A_r3"][1], -100.0, places=6)

    def test_expansion_preserves_bar_topology(self):
        nodes = {"A": (100.0, 0.0, 0.0), "B": (50.0, 0.0, 50.0)}
        bars = [{"id": "m", "from": "A", "to": "B"}]
        nn, nb = g.expand_to_4_face_truss(nodes, bars)
        ids = {b["id"] for b in nb}
        self.assertEqual(ids, {"m", "m_r1", "m_r2", "m_r3"})


class ClassifyAndStitchTest(unittest.TestCase):
    """M4：语义分类 + 分段缝合。"""

    def _sample(self):
        nodes = {
            "L1": (-2000.0, -2000.0, 0.0), "L2": (-1500.0, -1500.0, 3000.0),
            "H1": (0.0, -2000.0, 50.0), "H2": (0.0, 2000.0, 50.0),
            "D1": (0.0, 0.0, 0.0), "D2": (1000.0, 1000.0, 1500.0),
            "C1": (3000.0, 0.0, 50.0), "C2": (6000.0, 0.0, 50.0),
        }
        bars = [
            {"id": "leg", "from": "L1", "to": "L2"},
            {"id": "horiz", "from": "H1", "to": "H2"},
            {"id": "diag", "from": "D1", "to": "D2"},
            {"id": "cross", "from": "C1", "to": "C2"},
        ]
        return nodes, bars

    def test_classify_roles(self):
        nodes, bars = self._sample()
        roles = g.classify_members(nodes, bars)
        self.assertEqual(roles["leg"], "LEG")
        self.assertEqual(roles["horiz"], "HORIZ")
        self.assertEqual(roles["diag"], "DIAG")
        self.assertEqual(roles["cross"], "CROSS")

    def test_stitch_collinear(self):
        # 三段共线、首尾相接 -> 缝合成一根
        nodes = {
            "N1": (0.0, 0.0, 0.0), "N2": (100.0, 0.0, 0.0),
            "N3": (200.0, 0.0, 0.0), "N4": (300.0, 0.0, 0.0),
        }
        bars = [
            {"id": "s1", "from": "N1", "to": "N2"},
            {"id": "s2", "from": "N2", "to": "N3"},
            {"id": "s3", "from": "N3", "to": "N4"},
        ]
        nn, nb = g.stitch_collinear_segments(nodes, bars)
        self.assertEqual(len(nb), 1)
        merged = nb[0]
        # 两端覆盖 0..300
        ends = sorted([nn[merged["from"]][0], nn[merged["to"]][0]])
        self.assertAlmostEqual(ends[0], 0.0, places=3)
        self.assertAlmostEqual(ends[1], 300.0, places=3)

    def test_stitch_skips_angle(self):
        # 直角两段不缝合
        nodes = {
            "N1": (0.0, 0.0, 0.0), "N2": (100.0, 0.0, 0.0),
            "N3": (100.0, 100.0, 0.0),
        }
        bars = [
            {"id": "s1", "from": "N1", "to": "N2"},
            {"id": "s2", "from": "N2", "to": "N3"},
        ]
        nn, nb = g.stitch_collinear_segments(nodes, bars)
        self.assertEqual(len(nb), 2)


if __name__ == "__main__":
    unittest.main()


class FitTowerHalfWidthTest(unittest.TestCase):
    """阶段3.2：生产路径从立面主腿拟合 half_width(z)，不用 GT、不用 abs(t)。"""

    def _tapered_face(self):
        # 四棱台立面：底半宽 2000，顶半宽 1000，4 段主腿
        nodes = {}
        bars = []
        zs = [0, 1000, 2000, 3000, 4000]
        half = [2000, 1750, 1500, 1250, 1000]
        for i, (z, hw) in enumerate(zip(zs, half)):
            nodes[f"L{i}"] = (hw, 0.0, z)
            nodes[f"R{i}"] = (-hw, 0.0, z)
        # 主腿：左腿 + 右腿（近竖直）
        for i in range(len(zs) - 1):
            bars.append({"id": f"legL{i}", "from": f"L{i}", "to": f"L{i+1}"})
            bars.append({"id": f"legR{i}", "from": f"R{i}", "to": f"R{i+1}"})
        # 内部斜材（|x| < 半宽，不应影响拟合）
        nodes["D0"] = (0.0, 0.0, 500)
        nodes["D1"] = (500.0, 0.0, 1500)
        bars.append({"id": "diag0", "from": "D0", "to": "D1"})
        return nodes, bars

    def test_fit_half_width_monotonic_taper(self):
        nodes, bars = self._tapered_face()
        fn = g.fit_tower_half_width_from_face(nodes, bars)
        self.assertIsNotNone(fn, "应从主腿拟合出半宽函数")
        # 底宽 2000，顶宽 1000，随 z 单调递减
        self.assertAlmostEqual(fn(0.0), 2000.0, delta=80.0)
        self.assertAlmostEqual(fn(2000.0), 1500.0, delta=80.0)
        self.assertAlmostEqual(fn(4000.0), 1000.0, delta=80.0)
        # 越界夹紧
        self.assertAlmostEqual(fn(-500.0), fn(0.0), places=3)
        self.assertAlmostEqual(fn(5000.0), fn(4000.0), places=3)

    def test_fit_returns_none_when_no_legs(self):
        nodes = {"A": (100.0, 0.0, 0.0), "B": (0.0, 0.0, 100.0)}
        bars = [{"id": "d", "from": "A", "to": "B"}]  # 斜杆，非主腿
        self.assertIsNone(g.fit_tower_half_width_from_face(nodes, bars))


class FaceDepthNotAbsTTest(unittest.TestCase):
    """阶段3.1：塔身深度 = half_width(z)，不是节点自身 abs(t)。"""

    def test_internal_node_keeps_face_coordinate(self):
        # 内部腹杆节点 t=300，塔身半宽 1000：深度应为 1000（半宽），
        # 面内坐标 t 保留 300，而不是把 t 压成 1000 或用 abs(300)=300 当深度。
        nodes = {
            "legL": (1000.0, 0.0, 0.0),
            "legR": (-1000.0, 0.0, 0.0),
            "inner": (300.0, 0.0, 500.0),
        }
        bars = [
            {"id": "leg", "from": "legL", "to": "legR"},
            {"id": "diag", "from": "legL", "to": "inner"},
        ]
        # 用拟合的半宽函数（半宽恒 1000）
        def hw(z):
            return 1000.0
        nn, nb = g.expand_4_face_symmetry(nodes, bars, half_width_fn=hw)
        # inner 节点 front 面应在 y=+1000，x 保留 300
        front_inner = None
        for nid, pos in nn.items():
            if abs(pos[0] - 300.0) < 1e-6 and pos[1] > 0:
                front_inner = pos
        self.assertIsNotNone(front_inner, "内部节点 front 面应保留 x=300")
        self.assertAlmostEqual(front_inner[1], 1000.0, places=3,
                               msg="塔身深度应为 half_width(500)=1000，不是 abs(t)=300")


if __name__ == "__main__":
    unittest.main()
