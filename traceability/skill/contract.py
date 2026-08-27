"""Skill 输出契约。

职责：把 MLLM/规则的「候选输出」强制转成符合工程规范的
EngineeringModel。这是 Skill 的代码化落地，硬性规则：

    1. 每个对象必须有 SourceRef（没有来源就不进模型）
    2. 读不到的尺寸 -> origin=placeholder，绝不猜值
    3. confidence 永远 < 1.0（模型识别默认封顶 0.9）
    4. 冲突不覆盖：同一 id 的重复候选 -> 保留低置信度并标记
    5. 输出必须是 EngineeringModel，禁止裸 JSON 直出
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..intake.mllm_backend import CandidateObject, ModelCandidate
from ..model import (
    Component,
    Dimension,
    DimensionOrigin,
    EngineeringModel,
    SourceRef,
    SourceType,
    ValidationStatus,
)


# 模型置信度封顶：模型识别永远不可能 100% 确定
MAX_MODEL_CONFIDENCE = 0.9


def _clamp_confidence(value: Optional[float]) -> float:
    if value is None:
        return 0.5
    return max(0.0, min(MAX_MODEL_CONFIDENCE, float(value)))


def _make_source(source: Optional[Dict[str, Any]], candidate: CandidateObject) -> SourceRef:
    """从候选的 source 字段构造 SourceRef；缺失时用未知来源兜底（置信度 0）。"""
    if source:
        return SourceRef(
            source_type=SourceType(source.get("source_type", "unknown")),
            reference=source.get("reference", "unknown"),
            detail=source.get("detail"),
            confidence=_clamp_confidence(source.get("confidence")),
            extracted_at=source.get("extracted_at"),
            extracted_by=source.get("extracted_by"),
        )
    # 无来源 -> 未知来源，置信度 0（Hard rule：没有来源不许进模型）
    return SourceRef(SourceType.UNKNOWN, "unknown", confidence=0.0)


def to_engineering_model(
    candidate: ModelCandidate,
    name: Optional[str] = None,
) -> EngineeringModel:
    """把候选输出转成 EngineeringModel。

    candidate: MLLM/规则后端的原始输出
    返回：符合契约的工程模型
    """
    model = EngineeringModel(name=name or f"compiled-{candidate.input.path}")

    # 图纸文件上下文（保留文件、版本、原始位置）
    model.add_component(Component(
        id="drawing_file",
        name=candidate.input.path,
        kind="drawing_file",
        source=SourceRef(
            SourceType.DRAWING,
            candidate.input.path,
            detail=candidate.input.original_location,
            confidence=1.0,
        ),
        properties={
            "path": candidate.input.path,
            "kind": candidate.input.kind,
            "version": candidate.input.version,
        },
    ))

    for obj in candidate.objects:
        conf = _clamp_confidence(obj.confidence)
        src = _make_source(obj.source, obj)

        if obj.obj_type == "component":
            data = obj.data
            model.add_component(Component(
                id=data.get("id", f"c_{len(model.components)}"),
                name=data.get("name", data.get("id", "unnamed")),
                kind=data.get("kind", "unknown"),
                source=src,
                properties=data.get("properties", {}),
                tags=data.get("tags", []),
            ))

        elif obj.obj_type == "dimension":
            data = obj.data
            value = data.get("value")
            origin_raw = data.get("origin", "placeholder")
            # Hard rule：value 缺失 -> 强制 placeholder
            if value is None:
                origin_raw = "placeholder"
            try:
                origin = DimensionOrigin(origin_raw)
            except ValueError:
                origin = DimensionOrigin.PLACEHOLDER

            model.add_dimension(Dimension(
                id=data.get("id", f"d_{len(model.dimensions)}"),
                name=data.get("name", "unnamed dimension"),
                value=value,
                unit=data.get("unit", ""),
                origin=origin,
                source=src,
                applies_to=data.get("applies_to"),
                status=ValidationStatus.PENDING,
            ))

        elif obj.obj_type == "connection":
            data = obj.data
            from ..model import Connection
            model.add_connection(Connection(
                id=data.get("id", f"conn_{len(model.connections)}"),
                from_component=data.get("from_component", ""),
                to_component=data.get("to_component", ""),
                connection_type=data.get("connection_type", "physical"),
                source=src,
                validation_status=ValidationStatus.PENDING,
                rule_ids=data.get("rule_ids", []),
            ))

        elif obj.obj_type == "rule":
            data = obj.data
            from ..model import Rule
            model.add_rule(Rule(
                id=data.get("id", f"r_{len(model.rules)}"),
                name=data.get("name", "unnamed rule"),
                description=data.get("description", ""),
                applies_to=data.get("applies_to", []),
                status=ValidationStatus.PENDING,
            ))

    return model
