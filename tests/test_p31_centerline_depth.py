"""P3.1 深度横隔 + centerline 几何 filter 回归测试。"""

from __future__ import annotations

import unittest

from traceability.intake.centerline_geom_filter import (
    filter_drawing_bars,
    should_keep_centerline_segment,
    stem_uses_centerline_geom_filter,
)
from traceability.solve.tower_geometry import (
    classify_members,
    filter_diaphragm_bars_by_evidence,
    filter_panel_levels_for_diaphragms,
    generate_diaphragms,
)


class TestCenterlineGeomFilter(unittest.TestCase):
    def test_drop_dim_like_short_horizontal(self):
        ok, reason = should_keep_centerline_segment(
            0, 0, 200, 0, scale_mm=1.0,
            cfg={"min_len_mm": 80, "dim_max_len_mm": 450, "dim_angle_tol_deg": 8})
        self.assertFalse(ok)
        self.assertEqual(reason, "dim_like")

    def test_keep_structural_diagonal(self):
        ok, _ = should_keep_centerline_segment(
            0, 0, 500, 400, scale_mm=1.0,
            cfg={"min_len_mm": 80, "dim_max_len_mm": 450, "dim_angle_tol_deg": 8})
        self.assertTrue(ok)

    def test_filter_drawing_bars_05(self):
        bars = [
            {"bar_uid": "a", "x1": 0, "y1": 0, "x2": 150, "y2": 0},
            {"bar_uid": "b", "x1": 0, "y1": 0, "x2": 400, "y2": 300},
        ]
        kept, _, rep = filter_drawing_bars(bars, stem="35A1-JC1-05", overlay={
            "centerline_geom_filter": True,
            "mllm_keep_drop_sheets": ["35A1-JC1-05"],
            "centerline_geom_filter_by_stem": {
                "35A1-JC1-05": {"min_len_mm": 100, "dim_max_len_mm": 400},
            },
        })
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["bar_uid"], "b")
        self.assertEqual(rep["n_dropped"], 1)

    def test_stem_flag(self):
        self.assertTrue(stem_uses_centerline_geom_filter(
            "35A1-JC1-05",
            {"mllm_keep_drop_sheets": ["35A1-JC1-05"], "centerline_geom_filter": True},
        ))


class TestDiaphragmDepthFilter(unittest.TestCase):
    def test_skip_high_platform_levels(self):
        levels = [14000.0, 24000.0, 30024.0]
        kept, rep = filter_panel_levels_for_diaphragms(
            levels, z_cap=30000.0, exclusive=True)
        self.assertEqual(kept, [14000.0, 24000.0])
        self.assertEqual(len(rep["removed_high"]), 1)

    def test_generate_skips_hw_mismatch_corners(self):
        z = 14000.0
        hw = 1680.0
        nodes = {
            "c1": (hw, hw, z),
            "c2": (-hw, hw, z),
            "c3": (-hw, -hw, z),
            "c4": (hw, -hw, z),
            "bad": (500.0, 500.0, z),
        }
        report: dict = {}
        _, bars = generate_diaphragms(
            nodes, [],
            levels=[z],
            half_width_fn=lambda _z: hw,
            hw_tol_ratio=0.2,
            level_validation_report=report,
        )
        self.assertEqual(len(bars), 22)

    def test_filter_diaphragm_not_on_leg(self):
        z = 14000.0
        nodes = {
            "leg_a": (1000.0, 1000.0, 13000.0),
            "leg_b": (1000.0, 1000.0, 15000.0),
            "p1": (1000.0, 1000.0, z),
            "p2": (-1000.0, 1000.0, z),
            "far1": (5000.0, 5000.0, z),
            "far2": (6000.0, 5000.0, z),
        }
        bars = [
            {"id": "leg1", "from": "leg_a", "to": "leg_b", "role": "LEG"},
            {"id": "d1", "from": "p1", "to": "p2", "face": "diaphragm", "diaphragm": True},
            {"id": "d2", "from": "far1", "to": "far2", "face": "diaphragm", "diaphragm": True},
        ]
        roles = classify_members(nodes, bars)

        def hw(_z):
            return 1000.0

        kept, rep = filter_diaphragm_bars_by_evidence(
            nodes, bars, roles, half_width_fn=hw, leg_attach_mm=400.0)
        dia = [b for b in kept if b.get("diaphragm")]
        self.assertEqual(len(dia), 1)
        self.assertEqual(dia[0]["id"], "d1")
        self.assertEqual(rep["n_removed"], 1)


if __name__ == "__main__":
    unittest.main()
