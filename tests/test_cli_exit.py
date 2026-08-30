"""阶段 8.6：CLI 退出码统一常量测试。

权威映射（计划 §8.6）：
    verified -> 0, failed -> 1, review_required -> 2, GT pollution -> 3
"""

import unittest

from traceability.cli_exit import (
    EXIT_VERIFIED,
    EXIT_FAILED,
    EXIT_REVIEW_REQUIRED,
    EXIT_GT_POLLUTION,
    status_to_exit,
)


class CliExitCodeConstantTest(unittest.TestCase):
    """退出码常量必须与计划 §8.6 完全一致。"""

    def test_constants_match_spec(self):
        self.assertEqual(EXIT_VERIFIED, 0)
        self.assertEqual(EXIT_FAILED, 1)
        self.assertEqual(EXIT_REVIEW_REQUIRED, 2)
        self.assertEqual(EXIT_GT_POLLUTION, 3)

    def test_status_to_exit_verified(self):
        self.assertEqual(status_to_exit("verified"), 0)

    def test_status_to_exit_failed(self):
        self.assertEqual(status_to_exit("failed"), 1)

    def test_status_to_exit_review_required(self):
        self.assertEqual(status_to_exit("review_required"), 2)

    def test_status_to_exit_unknown_fail_closed(self):
        # 未知 / 缺失状态一律按 failed（1），不静默当作 verified
        self.assertEqual(status_to_exit(""), 1)
        self.assertEqual(status_to_exit(None), 1)
        self.assertEqual(status_to_exit("bogus"), 1)

    def test_three_states_distinct(self):
        self.assertNotEqual(status_to_exit("verified"), status_to_exit("failed"))
        self.assertNotEqual(status_to_exit("review_required"), status_to_exit("failed"))
        self.assertNotEqual(status_to_exit("verified"), status_to_exit("review_required"))


if __name__ == "__main__":
    unittest.main()
