"""CLI 退出码统一常量（阶段 8.6）。

权威映射（《Engineering-Trace 完整修复计划》§8.6）：

    verified        -> 0
    failed          -> 1
    review_required -> 2
    GT pollution    -> 3

所有正式入口（run-tower / deliver-tower / deliver-project / evaluate 脚本）
必须引用本模块，禁止各自硬编码数字，避免 CLI 与测试分叉。
"""

from __future__ import annotations

EXIT_VERIFIED = 0
EXIT_FAILED = 1
EXIT_REVIEW_REQUIRED = 2
EXIT_GT_POLLUTION = 3

# 交付/求解结果 status 字符串 -> 退出码
_STATUS_TO_EXIT = {
    "verified": EXIT_VERIFIED,
    "failed": EXIT_FAILED,
    "review_required": EXIT_REVIEW_REQUIRED,
}


def status_to_exit(status: str) -> int:
    """把交付 status 字符串映射为退出码，未知/缺失一律按 failed（1）fail-closed。"""
    return _STATUS_TO_EXIT.get((status or "").strip().lower(), EXIT_FAILED)
