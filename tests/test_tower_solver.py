"""阶段 5.1 & 5.3 验收：整高角腿降级 + 多段塔拼接缝合。

覆盖官网验收标准：
    * 整高合成角腿（corner_leg）/ 横隔面（diaphragm）降级为 internal helper，
      不计入 BOM / GLB 物理杆件数 / P-R 统计；
    * 多段立面（02/04/05/06/07/40）拼接处段边界节点去重（<=5mm），
      消除相邻段重叠横向连接杆，且拼接前后物理长度不失真。
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys = __import__("sys")
sys.path.insert(0, str(REPO))

from traceability.model import Component, EngineeringModel, SourceRef, SourceType  # noqa: E402


class InternalHelperExclusionTest(unittest.TestCase):
    """阶段 5.1：corner_leg / diaphragm 不计入物理杆件数。"""

    def test_is_internal_helper_flags_corner_and_diaphragm(self):
        from traceability.solve.tower_solver import _is_internal_helper

        def bar(props):
            return Component(id="b", name="b", kind="tower_bar",
                             source=SourceRef(SourceType.DERIVED, "s"),
                             properties=props)

        self.assertTrue(_is_internal_helper(bar({"corner_leg": True})))
        self.assertTrue(_is_internal_helper(bar({"diaphragm": True})))
        self.assertTrue(_is_internal_helper(bar({"face": "corner"})))
        self.assertTrue(_is_internal_helper(bar({"face": "diaphragm"})))
        self.assertTrue(_is_internal_helper(bar({"evidence_status": "derived"})))
        self.assertTrue(_is_internal_helper(bar({"geometry_class": "derived"})))
        # 普通物理杆件不是 internal helper
        self.assertFalse(_is_internal_helper(bar({"bar_id": "105", "face": "f"})))
        self.assertFalse(_is_internal_helper(bar({"evidence_status": "mirrored", "face": "b"})))

    def test_gate_reports_physical_bars_and_internal_helpers(self):
        from traceability.solve.tower_solver import tower_geometry_gate

        m = EngineeringModel(name="gate-test")
        m.add_component(Component(
            id="drawing_file", name="df", kind="drawing_file",
            source=SourceRef(SourceType.DRAWING, "s.dxf"), properties={},
        ))
        # 两个物理节点 + 2 根物理杆件 + 1 根 corner_leg 辅助线
        for nid, (x, y, z) in {"N1": (0.0, 0.0, 0.0), "N2": (100.0, 0.0, 0.0),
                               "N3": (0.0, 100.0, 0.0)}.items():
            m.add_component(Component(
                id=nid, name=nid, kind="tower_node",
                source=SourceRef(SourceType.DRAWING, "s.dxf"),
                properties={"x": x, "y": y, "z": z},
            ))
        m.add_component(Component(
            id="bar1", name="bar1", kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "s.dxf"),
            properties={"bar_id": "105", "from_node": "N1", "to_node": "N2"},
        ))
        m.add_component(Component(
            id="bar2", name="bar2", kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "s.dxf"),
            properties={"bar_id": "106", "from_node": "N2", "to_node": "N3"},
        ))
        m.add_component(Component(
            id="corner", name="corner", kind="tower_bar",
            source=SourceRef(SourceType.DERIVED, "s"),
            properties={"bar_id": "corner_leg_1", "from_node": "N1", "to_node": "N3",
                        "corner_leg": True, "face": "corner", "evidence_status": "derived"},
        ))

        gate = tower_geometry_gate(m)
        # 3 根杆件中 2 根物理 + 1 根 internal helper
        self.assertEqual(gate["bars"], 3)
        self.assertEqual(gate["internal_helpers"], 1)
        self.assertEqual(gate["physical_bars"], 2)


class SegmentBoundaryStitchTest(unittest.TestCase):
    """阶段 5.3：段边界节点去重（<=5mm）+ 重叠杆件消除。"""

    def test_merges_boundary_nodes_within_5mm(self):
        from traceability.solve.tower_geometry import stitch_segment_boundaries

        # 相邻两段的接头节点相距 3mm（<=5mm），应合并为共享节点
        nodes = {
            "A_bot": (0.0, 0.0, 1000.0),
            "A_top": (0.0, 0.0, 1003.0),  # 段 2 底端，与 A_bot 相距 3mm
            "B": (100.0, 0.0, 1000.0),
        }
        bars = [
            {"id": "leg_bot", "from": "A_bot", "to": "B"},
            {"id": "leg_top", "from": "A_top", "to": "B"},  # 与 leg_bot 重叠
        ]
        nn, nb, report = stitch_segment_boundaries(nodes, bars, boundary_tol_mm=5.0)
        self.assertGreaterEqual(report["merged_nodes"], 1, "段边界节点应合并")
        # 合并后两杆端点相同 -> 去重只剩 1 根
        self.assertEqual(len(nb), 1, "重叠横向连接杆应去重为 1 根")
        self.assertGreaterEqual(report["dedup_bars"], 1)

    def test_does_not_merge_nodes_beyond_5mm(self):
        from traceability.solve.tower_geometry import stitch_segment_boundaries

        nodes = {
            "A_bot": (0.0, 0.0, 1000.0),
            "A_top": (0.0, 0.0, 1020.0),  # 相距 20mm，>5mm，不应合并
            "B": (100.0, 0.0, 1000.0),
        }
        bars = [
            {"id": "leg_bot", "from": "A_bot", "to": "B"},
            {"id": "leg_top", "from": "A_top", "to": "B"},
        ]
        nn, nb, report = stitch_segment_boundaries(nodes, bars, boundary_tol_mm=5.0)
        self.assertEqual(report["merged_nodes"], 0, "超过 5mm 不应合并节点")
        self.assertEqual(len(nb), 2, "不重合的杆件不应去重")

    def test_stitch_preserves_physical_length(self):
        from traceability.solve.tower_geometry import stitch_segment_boundaries
        import math

        # 拼接前后物理长度不失真：合并只改节点身份，不缩放坐标
        nodes = {
            "N1": (0.0, 0.0, 0.0),
            "N2": (1000.0, 0.0, 0.0),
            "N2b": (1000.5, 0.0, 0.0),  # 段 2 起点，与 N2 相距 0.5mm
            "N3": (2000.0, 0.0, 0.0),
        }
        bars = [
            {"id": "seg1", "from": "N1", "to": "N2"},
            {"id": "seg2", "from": "N2b", "to": "N3"},
        ]
        nn, nb, report = stitch_segment_boundaries(nodes, bars, boundary_tol_mm=5.0)
        # seg1 长度保持 1000，seg2 长度保持 ~999.5（N2b 重指到 N2 后从 N2 到 N3=1000）
        merged = {b["id"]: b for b in nb}
        if "seg1" in merged:
            a, b = nn[merged["seg1"]["from"]], nn[merged["seg1"]["to"]]
            self.assertAlmostEqual(math.dist(a, b), 1000.0, places=1)
        if "seg2" in merged:
            a, b = nn[merged["seg2"]["from"]], nn[merged["seg2"]["to"]]
            # 合并后 seg2 从 N2(1000) 到 N3(2000) = 1000，不失真
            self.assertAlmostEqual(math.dist(a, b), 1000.0, places=1)


if __name__ == "__main__":
    unittest.main()
