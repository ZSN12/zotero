"""Phase 6 / P2.4：candidate lifecycle + geom_method 解析回归测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from traceability.intake.candidate_lifecycle import (
    append_lifecycle_to_model,
    build_lifecycle_block,
    entries_from_centerline_drops,
    entries_from_confidence_rejects,
    lifecycle_blocks_from_model,
    lifecycle_to_review_groups,
)
from traceability.intake.mllm_candidate_protocol import CandidateRecord
from traceability.intake.tower_spec import resolve_geom_method_for_sheet

REPO = Path(__file__).resolve().parent.parent


class TestCandidateLifecycle(unittest.TestCase):
    def test_confidence_rejects(self):
        rec = CandidateRecord(
            bar_uid="x1", x1=0, y1=0, x2=10, y2=0,
            source_agent="mllm", confidence_effective=0.2, stratium="low",
        )
        entries = entries_from_confidence_rejects([rec], sheet_stem="35A1-JC1-05")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].reason, "low_confidence")

    def test_centerline_drops(self):
        cands = [
            {"bar_uid": "b1", "x1": 0, "y1": 0, "x2": 100, "y2": 0},
            {"bar_uid": "b2", "x1": 0, "y1": 10, "x2": 100, "y2": 10},
        ]
        dropped = entries_from_centerline_drops(
            cands, {"b1"}, sheet_stem="35A1-JC1-05")
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0].bar_uid, "b2")

    def test_lifecycle_to_review(self):
        blk = build_lifecycle_block(
            sheet_stem="35A1-JC1-05",
            geom_method="centerline",
            n_in=2,
            n_accepted=1,
            rejected_entries=entries_from_centerline_drops(
                [{"bar_uid": "b2", "x1": 0, "y1": 0, "x2": 1, "y2": 0}],
                set(), sheet_stem="35A1-JC1-05"),
        )
        groups = lifecycle_to_review_groups([blk.to_dict()])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "mllm_rejected_candidate")


class TestGeomMethodResolve(unittest.TestCase):
    def test_keep_drop_sheets_use_centerline(self):
        ov = REPO / "examples/external/guowang_35A1/layer_overlay.json"
        self.assertEqual(
            resolve_geom_method_for_sheet("35A1-JC1-05", ov, mergeable=True),
            "centerline",
        )
        self.assertEqual(
            resolve_geom_method_for_sheet("35A1-JC1-06", ov, mergeable=True),
            "auto",
        )

    def test_non_mergeable_uses_ezdxf(self):
        ov = REPO / "examples/external/guowang_35A1/layer_overlay.json"
        self.assertEqual(
            resolve_geom_method_for_sheet("35A1-JC1-05", ov, mergeable=False),
            "ezdxf",
        )

    def test_frozen_overlay_keys(self):
        frozen = json.loads(
            (REPO / "profiles/frozen_jc1_development.json").read_text(encoding="utf-8"))
        keys = frozen["overlay_keys"]
        self.assertIn("mllm_keep_drop_sheets", keys)
        self.assertIn("35A1-JC1-05", keys["mllm_keep_drop_sheets"])


class TestBlindEvalScaffold(unittest.TestCase):
    def test_blind_split_empty(self):
        split = json.loads((REPO / "examples/dataset_split.json").read_text(encoding="utf-8"))
        bt = split["splits"]["blind_test"]["datasets"]
        self.assertEqual(bt, [])


if __name__ == "__main__":
    unittest.main()
