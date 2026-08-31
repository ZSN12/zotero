"""P2.3 分角色共线拼接回归测试。"""

from __future__ import annotations

import unittest

import traceability.solve.tower_geometry as g


class TestRoleSpecificStitch(unittest.TestCase):
    def test_leg_blocked_across_platform(self):
        levels = [8000.0]
        nodes = {
            "a1": (1000.0, 1000.0, 5000.0),
            "a2": (1000.0, 1000.0, 7900.0),
            "b1": (1000.0, 1000.0, 8100.0),
            "b2": (1000.0, 1000.0, 10000.0),
        }
        bars = [
            {"id": "l1", "from": "a1", "to": "a2", "face": "f", "role": "LEG",
             "geometry_class": "recognized", "source_file": "35A1-JC1-02"},
            {"id": "l2", "from": "b1", "to": "b2", "face": "f", "role": "LEG",
             "geometry_class": "recognized", "source_file": "35A1-JC1-02"},
        ]
        out, _, rep = g.stitch_collinear_bars(
            nodes, bars, gap_mm=400.0, ang_deg=10.0, max_segments=2,
            role_specific=True, panel_levels=levels)
        self.assertEqual(rep["merged_groups"], 0)
        self.assertGreater(rep["role_rejected"].get("leg_platform_break", 0), 0)

    def test_leg_stitch_same_platform_band(self):
        levels = [8000.0]
        nodes = {
            "n1": (1000.0, 1000.0, 8200.0),
            "n2": (1000.0, 1000.0, 9000.0),
            "n3": (1000.0, 1000.0, 9200.0),
            "n4": (1000.0, 1000.0, 10000.0),
        }
        bars = [
            {"id": "l1", "from": "n1", "to": "n2", "face": "f", "role": "LEG",
             "geometry_class": "recognized"},
            {"id": "l2", "from": "n3", "to": "n4", "face": "f", "role": "LEG",
             "geometry_class": "recognized"},
        ]
        out, _, rep = g.stitch_collinear_bars(
            nodes, bars, gap_mm=400.0, ang_deg=10.0, max_segments=2,
            role_specific=True, panel_levels=levels)
        self.assertEqual(rep["merged_groups"], 1)
        self.assertEqual(len(out), 1)

    def test_diag_requires_same_source(self):
        nodes = {
            "a1": (0.0, 1000.0, 5000.0), "a2": (800.0, 1000.0, 6000.0),
            "b1": (850.0, 1000.0, 6050.0), "b2": (1600.0, 1000.0, 7000.0),
        }
        bars = [
            {"id": "d1", "from": "a1", "to": "a2", "face": "f", "role": "DIAG",
             "geometry_class": "recognized", "source_file": "35A1-JC1-05",
             "drawing_view": "front"},
            {"id": "d2", "from": "b1", "to": "b2", "face": "f", "role": "DIAG",
             "geometry_class": "recognized", "source_file": "35A1-JC1-06",
             "drawing_view": "front"},
        ]
        out, _, rep = g.stitch_collinear_bars(
            nodes, bars, gap_mm=400.0, ang_deg=10.0, max_segments=2,
            role_specific=True)
        self.assertEqual(rep["merged_groups"], 0)
        self.assertGreater(rep["role_rejected"].get("diag_source_mismatch", 0), 0)

    def test_diag_same_source_stitches(self):
        nodes = {
            "a1": (0.0, 1000.0, 5000.0), "a2": (800.0, 1000.0, 6000.0),
            "b1": (850.0, 1000.0, 6050.0), "b2": (1600.0, 1000.0, 7000.0),
        }
        bars = [
            {"id": "d1", "from": "a1", "to": "a2", "face": "f", "role": "DIAG",
             "geometry_class": "recognized", "source_file": "35A1-JC1-05",
             "drawing_view": "front", "geometry_origin": "dxf_geom"},
            {"id": "d2", "from": "b1", "to": "b2", "face": "f", "role": "DIAG",
             "geometry_class": "recognized", "source_file": "35A1-JC1-05",
             "drawing_view": "front", "geometry_origin": "dxf_geom"},
        ]
        out, _, rep = g.stitch_collinear_bars(
            nodes, bars, gap_mm=400.0, ang_deg=10.0, max_segments=2,
            min_merged_len_mm=600.0, role_specific=True)
        self.assertEqual(rep["merged_groups"], 1)

    def test_horiz_blocks_cross_center(self):
        nodes = {
            "l1": (-1500.0, 1000.0, 10000.0), "l2": (-200.0, 1000.0, 10000.0),
            "r1": (200.0, 1000.0, 10000.0), "r2": (1500.0, 1000.0, 10000.0),
        }
        bars = [
            {"id": "h1", "from": "l1", "to": "l2", "face": "f", "role": "HORIZ",
             "geometry_class": "recognized"},
            {"id": "h2", "from": "r1", "to": "r2", "face": "f", "role": "HORIZ",
             "geometry_class": "recognized"},
        ]
        out, _, rep = g.stitch_collinear_bars(
            nodes, bars, gap_mm=400.0, ang_deg=10.0, max_segments=2,
            role_specific=True, horiz_center_tol_mm=300.0)
        self.assertEqual(rep["merged_groups"], 0)
        self.assertGreater(rep["role_rejected"].get("horiz_cross_center", 0), 0)

    def test_horiz_blocks_z_mismatch(self):
        nodes = {
            "a1": (1000.0, 1000.0, 10000.0), "a2": (2000.0, 1000.0, 10000.0),
            "b1": (2050.0, 1000.0, 10100.0), "b2": (3000.0, 1000.0, 10100.0),
        }
        bars = [
            {"id": "h1", "from": "a1", "to": "a2", "face": "f", "role": "HORIZ",
             "geometry_class": "recognized"},
            {"id": "h2", "from": "b1", "to": "b2", "face": "f", "role": "HORIZ",
             "geometry_class": "recognized"},
        ]
        out, _, rep = g.stitch_collinear_bars(
            nodes, bars, gap_mm=400.0, ang_deg=10.0, max_segments=2,
            role_specific=True, horiz_z_tol_mm=80.0)
        self.assertEqual(rep["merged_groups"], 0)
        self.assertGreater(rep["role_rejected"].get("horiz_z_mismatch", 0), 0)

    def test_legacy_fragments_without_role(self):
        """无 role 标注时保持旧行为（向后兼容）。"""
        nodes = {
            "n1": (0.0, 1000.0, 0.0), "n2": (800.0, 1000.0, 600.0),
            "n3": (850.0, 1000.0, 650.0), "n4": (1600.0, 1000.0, 1200.0),
        }
        bars = [
            {"id": "f1", "from": "n1", "to": "n2", "face": "f",
             "geometry_class": "recognized"},
            {"id": "f2", "from": "n3", "to": "n4", "face": "f",
             "geometry_class": "recognized"},
        ]
        out, _, rep = g.stitch_collinear_bars(
            nodes, bars, gap_mm=400.0, ang_deg=10.0, max_segments=2,
            role_specific=True)
        self.assertEqual(rep["merged_groups"], 1)


if __name__ == "__main__":
    unittest.main()
