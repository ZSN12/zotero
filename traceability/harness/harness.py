"""Agent Harness 编排器。

职责：
    1. 收集待验证的 Rule / Connection
    2. 为每条规则找到对应验证器（validator）
    3. 执行验证，写回 status 与 message
    4. 验证通过的节点恢复 current
    5. 输出验证摘要，供后续交付（CAD/PLM/数字孪生）使用

验证器接口：
    validator(model, target_id) -> ValidationResult
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..model import EngineeringModel, Staleness, ValidationStatus


@dataclass
class ValidationResult:
    """一次验证的结果。"""
    target_id: str
    status: ValidationStatus
    message: str
    validator: str


ValidatorFn = Callable[[EngineeringModel, str], Optional[ValidationResult]]


class AgentHarness:
    """可插拔验证器编排器。"""

    def __init__(self, validators: dict[str, ValidatorFn] | None = None):
        # 键：规则 ID；值：验证函数
        self.validators: dict[str, ValidatorFn] = validators or {}

    def register(self, rule_id: str, fn: ValidatorFn) -> "AgentHarness":
        self.validators[rule_id] = fn
        return self

    def run(self, model: EngineeringModel, rule_ids: list[str] | None = None) -> list[ValidationResult]:
        """执行验证。不指定 rule_ids 则验证所有规则。"""
        targets = rule_ids or list(model.rules.keys())
        results: list[ValidationResult] = []

        for rid in targets:
            if rid not in model.rules:
                results.append(ValidationResult(rid, ValidationStatus.FAILED,
                                                "规则不存在", "harness"))
                continue

            fn = self.validators.get(rid)
            if fn is None:
                # 没有验证器：保持 pending，绝不擅自通过
                results.append(ValidationResult(
                    rid, ValidationStatus.PENDING,
                    "无可用验证器，需人工或 AI Skill 验证",
                    "harness",
                ))
                model.rules[rid].status = ValidationStatus.PENDING
                model.rules[rid].message = "无可用验证器"
                continue

            result = fn(model, rid) or ValidationResult(
                rid, ValidationStatus.PENDING, "验证器未返回结果", "harness"
            )
            model.rules[rid].status = result.status
            model.rules[rid].message = result.message
            results.append(result)

        # 验证通过的规则恢复 current
        passed = {r.target_id for r in results if r.status == ValidationStatus.PASSED}
        model.refresh(passed)

        # 规则通过后，引用它的连接也视为验证通过（若该连接所有规则都过了）
        self._propagate_rule_pass(model)

        return results

    @staticmethod
    def _propagate_rule_pass(model: EngineeringModel) -> None:
        for conn in model.connections.values():
            if not conn.rule_ids:
                continue
            statuses = {model.rules[r].status for r in conn.rule_ids if r in model.rules}
            if statuses and statuses == {ValidationStatus.PASSED}:
                conn.validation_status = ValidationStatus.PASSED
                model.refresh({conn.id})
            elif ValidationStatus.FAILED in statuses:
                conn.validation_status = ValidationStatus.FAILED


def run_harness(model: EngineeringModel, rule_ids: list[str] | None = None,
                validators: dict[str, ValidatorFn] | None = None) -> list[ValidationResult]:
    """便捷函数：用内置 + 铁塔验证器跑一次 Harness。"""
    from .validators import builtin_validators
    from .tower_validators import tower_validators, connection_validators_for_model

    merged = dict(builtin_validators)
    merged.update(tower_validators)
    merged.update(connection_validators_for_model(model))
    if validators:
        merged.update(validators)
    return AgentHarness(merged).run(model, rule_ids)


def summarize(results: list[ValidationResult]) -> str:
    """生成验证摘要（交付报告用）。"""
    lines = ["验证摘要："]
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
        lines.append(f"  - [{r.status.value}] {r.target_id}: {r.message} (by {r.validator})")
    lines.insert(1, f"  结果分布：{by_status}")
    return "\n".join(lines)
