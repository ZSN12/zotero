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
