# -*- coding: utf-8 -*-
"""P7：MLLM 候选协议 + 置信度分层 + 双源交叉验证回归测试。"""
import unittest

from traceability.intake.mllm_candidate_protocol import (
    CANDIDATE_PROTOCOL_VERSION,
    CandidateRecord,
    apply_confidence_gate,
    cross_validate,
    records_to_evidence,
    stratify,
)


def _bar(uid, x1, y1, x2, y2, conf=None):
    b = {"bar_uid": uid, "x1": x1, "y1": y1, "x2": x2, "y2": y2}
    if conf is not None:
        b["confidence"] = conf
    return b


class CrossValidateTest(unittest.TestCase):
    def test_consistent_pair_gets_bonus(self):
        """双源一致：置信度 +0.2，cross_source=consistent。"""
        mllm = [_bar("m1", 100, 100, 400, 500, conf=0.6)]
        dxf = [{"component_id": "d1", "x1": 105, "y1": 98, "x2": 398, "y2": 505}]
        records, report = cross_validate(mllm, dxf, model_name="k3-256k")
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.cross_source, "consistent")
        self.assertEqual(r.matched_component_id, "d1")
        self.assertAlmostEqual(r.confidence_effective, 0.8)
        self.assertEqual(r.stratium, "high")  # 0.6+0.2=0.8 >= 0.7
        self.assertEqual(report["consistent"], 1)
        self.assertEqual(report["consistency_rate"], 1.0)

    def test_mllm_only_no_bonus(self):
        """仅 MLLM 检出：无加成，置信度原样。"""
        mllm = [_bar("m1", 100, 100, 400, 500, conf=0.6)]
        records, report = cross_validate(mllm, [])
        self.assertEqual(records[0].cross_source, "mllm_only")
        self.assertAlmostEqual(records[0].confidence_effective, 0.6)
        self.assertEqual(records[0].stratium, "medium")
        self.assertEqual(report["mllm_only"], 1)

    def test_dxf_only_appended(self):
        """ezdxf 独有候选：source_agent=dxf 候补记录。"""
        mllm = []
        dxf = [{"component_id": "d1", "x1": 0, "y1": 0, "x2": 300, "y2": 400}]
        records, report = cross_validate(mllm, dxf)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_agent, "dxf")
        self.assertEqual(records[0].cross_source, "dxf_only")
        self.assertEqual(report["dxf_only"], 1)

    def test_spatial_mismatch_not_consistent(self):
        """空间不匹配（角度差大）：不算 consistent。"""
        mllm = [_bar("m1", 0, 0, 100, 100)]       # 45°
        dxf = [{"component_id": "d1", "x1": 0, "y1": 0, "x2": 100, "y2": 0}]  # 0°
        records, _ = cross_validate(mllm, dxf)
        self.assertEqual(records[0].cross_source, "mllm_only")

    def test_protocol_version_stamped(self):
        """协议版本随记录与报告打标。"""
        records, report = cross_validate([_bar("m1", 0, 0, 1, 1)], [])
        self.assertEqual(records[0].protocol_version, CANDIDATE_PROTOCOL_VERSION)
        self.assertEqual(report["protocol_version"], CANDIDATE_PROTOCOL_VERSION)


class ConfidenceGateTest(unittest.TestCase):
    def test_low_rejected(self):
        """low 层（<0.4）拒绝入模，证据保留。"""
        recs = [
            CandidateRecord("a", 0, 0, 1, 1, "mllm", 0.3, 0.3, "low"),
            CandidateRecord("b", 0, 0, 1, 1, "mllm", 0.6, 0.6, "medium"),
            CandidateRecord("c", 0, 0, 1, 1, "mllm", 0.9, 0.9, "high"),
        ]
        accepted, rejected, counts = apply_confidence_gate(recs)
        self.assertEqual([r.bar_uid for r in accepted], ["b", "c"])
        self.assertEqual([r.bar_uid for r in rejected], ["a"])
        self.assertEqual(counts["rejected_low_confidence"], 1)
        self.assertEqual(counts["accepted_medium_review"], 1)

    def test_stratify_boundaries(self):
        """分层边界：0.7 high / 0.4 medium / 低于 low。"""
        self.assertEqual(stratify(0.7), "high")
        self.assertEqual(stratify(0.69), "medium")
        self.assertEqual(stratify(0.4), "medium")
        self.assertEqual(stratify(0.39), "low")


class EvidenceTest(unittest.TestCase):
    def test_records_to_evidence_shape(self):
        """证据块结构：summary + gate + records 三段。"""
        records, report = cross_validate(
            [_bar("m1", 0, 0, 100, 100, conf=0.5)], [])
        _, _, counts = apply_confidence_gate(records)
        ev = records_to_evidence(records, report, counts)
        self.assertEqual(ev["protocol_version"], CANDIDATE_PROTOCOL_VERSION)
        self.assertIn("summary", ev)
        self.assertIn("confidence_gate", ev)
        self.assertEqual(len(ev["records"]), 1)
        self.assertEqual(ev["records"][0]["bar_uid"], "m1")


if __name__ == "__main__":
    unittest.main()
