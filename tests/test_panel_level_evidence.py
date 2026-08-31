# -*- coding: utf-8 -*-
"""P4.1：节间平台多证据判定回归测试（逐层 source 分层 + manual 吸附）。"""
import unittest

from traceability.solve.tower_geometry import derive_panel_levels_detailed


def _fixture():
    """三层平台（z=8000/10000/12000）的塔身：每层 2 根水平杆 + 4 节点。"""
    nodes = {}
    bars = []
    k = 0
    for z in (8000, 10000, 12000):
        ids = []
        for x in (-2000, -600, 600, 2000):
            nid = f"n{k}"
            nodes[nid] = (float(x), 0.0, float(z))
            ids.append(nid)
            k += 1
        # 水平杆（2 根）+ 竖杆
        bars.append({"id": f"h1_{z}", "from": ids[0], "to": ids[1]})
        bars.append({"id": f"h2_{z}", "from": ids[2], "to": ids[3]})
        bars.append({"id": f"v_{z}", "from": ids[0], "to": ids[2]})
    return nodes, bars


class PanelLevelEvidenceTest(unittest.TestCase):
    def test_dxf_levels_with_evidence_records(self):
        """DXF 层带证据计数：n_bar/n_horiz/span 逐层可追溯。"""
        nodes, bars = _fixture()
        levels, records = derive_panel_levels_detailed(nodes, bars)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(r["source"] == "dxf" for r in records))
        for r in records:
            self.assertGreaterEqual(r["n_bar_evidence"], 1)
            self.assertGreaterEqual(r["n_horiz_evidence"], 1)
        self.assertEqual([r["z_mm"] for r in records],
                         sorted(r["z_mm"] for r in records))

    def test_manual_snap_corrects_dxf_level(self):
        """manual 层位吸附：DXF 层 z 与 manual 差 <= 500 时取 manual 值。"""
        nodes, bars = _fixture()
        # DXF 推到 8000（整百对齐），manual 给 8150（差 150 < 500）
        levels, records = derive_panel_levels_detailed(
            nodes, bars, manual_levels=[8150.0])
        snapped = [r for r in records if r["manual_snapped"]]
        self.assertEqual(len(snapped), 1)
        self.assertEqual(snapped[0]["z_mm"], 8150.0)
        self.assertEqual(snapped[0]["source"], "dxf",
                         "吸附后 source 仍为 dxf（有图纸证据，值被人工校正）")

    def test_manual_only_level_appended(self):
        """无 DXF 证据的 manual 层追加为 source=manual。"""
        nodes, bars = _fixture()
        levels, records = derive_panel_levels_detailed(
            nodes, bars, manual_levels=[9000.0])
        manual = [r for r in records if r["source"] == "manual"]
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["z_mm"], 9000.0)

    def test_no_manual_passthrough(self):
        """无 manual 输入：与旧 derive_panel_levels 行为兼容（纯 dxf）。"""
        nodes, bars = _fixture()
        levels, records = derive_panel_levels_detailed(nodes, bars)
        self.assertEqual(len(levels), len(records))
        self.assertTrue(all(not r["manual_snapped"] for r in records))

    def test_distant_manual_not_snapped(self):
        """manual 距离 > snap 容差：不吸附，作为独立 manual 层追加。"""
        nodes, bars = _fixture()
        levels, records = derive_panel_levels_detailed(
            nodes, bars, manual_levels=[8050.0 + 600.0])  # 距 8000 层 650 > 500
        snapped = [r for r in records if r["manual_snapped"]]
        manual = [r for r in records if r["source"] == "manual"]
        self.assertEqual(len(snapped), 0)
        self.assertEqual(len(manual), 1)


if __name__ == "__main__":
    unittest.main()


class SubdivideConservationTest(unittest.TestCase):
    """P4.2：主腿细分长度守恒 <= 0.1%（切点直线插值的理论验证）。"""

    def test_length_conservation_exact_cut(self):
        import math
        from traceability.solve.tower_geometry import subdivide_legs_at_levels
        # 通长腿 8000→12000，切点 9000/10000/11000（直线插值 → 守恒精确）
        nodes = {"a": (2000.0, 0.0, 8000.0), "b": (1800.0, 0.0, 12000.0)}
        bars = [{"id": "leg1", "from": "a", "to": "b"}]
        nn, nb, rep = subdivide_legs_at_levels(
            nodes, bars, [9000.0, 10000.0, 11000.0])
        self.assertEqual(rep["segments_created"], 4)
        self.assertLessEqual(
            rep["length_conservation_max_rel_err"], 0.001,
            "直线插值切分长度守恒必须 <= 0.1%")

    def test_length_conservation_node_reuse_jitter(self):
        """切点复用已有节点（±1mm 吸附抖动）：守恒仍须 <= 0.1%。"""
        from traceability.solve.tower_geometry import subdivide_legs_at_levels
        # 已有节点在切点附近偏 1mm
        nodes = {
            "a": (2000.0, 0.0, 8000.0),
            "b": (1600.0, 0.0, 12000.0),
            "existing": (1950.0, 0.0, 9999.5),  # 切点 10000 附近 0.5mm 抖动
        }
        bars = [{"id": "leg1", "from": "a", "to": "b"}]
        nn, nb, rep = subdivide_legs_at_levels(
            nodes, bars, [9000.0, 10000.0])
        self.assertLessEqual(
            rep["length_conservation_max_rel_err"], 0.001)
        self.assertIn("length_conservation_max_rel_err", rep)
