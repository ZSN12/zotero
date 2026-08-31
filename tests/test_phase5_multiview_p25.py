"""Phase 5 multiview hypothesis + P2.5 gate block tests."""

from __future__ import annotations

import unittest

from traceability.intake.mllm_candidate_protocol import filter_mllm_bars_for_inject
from traceability.solve.multiview_hypothesis import (
    apply_multiview_hypotheses,
    associate_multiview_pairs,
)


def hw(_z: float) -> float:
    return 1950.0


class TestMultiviewHypothesis(unittest.TestCase):
    def test_associate_front_side(self):
        front = [{
            "bar_id": "f1", "face": "f", "axis": "xz",
            "seg": ((1950.0, 14000.0), (-1950.0, 17000.0)),
            "len2d": 5000.0, "z_mid": 15500.0,
        }]
        side = [{
            "bar_id": "l1", "face": "l", "axis": "yz",
            "seg": ((1950.0, 14000.0), (-1950.0, 17000.0)),
            "len2d": 4800.0, "z_mid": 15500.0,
        }]
        pairs = associate_multiview_pairs(front, side)
        self.assertEqual(len(pairs), 1)

    def test_apply_generates_bars(self):
        nodes = {
            "n1": (1950.0, 1600.0, 14000.0),
            "n2": (-1950.0, 1600.0, 17000.0),
            "n3": (1950.0, 1950.0, 14000.0),
            "n4": (-1950.0, -1950.0, 17000.0),
        }
        bars = [
            {"id": "35A1-JC1-06__bar_F", "from": "n1", "to": "n2",
             "face": "f", "role": "DIAG", "source_file": "35A1-JC1-06",
             "geometry_origin": "dxf_geom", "geometry_class": "recognized"},
            {"id": "35A1-JC1-06__bar_L", "from": "n3", "to": "n4",
             "face": "l", "role": "DIAG", "source_file": "35A1-JC1-06",
             "geometry_origin": "dxf_geom", "geometry_class": "recognized"},
        ]
        _, new_bars, rep = apply_multiview_hypotheses(
            nodes, bars, hw, sheet="35A1-JC1-06",
            z_window=(11000.0, 17500.0))
        self.assertGreaterEqual(rep["n_generated"], 1)
        gen = [b for b in new_bars if b.get("geometry_origin") == "multiview_hypothesis"]
        self.assertGreaterEqual(len(gen), 1)


class TestP25GateBlock(unittest.TestCase):
    def test_low_confidence_filtered(self):
        mllm = [
            {"bar_uid": "a", "x1": 0, "y1": 0, "x2": 100, "y2": 0, "confidence": 0.3},
            {"bar_uid": "b", "x1": 0, "y1": 10, "x2": 100, "y2": 10, "confidence": 0.8},
        ]
        kept, rejected, audit = filter_mllm_bars_for_inject(mllm, [])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["bar_uid"], "b")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(audit["n_rejected"], 1)


if __name__ == "__main__":
    unittest.main()
