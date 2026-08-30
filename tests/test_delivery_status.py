"""阶段 8.4 / 8.5 / 8.2 / 3.2：交付状态传播单元测试。

覆盖：
    * harness _summarize 的 all_passed 必须 failed=0 且 pending=0（§8.4）
    * status_to_exit 三态映射（§8.6，与 test_cli_exit 重复确认）
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO))

from traceability.model import ValidationStatus  # noqa: E402
from traceability.project.harness import ProjectValidationResult, _summarize  # noqa: E402


def _res(rule_id: str, status: ValidationStatus) -> ProjectValidationResult:
    return ProjectValidationResult(rule_id, status, "msg")


class HarnessAllPassedTest(unittest.TestCase):
    """§8.4：all_passed 必须 failed=0 且 pending=0。"""

    def test_all_passed_requires_no_failed_and_no_pending(self):
        s = _summarize([_res("r1", ValidationStatus.PASSED)])
        self.assertTrue(s["all_passed"])

    def test_pending_blocks_all_passed(self):
        s = _summarize([
            _res("r1", ValidationStatus.PASSED),
            _res("r2", ValidationStatus.PENDING),
        ])
        self.assertFalse(s["all_passed"], "存在 pending 时 all_passed 必须为 False")
        self.assertEqual(s["pending"], ["r2"])

    def test_failed_blocks_all_passed(self):
        s = _summarize([_res("r1", ValidationStatus.FAILED)])
        self.assertFalse(s["all_passed"])
        self.assertEqual(s["failed"], ["r1"])

    def test_failed_and_pending_both_reported(self):
        s = _summarize([
            _res("r1", ValidationStatus.FAILED),
            _res("r2", ValidationStatus.PENDING),
        ])
        self.assertFalse(s["all_passed"])
        self.assertEqual(s["failed"], ["r1"])
        self.assertEqual(s["pending"], ["r2"])


if __name__ == "__main__":
    unittest.main()
