# -*- coding: utf-8 -*-
"""P0-6（2026-09-08）：人工复核豁免机制测试——pending 与 FAILED 双通道。

背景：deliver_status 三态中 failed 的唯一硬因是 r_project_bom_master
的 6 件真超计（件号错挂/横担四面展开 vs BOM qty=2 结构语义分歧）。
AI 不替人工盖章——只落机制：project harness 豁免通道 + FAILED 豁免
必须显式 confirm_conflict=True（人工裁决「模型侧正确/图纸数据分歧」）。
豁免永远不是 passed：规则标 review_exempted，报告可见，带指纹时效。
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from traceability.project.delivery import _apply_review_exemptions


def _harness_with(rule: str, status: str, message: str) -> dict:
    """最小 harness 摘要（_summarize 同构）。"""
    counts = {status: 1}
    return {
        "counts": counts,
        "failed": [rule] if status == "failed" else [],
        "pending": [rule] if status == "pending" else [],
        "results": [{"rule": rule, "status": status, "message": message}],
        "all_passed": False,
    }


def _fp(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]


class PendingExemptionTest(unittest.TestCase):
    def test_pending_exempted_with_fingerprint(self):
        h = _harness_with("r_some_pending", "pending", "待人工核对：XX")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "exemptions.json"
            p.write_text(json.dumps({
                "reviewed_by": "engineer",
                "reviewed_at": "2026-09-08",
                "exemptions": {
                    "r_some_pending": {
                        "message_fingerprint": _fp("待人工核对：XX"),
                        "reason": "已核对，图纸侧多注",
                    },
                },
            }), encoding="utf-8")
            d = _apply_review_exemptions(h, p)
        self.assertEqual(d["applied"][0]["rule"], "r_some_pending")
        self.assertEqual(h["pending"], [])
        self.assertEqual(h["review_exempted"], ["r_some_pending"])
        self.assertEqual(h["results"][0]["status"], "review_exempted")
        self.assertIn("人工复核豁免", h["results"][0]["message"])
        self.assertEqual(h["counts"]["pending"], 0)
        self.assertEqual(h["counts"]["review_exempted"], 1)
        self.assertTrue(h["all_passed"])

    def test_fingerprint_mismatch_rejected(self):
        h = _harness_with("r_some_pending", "pending", "待人工核对：XX")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "exemptions.json"
            p.write_text(json.dumps({
                "exemptions": {
                    "r_some_pending": {"message_fingerprint": "deadbeefdeadbeef"},
                },
            }), encoding="utf-8")
            d = _apply_review_exemptions(h, p)
        self.assertEqual(d["applied"], [])
        self.assertEqual(len(d["rejected"]), 1)
        self.assertIn("指纹", d["rejected"][0]["reason"])
        self.assertEqual(h["pending"], ["r_some_pending"])
        self.assertFalse(h["all_passed"])

    def test_expired_rejected(self):
        h = _harness_with("r_some_pending", "pending", "待人工核对：XX")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "exemptions.json"
            p.write_text(json.dumps({
                "expires": "2020-01-01",
                "exemptions": {
                    "r_some_pending": {
                        "message_fingerprint": _fp("待人工核对：XX")},
                },
            }), encoding="utf-8")
            d = _apply_review_exemptions(h, p)
        self.assertEqual(d["applied"], [])
        self.assertEqual(h["pending"], ["r_some_pending"])


class FailedExemptionTest(unittest.TestCase):
    """P0-6 新增：FAILED（数据冲突）豁免——必须显式 confirm_conflict。"""

    def test_failed_exempted_with_confirm_conflict(self):
        h = _harness_with(
            "r_project_bom_master", "failed",
            "6 件超计：602/623/3906/3907/3910/3914")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "exemptions.json"
            p.write_text(json.dumps({
                "reviewed_by": "engineer",
                "reviewed_at": "2026-09-08",
                "exemptions": {
                    "r_project_bom_master": {
                        "message_fingerprint":
                            _fp("6 件超计：602/623/3906/3907/3910/3914"),
                        "confirm_conflict": True,
                        "reason": "横担四面展开 vs BOM qty=2 属结构语义分歧，"
                                  "模型侧维持现状",
                    },
                },
            }), encoding="utf-8")
            d = _apply_review_exemptions(h, p)
        self.assertEqual(len(d["applied"]), 1)
        self.assertTrue(d["applied"][0]["confirm_conflict"])
        self.assertEqual(h["failed"], [])
        self.assertEqual(h["review_exempted"], ["r_project_bom_master"])
        self.assertEqual(h["results"][0]["status"], "review_exempted")
        self.assertEqual(h["counts"]["failed"], 0)
        self.assertEqual(h["counts"]["review_exempted"], 1)
        self.assertTrue(h["all_passed"],
                        "failed 清零且无 pending → all_passed 应为 True")

    def test_failed_without_confirm_conflict_rejected(self):
        h = _harness_with(
            "r_project_bom_master", "failed", "6 件超计")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "exemptions.json"
            p.write_text(json.dumps({
                "exemptions": {
                    "r_project_bom_master": {
                        "message_fingerprint": _fp("6 件超计"),
                        "reason": "没有勾 confirm_conflict 的豁免",
                    },
                },
            }), encoding="utf-8")
            d = _apply_review_exemptions(h, p)
        self.assertEqual(d["applied"], [])
        self.assertIn("confirm_conflict", d["rejected"][0]["reason"])
        self.assertEqual(h["failed"], ["r_project_bom_master"])
        self.assertEqual(h["results"][0]["status"], "failed",
                         "未确认的冲突豁免不得静默改状态")

    def test_failed_fingerprint_mismatch_rejected(self):
        h = _harness_with(
            "r_project_bom_master", "failed", "6 件超计")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "exemptions.json"
            p.write_text(json.dumps({
                "exemptions": {
                    "r_project_bom_master": {
                        "message_fingerprint": "0" * 16,
                        "confirm_conflict": True,
                    },
                },
            }), encoding="utf-8")
            d = _apply_review_exemptions(h, p)
        self.assertEqual(d["applied"], [])
        self.assertEqual(h["failed"], ["r_project_bom_master"])

    def test_exempted_not_reported_as_passed(self):
        """红线：豁免 ≠ 通过——review_exempted 状态在结果里永远可见。"""
        h = _harness_with(
            "r_project_bom_master", "failed", "6 件超计")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "exemptions.json"
            p.write_text(json.dumps({
                "exemptions": {
                    "r_project_bom_master": {
                        "message_fingerprint": _fp("6 件超计"),
                        "confirm_conflict": True,
                    },
                },
            }), encoding="utf-8")
            _apply_review_exemptions(h, p)
        statuses = [r["status"] for r in h["results"]]
        self.assertNotIn("passed", statuses)
        self.assertIn("review_exempted", statuses)


class EdgeCaseTest(unittest.TestCase):
    def test_missing_file_noop(self):
        h = _harness_with("r_x", "pending", "msg")
        d = _apply_review_exemptions(h, Path("/nonexistent/ex.json"))
        self.assertEqual(d["applied"], [])
        self.assertEqual(h["pending"], ["r_x"])

    def test_rule_not_pending_or_failed_rejected(self):
        """规则已 passed 时豁免无对象——拒绝（防错盖）。"""
        h = _harness_with("r_x", "passed", "OK")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "exemptions.json"
            p.write_text(json.dumps({
                "exemptions": {
                    "r_x": {"message_fingerprint": _fp("OK")},
                },
            }), encoding="utf-8")
            d = _apply_review_exemptions(h, p)
        self.assertEqual(d["applied"], [])
        self.assertEqual(h["results"][0]["status"], "passed")

    def test_malformed_json_rejected_gracefully(self):
        h = _harness_with("r_x", "pending", "msg")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "exemptions.json"
            p.write_text("{not json", encoding="utf-8")
            d = _apply_review_exemptions(h, p)
        self.assertEqual(d["applied"], [])
        self.assertIn("不可读", d["rejected"][0]["reason"])
        self.assertEqual(h["pending"], ["r_x"])


if __name__ == "__main__":
    unittest.main()
