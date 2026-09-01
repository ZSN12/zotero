"""P3.2/P3.3 横隔层 cap + 横担 FP 剔除回归测试。"""

from __future__ import annotations

import unittest

from traceability.solve.tower_geometry import (
    classify_members,
    filter_panel_levels_for_diaphragms,
    prune_spurious_crossarm_bars,
    resolve_diaphragm_z_cap,
)


class TestDiaphragmZCap(unittest.TestCase):
    def test_max_z_exclusive(self):
        levels = [14000.0, 24000.0, 30024.0, 30800.0]
        kept, rep = filter_panel_levels_for_diaphragms(
            levels, z_cap=30000.0, exclusive=True)
        self.assertEqual(kept, [14000.0, 24000.0])
        self.assertEqual(len(rep["removed_high"]), 2)

    def test_crossarm_layers_tighten_cap(self):
        layers = [{"z_lo": 29800.0, "z_hi": 31000.0, "arm_mm": 2200.0}]
        cap = resolve_diaphragm_z_cap(
            diaphragm_max_z_mm=30000.0,
            crossarm_layers=layers,
            crossarm_margin_mm=200.0,
        )
        self.assertAlmostEqual(cap, 29600.0)
        kept, _ = filter_panel_levels_for_diaphragms(
            [24000.0, 30024.0], z_cap=cap, exclusive=True)
        self.assertEqual(kept, [24000.0])


class TestCrossarmFpPrune(unittest.TestCase):
    def test_body_zone_cross_removed(self):
        nodes = {
            "a": (1500.0, 0.0, 15000.0),
            "b": (2500.0, 0.0, 15000.0),
        }
        bars = [{
            "id": "c1", "from": "a", "to": "b", "face": "f", "role": "CROSS",
        }]
        roles = {"c1": "CROSS"}

        def hw(_z):
            return 1000.0

        kept, rep = prune_spurious_crossarm_bars(
            nodes, bars, roles,
            half_width_fn=hw,
            crossarm_zone_z_min_mm=29000.0,
        )
        self.assertEqual(len(kept), 0)
        self.assertEqual(rep["n_removed"], 1)
        self.assertEqual(rep["removed"][0]["reason"], "below_crossarm_zone")

    def test_insufficient_radial_extension(self):
        nodes = {
            "a": (800.0, 0.0, 31000.0),
            "b": (900.0, 0.0, 31000.0),
        }
        bars = [{
            "id": "c2", "from": "a", "to": "b", "face": "f", "role": "CROSS",
        }]
        roles = classify_members(nodes, bars)

        def hw(_z):
            return 1000.0

        kept, rep = prune_spurious_crossarm_bars(
            nodes, bars, roles,
            half_width_fn=hw,
            crossarm_zone_z_min_mm=29000.0,
            crossarm_radial_ratio=1.3,
        )
        # P1.2 修复后：|x|=900 < hw=1000 → 塔身内水平杆（角柱横梁），
        # 不再按「外伸不足」误杀（06 册 19 根 marker_synth 横杆曾在此全灭）。
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(rep["removed"]), 0)

    def test_valid_crossarm_kept(self):
        nodes = {
            "a": (2200.0, 0.0, 31000.0),
            "b": (2400.0, 0.0, 31000.0),
        }
        bars = [{
            "id": "c3", "from": "a", "to": "b", "face": "f", "role": "CROSS",
        }]
        roles = classify_members(nodes, bars)

        def hw(_z):
            return 650.0

        def arm(z):
            return 2200.0 if z >= 30000 else 0.0

        kept, rep = prune_spurious_crossarm_bars(
            nodes, bars, roles,
            half_width_fn=hw,
            crossarm_half_width_fn=arm,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(rep["n_removed"], 0)


if __name__ == "__main__":
    unittest.main()
