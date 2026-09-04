"""内置验证器集合。

每条验证器都是一个「纯函数」：输入模型 + 规则 ID，输出 ValidationResult。
真实工程中，这些函数应替换为对设计规范的正式检查；这里给出的是
「可执行、可解释」的骨架示例，说明验证器应该长什么样。

重要约定：验证器**只依据数据说话**，数据不足时返回 pending，不得编造。
"""

from __future__ import annotations

from typing import Optional

from ..model import EngineeringModel, ValidationStatus
from .harness import ValidationResult


def _connection_for_rule(model: EngineeringModel, rule_id: str):
    """找到引用该规则的第一条连接。"""
    for conn in model.connections.values():
        if rule_id in conn.rule_ids:
            return conn
    return None


def validate_pressure_rating(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """压力等级匹配：阀门/管道压力等级 >= 设计压力。"""
    conn = _connection_for_rule(model, rule_id)
    if conn is None:
        return ValidationResult(rule_id, ValidationStatus.PENDING, "未找到引用该规则的连接", "pressure-rating")

    # 从构件属性里找设计压力；找不到就 pending，不猜
    values = []
    for cid in (conn.from_component, conn.to_component):
        comp = model.components.get(cid)
        if comp and "design_pressure_bar" in comp.properties:
            values.append(comp.properties["design_pressure_bar"])

    if len(values) < 2:
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "缺少至少两个端点的设计压力数据，待补充", "pressure-rating")
    if min(values) >= max(values):
        return ValidationResult(rule_id, ValidationStatus.PASSED, "两端压力等级匹配", "pressure-rating")
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"压力等级不匹配：{values[0]} vs {values[1]}", "pressure-rating")


def validate_flange_match(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """法兰匹配：连接两端存在法兰且尺寸一致。"""
    conn = _connection_for_rule(model, rule_id)
    if conn is None:
        return ValidationResult(rule_id, ValidationStatus.PENDING, "未找到引用该规则的连接", "flange-match")

    diameters = []
    for cid in (conn.from_component, conn.to_component):
        comp = model.components.get(cid)
        if comp and comp.properties.get("nominal_diameter_mm"):
            diameters.append(comp.properties["nominal_diameter_mm"])

    if len(diameters) < 2:
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "缺少法兰公称直径数据，待补充", "flange-match")
    if diameters[0] == diameters[1]:
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                f"法兰匹配（DN{diameters[0]}）", "flange-match")
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"法兰不匹配：DN{diameters[0]} vs DN{diameters[1]}", "flange-match")


def validate_weld_material(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """焊接材料兼容：连接两端管材一致则视为兼容（简化示例）。"""
    conn = _connection_for_rule(model, rule_id)
    if conn is None:
        return ValidationResult(rule_id, ValidationStatus.PENDING, "未找到引用该规则的连接", "weld-material")

    materials = []
    for cid in (conn.from_component, conn.to_component):
        comp = model.components.get(cid)
        if comp and comp.properties.get("material"):
            materials.append(comp.properties["material"])

    if len(materials) < 2:
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "缺少两端材料数据，待补充", "weld-material")
    if materials[0] == materials[1]:
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                f"材料兼容（{materials[0]}）", "weld-material")
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"材料不兼容：{materials[0]} vs {materials[1]}", "weld-material")


# 规则 ID -> 验证器
builtin_validators = {
    "r_pressure_rating": validate_pressure_rating,
    "r_flange_match": validate_flange_match,
    "r_weld_material": validate_weld_material,
}
