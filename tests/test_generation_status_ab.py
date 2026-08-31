"""generation_status 采集 + TP 回归 diff 单元测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from traceability.eval.generation_status import collect_generation_status


class TestGenerationStatus(unittest.TestCase):
    def test_collect_diagonal_per_sheet(self):
        model = {
            "components": {
                "drawing_file": {
                    "properties": {
                        "diagonal_topology_report": {
                            "sheets": ["35A1-JC1-06"],
                            "totals": {"generated": 64, "fan_pairs": 8, "twist_pairs": 0},
                            "per_sheet": [{
                                "sheet": "35A1-JC1-06",
                                "n_candidates": 11,
                                "generated": 64,
                                "fan_pairs": 8,
                                "twist_pairs": 0,
                                "selection": {
                                    "mode": "p11",
                                    "kept": 8,
                                    "rejected": [
                                        {"reason": "span_off_grid", "z_lo": 1, "z_hi": 2},
                                    ],
                                    "beat_unit": 1014.0,
                                },
                            }],
                        },
                    },
                },
            },
        }
        st = collect_generation_status(model)
        self.assertEqual(st["diagonal_topology"]["totals"]["generated"], 64)
        self.assertEqual(len(st["diagonal_topology"]["per_sheet"]), 1)
        self.assertEqual(st["diagonal_topology"]["per_sheet"][0]["selection_rejected"], 1)
        self.assertIn("span_off_grid", st["diagonal_topology"]["per_sheet"][0]["reject_reasons"])


class TestDiffTpRegression(unittest.TestCase):
    def test_detects_lost_gt_bar(self):
        from scripts.diff_tp_regression import diff_tp_regression

        gt = {
            "nodes": {
                "a": (1000.0, 1000.0, 10000.0),
                "b": (0.0, 1000.0, 14000.0),
                "c": (1000.0, 0.0, 14000.0),
            },
            "bars": [
                {"id": "G1", "from": "a", "to": "b", "section": "L100"},
                {"id": "G2", "from": "a", "to": "c", "section": "L100"},
            ],
        }
        base_model = {
            "components": {
                "b1": {"kind": "tower_bar", "properties": {
                    "from_node": "n1", "to_node": "n2", "face": "f", "role": "DIAG",
                    "geometry_origin": "dxf_geom", "geometry_class": "recognized",
                    "evidence_status": "recognized", "id": "b1",
                }},
                "b2": {"kind": "tower_bar", "properties": {
                    "from_node": "n3", "to_node": "n4", "face": "l", "role": "DIAG",
                    "geometry_origin": "dxf_geom", "geometry_class": "recognized",
                    "evidence_status": "recognized", "id": "b2",
                }},
                "n1": {"kind": "tower_node", "properties": {"x": 1000, "y": 1000, "z": 10000}},
                "n2": {"kind": "tower_node", "properties": {"x": 0, "y": 1000, "z": 14000}},
                "n3": {"kind": "tower_node", "properties": {"x": 1000, "y": 0, "z": 14000}},
                "n4": {"kind": "tower_node", "properties": {"x": 500, "y": 0, "z": 12000}},
            },
        }
        # compare: drop side-view bar → lose G2 match potential
        cmp_model = json.loads(json.dumps(base_model))
        del cmp_model["components"]["b2"]
        del cmp_model["components"]["n3"]
        del cmp_model["components"]["n4"]

        rep = diff_tp_regression(
            gt=gt, baseline_model=base_model, compare_model=cmp_model, tol=500.0)
        self.assertGreaterEqual(rep["delta"]["full_tp"], 0)
        self.assertIn("lost_gt_by_role", rep)


if __name__ == "__main__":
    unittest.main()
