"""P1.3/P1.4 + P2.1 + P3/P4 批次回归测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from traceability.solve.diagonal_topology import (
    infer_z_window_from_candidates,
    resolve_diagonal_sheet_configs,
    reconstruct_diagonal_sheets,
)
from traceability.solve.leg_chain_builder import build_leg_chains

REPO = Path(__file__).resolve().parent.parent


class TestDiagonalMultiSheet(unittest.TestCase):
    def test_infer_z_window(self):
        cands = [{"endpoints": [(0, 12000), (100, 15000)]},
                 {"endpoints": [(0, 14000), (100, 16000)]}]
        win = infer_z_window_from_candidates(cands, margin_mm=500, min_span_mm=2000)
        self.assertIsNotNone(win)
        self.assertLessEqual(win[0], 12000)
        self.assertGreaterEqual(win[1], 16000)

    def test_resolve_sheet_configs(self):
        spec = {
            "diagonal_topology_sheets": ["35A1-JC1-05", "35A1-JC1-06"],
            "diagonal_topology_sheet_config": {
                "35A1-JC1-06": {"z_window": [11000, 17500], "auto_z_window": False},
                "35A1-JC1-05": {"auto_z_window": True},
            },
        }
        cfgs = resolve_diagonal_sheet_configs(spec)
        self.assertEqual(len(cfgs), 2)
        self.assertFalse(cfgs[1]["auto_z_window"])
        self.assertTrue(cfgs[0]["auto_z_window"])

    def test_reconstruct_multi_sheet_synthetic(self):
        def hw(z):
            return 1950.0

        zt, zb = 17000.0, 14000.0
        nodes = {
            "n1": (hw(zt), hw(zt), zt),
            "n2": (hw(zb), -hw(zb), zb),
        }
        bars = [{
            "id": "35A1-JC1-07__bar_T_left_L", "from": "n1", "to": "n2",
            "face": "l", "role": "DIAG",
            "source_file": "35A1-JC1-07", "geometry_origin": "dxf_geom",
            "geometry_class": "recognized", "bar_id": "T", "layer": "1",
        }]
        spec = {
            "diagonal_topology_sheets": ["35A1-JC1-07"],
            "diagonal_topology_sheet_config": {
                "35A1-JC1-07": {"auto_z_window": True},
            },
        }
        _, new_bars, rep = reconstruct_diagonal_sheets(
            nodes, bars, hw, spec, panel_levels=[14000, 17000])
        self.assertIn("per_sheet", rep)
        self.assertEqual(len(rep["per_sheet"]), 1)
        self.assertGreaterEqual(rep["totals"]["twist_pairs"], 1)


class TestLegChainBuilder(unittest.TestCase):
    def test_four_corner_chains(self):
        nodes = {
            "a": (1000.0, 1000.0, 0.0),
            "b": (1000.0, 1000.0, 5000.0),
            "c": (-1000.0, 1000.0, 0.0),
            "d": (-1000.0, 1000.0, 5000.0),
        }
        bars = [
            {"id": "leg1", "from": "a", "to": "b", "role": "LEG"},
            {"id": "leg2", "from": "c", "to": "d", "role": "LEG"},
        ]
        rep = build_leg_chains(nodes, bars)
        self.assertGreaterEqual(rep["n_corners"], 2)
        self.assertTrue(rep["chains"])


class TestFrozenProfile(unittest.TestCase):
    def test_overlay_matches_frozen_critical_keys(self):
        frozen = json.loads(
            (REPO / "profiles/frozen_jc1_development.json").read_text(encoding="utf-8"))
        overlay = json.loads(
            (REPO / "examples/external/guowang_35A1/layer_overlay.json").read_text(encoding="utf-8"))
        keys = frozen["overlay_keys"]
        for k in ("diagonal_topology_sheets", "diaphragm_max_z_mm",
                  "collinear_stitch_max_single_len_mm"):
            self.assertEqual(overlay.get(k), keys.get(k), k)


if __name__ == "__main__":
    unittest.main()
