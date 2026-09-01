"""阶段 5.4 分册边界腿杆搭桥（bridge_segment_boundary_legs）单元测试。

背景：多段立面图册各画各的段，分册边界（如 07 册 [7000,12000] 与 06 册
[13000,17000] 之间的 [12000,13000]）腿链断裂。GT 实测 96 根杆跨越该边界。
搭桥函数按腿链断口（象限轨迹上相邻端点 z 缺口 + 横向配对）生成搭桥腿杆。
"""

import unittest

from traceability.solve.tower_geometry import bridge_segment_boundary_legs


class TestBridgeSegmentBoundaryLegs(unittest.TestCase):
    def test_basic_bridge(self):
        # 下段腿顶端 z=12006（07 册），上段腿底端 z=13000（06 册），
        # 同一物理腿（右前角），缺口 994mm ∈ (120, 1600] → 应生成 1 根搭桥腿。
        nodes = {
            "n_lo_bot": (2285.0, 2285.0, 7000.0),
            "n_lo_top": (2200.0, 2200.0, 12006.0),
            "n_hi_bot": (2100.0, 2100.0, 13000.0),
            "n_hi_top": (1900.0, 1900.0, 17000.0),
        }
        bars = [
            {"id": "leg_lo", "from": "n_lo_bot", "to": "n_lo_top", "role": "LEG"},
            {"id": "leg_hi", "from": "n_hi_bot", "to": "n_hi_top", "role": "LEG"},
        ]
        nn, nb, rep = bridge_segment_boundary_legs(nodes, bars, boundaries=[13000.0])
        self.assertEqual(rep["bridged"], 1)
        bridges = [b for b in nb if b.get("geometry_origin") == "boundary_leg_bridge"]
        self.assertEqual(len(bridges), 1)
        self.assertEqual(bridges[0]["from"], "n_lo_top")
        self.assertEqual(bridges[0]["to"], "n_hi_bot")
        self.assertEqual(bridges[0]["role"], "LEG")

    def test_bridge_despite_connected_nodes(self):
        # 断口两侧端点各自挂着横杆（degree>=2）——链断口检测与度数无关，
        # 仍应搭桥（06 顶 16645 与 05 底 18010 的真实形态）。
        nodes = {
            "n_lo_bot": (2285.0, 2285.0, 15000.0),
            "n_lo_top": (1700.0, 1700.0, 16645.0),
            "n_lo_beam": (-1700.0, 1700.0, 16645.0),
            "n_hi_bot": (1605.0, 1605.0, 18010.0),
            "n_hi_top": (1400.0, 1400.0, 20000.0),
            "n_hi_beam": (-1605.0, 1605.0, 18010.0),
        }
        bars = [
            {"id": "leg_lo", "from": "n_lo_bot", "to": "n_lo_top", "role": "LEG"},
            {"id": "beam_lo", "from": "n_lo_beam", "to": "n_lo_top", "role": "HORIZ"},
            {"id": "leg_hi", "from": "n_hi_bot", "to": "n_hi_top", "role": "LEG"},
            {"id": "beam_hi", "from": "n_hi_beam", "to": "n_hi_bot", "role": "HORIZ"},
        ]
        nn, nb, rep = bridge_segment_boundary_legs(nodes, bars, boundaries=[18000.0])
        self.assertEqual(rep["bridged"], 1)

    def test_no_bridge_when_gap_too_large(self):
        # 缺口 3000mm > max_gap 1600 → 不搭桥。
        nodes = {
            "n_lo_bot": (2285.0, 2285.0, 7000.0),
            "n_lo_top": (2200.0, 2200.0, 10000.0),
            "n_hi_bot": (2100.0, 2100.0, 13000.0),
            "n_hi_top": (1900.0, 1900.0, 17000.0),
        }
        bars = [
            {"id": "leg_lo", "from": "n_lo_bot", "to": "n_lo_top", "role": "LEG"},
            {"id": "leg_hi", "from": "n_hi_bot", "to": "n_hi_top", "role": "LEG"},
        ]
        nn, nb, rep = bridge_segment_boundary_legs(nodes, bars, boundaries=[13000.0])
        self.assertEqual(rep["bridged"], 0)

    def test_no_bridge_when_gap_too_small(self):
        # 缺口 60mm < min_gap 120（节点对齐容差内的链内端点）→ 不搭桥。
        nodes = {
            "n1": (2285.0, 2285.0, 7000.0),
            "n2": (2280.0, 2280.0, 7500.0),
            "n3": (2275.0, 2275.0, 7560.0),
            "n4": (2270.0, 2270.0, 9000.0),
        }
        bars = [
            {"id": "leg_1", "from": "n1", "to": "n2", "role": "LEG"},
            {"id": "leg_2", "from": "n3", "to": "n4", "role": "LEG"},
        ]
        nn, nb, rep = bridge_segment_boundary_legs(nodes, bars, boundaries=[7500.0])
        self.assertEqual(rep["bridged"], 0)

    def test_no_bridge_when_lateral_mismatch(self):
        # 两侧腿横向错位 > 400mm（不同物理腿/象限）→ 不搭桥。
        nodes = {
            "n_lo_bot": (2285.0, 2285.0, 7000.0),
            "n_lo_top": (2200.0, 2200.0, 12006.0),
            "n_hi_bot": (2100.0, -2100.0, 13000.0),  # 对角腿（不同象限）
            "n_hi_top": (1900.0, -1900.0, 17000.0),
        }
        bars = [
            {"id": "leg_lo", "from": "n_lo_bot", "to": "n_lo_top", "role": "LEG"},
            {"id": "leg_hi", "from": "n_hi_bot", "to": "n_hi_top", "role": "LEG"},
        ]
        nn, nb, rep = bridge_segment_boundary_legs(nodes, bars, boundaries=[13000.0])
        self.assertEqual(rep["bridged"], 0)

    def test_four_legs_bridge(self):
        # 四条腿同时断在边界 → 四根搭桥。
        nodes = {}
        bars = []
        for i, (sx, sy) in enumerate([(1, 1), (1, -1), (-1, 1), (-1, -1)]):
            nodes[f"lo_bot_{i}"] = (sx * 2285, sy * 2285, 7000)
            nodes[f"lo_top_{i}"] = (sx * 2200, sy * 2200, 12006)
            nodes[f"hi_bot_{i}"] = (sx * 2100, sy * 2100, 13000)
            nodes[f"hi_top_{i}"] = (sx * 1900, sy * 1900, 17000)
            bars.append({"id": f"leg_lo_{i}", "from": f"lo_bot_{i}", "to": f"lo_top_{i}", "role": "LEG"})
            bars.append({"id": f"leg_hi_{i}", "from": f"hi_bot_{i}", "to": f"hi_top_{i}", "role": "LEG"})
        nn, nb, rep = bridge_segment_boundary_legs(nodes, bars, boundaries=[13000.0])
        self.assertEqual(rep["bridged"], 4)

    def test_idempotent_leg_connection(self):
        # 两节点间已有腿杆相连 → 不重复搭桥。
        nodes = {
            "n_lo_bot": (2285.0, 2285.0, 7000.0),
            "n_shared": (2200.0, 2200.0, 12500.0),
            "n_hi_top": (1900.0, 1900.0, 17000.0),
        }
        bars = [
            {"id": "leg_lo", "from": "n_lo_bot", "to": "n_shared", "role": "LEG"},
            {"id": "leg_hi", "from": "n_shared", "to": "n_hi_top", "role": "LEG"},
        ]
        nn, nb, rep = bridge_segment_boundary_legs(nodes, bars, boundaries=[12500.0])
        self.assertEqual(rep["bridged"], 0)

    def test_diag_connection_does_not_block(self):
        # 断口两端只有斜材相连（无腿杆）→ 仍搭桥（斜材不能替代腿）。
        nodes = {
            "n0": (2285.0, 2285.0, 10000.0),
            "n1": (2285.0, 2285.0, 12006.0),
            "n2": (0.0, 2285.0, 11000.0),
            "n3": (2100.0, 2100.0, 13000.0),
            "n4": (1900.0, 1900.0, 17000.0),
            "n5": (0.0, 2100.0, 14000.0),
        }
        bars = [
            {"id": "leg_lo", "from": "n0", "to": "n1", "role": "LEG"},
            {"id": "diag_lo", "from": "n1", "to": "n2", "role": "DIAG"},
            {"id": "leg_hi", "from": "n3", "to": "n4", "role": "LEG"},
            {"id": "diag_hi", "from": "n3", "to": "n5", "role": "DIAG"},
        ]
        nn, nb, rep = bridge_segment_boundary_legs(nodes, bars, boundaries=[13000.0])
        self.assertEqual(rep["bridged"], 1)
        bridges = [b for b in nb if b.get("geometry_origin") == "boundary_leg_bridge"]
        self.assertEqual(len(bridges), 1)
        self.assertEqual(set((bridges[0]["from"], bridges[0]["to"])), {"n1", "n3"})


if __name__ == "__main__":
    unittest.main()
