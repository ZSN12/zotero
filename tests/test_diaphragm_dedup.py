"""Regression tests for diaphragm deduplication and panel conservation audit."""

from __future__ import annotations

import copy
import unittest

from traceability.solve.tower_geometry import (
    generate_diaphragms,
    subdivide_legs_at_levels,
)


def _four_corners(z: float = 0.0):
    return {
        "pp": (1000.0, 1000.0, z),
        "np": (-1000.0, 1000.0, z),
        "nn": (-1000.0, -1000.0, z),
        "pn": (1000.0, -1000.0, z),
    }


def _geometric_endpoint_key(nodes, bar):
    points = []
    for nid in (bar["from"], bar["to"]):
        x, y, _z = nodes[nid]
        points.append((round(float(x), 6), round(float(y), 6)))
    return tuple(sorted(points))


class DiaphragmDedupTest(unittest.TestCase):
    def test_near_duplicate_levels_are_merged(self):
        report = {}
        nodes, bars = generate_diaphragms(
            _four_corners(), [], levels=[0.0, 100.0], min_z_gap=2000.0,
            dedup_report=report,
        )

        self.assertGreater(report["duplicates_removed"], 0)
        # P3.7c：每层 23 杆（+1 y 向全宽梁），两层 46、去重后 23。
        self.assertEqual(report["n_generated"], 46)
        self.assertEqual(report["n_deduped"], 23)
        self.assertEqual(len(bars), 23)
        self.assertTrue(report["groups"])
        self.assertTrue(any(b.get("diaphragm_dedup_merged") for b in bars))

        endpoint_keys = [_geometric_endpoint_key(nodes, b) for b in bars]
        self.assertEqual(len(endpoint_keys), len(set(endpoint_keys)))

    def test_report_does_not_change_no_duplicate_output(self):
        fixture = _four_corners()
        expected_nodes, expected_bars = generate_diaphragms(
            fixture, [], levels=[0.0]
        )
        report = {}
        actual_nodes, actual_bars = generate_diaphragms(
            fixture, [], levels=[0.0], dedup_report=report
        )

        self.assertEqual(actual_nodes, expected_nodes)
        self.assertEqual(actual_bars, expected_bars)
        self.assertEqual(report["duplicates_removed"], 0)
        self.assertEqual(report["n_generated"], report["n_deduped"])
        self.assertEqual(report["groups"], [])


class PanelConservationAuditTest(unittest.TestCase):
    def setUp(self):
        self.nodes = {"a": (0.0, 0.0, 0.0), "b": (0.0, 0.0, 6000.0)}
        self.bars = [{"id": "leg", "from": "a", "to": "b", "section": "L90X6"}]

    def test_exact_subdivision_is_conservative(self):
        _nodes, _bars, report = subdivide_legs_at_levels(
            self.nodes, self.bars, [2000.0, 4000.0]
        )
        audit = report["panel_conservation"]
        self.assertEqual(len(audit["legs"]), 1)
        self.assertEqual(audit["legs"][0]["leg"], "leg")
        self.assertAlmostEqual(audit["legs"][0]["orig_len_mm"], 6000.0)
        self.assertAlmostEqual(audit["legs"][0]["sum_seg_len_mm"], 6000.0)
        self.assertAlmostEqual(audit["legs"][0]["delta_mm"], 0.0)
        self.assertAlmostEqual(audit["max_abs_delta_mm"], 0.0)
        self.assertEqual(audit["violations"], [])
        self.assertEqual(audit["tol_mm"], 0.5)
        self.assertTrue(audit["ok"])

    def test_endpoint_and_duplicate_levels_remain_conservative(self):
        _nodes, bars, report = subdivide_legs_at_levels(
            self.nodes, self.bars, [0.0, 2000.0, 2000.0, 6000.0]
        )
        # Endpoint levels are ignored by the minimum-segment guard.  Duplicate
        # interior levels currently yield a zero-length segment, which is still
        # audited from the actual emitted segment bars and conserves length.
        self.assertEqual(report["segments_created"], 3)
        self.assertEqual(sum(b["from"] == b["to"] for b in bars), 1)
        self.assertEqual(report["panel_conservation"]["violations"], [])
        self.assertTrue(report["panel_conservation"]["ok"])

    def test_audit_is_observational_only(self):
        original_nodes = copy.deepcopy(self.nodes)
        original_bars = copy.deepcopy(self.bars)
        nodes, bars, _report = subdivide_legs_at_levels(
            self.nodes, self.bars, [2000.0, 4000.0]
        )

        self.assertEqual(self.nodes, original_nodes)
        self.assertEqual(self.bars, original_bars)
        self.assertEqual(nodes, {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 6000.0),
            "psn_100001": (0.0, 0.0, 2000.0),
            "psn_100002": (0.0, 0.0, 4000.0),
        })
        self.assertEqual(bars, [
            {
                "id": "leg_ps00", "from": "a", "to": "psn_100001",
                "section": "L90X6", "role": "LEG", "panel_subdivision": True,
                "root_bar_id": "leg", "derived_from": "leg", "subdiv_index": 0,
                "subdiv_count": 3,
            },
            {
                "id": "leg_ps01", "from": "psn_100001", "to": "psn_100002",
                "section": "L90X6", "role": "LEG", "panel_subdivision": True,
                "root_bar_id": "leg", "derived_from": "leg", "subdiv_index": 1,
                "subdiv_count": 3,
            },
            {
                "id": "leg_ps02", "from": "psn_100002", "to": "b",
                "section": "L90X6", "role": "LEG", "panel_subdivision": True,
                "root_bar_id": "leg", "derived_from": "leg", "subdiv_index": 2,
                "subdiv_count": 3,
            },
        ])


if __name__ == "__main__":
    unittest.main()
