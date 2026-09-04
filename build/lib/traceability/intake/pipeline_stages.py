"""塔身 Agent 管线规范 stage id 常量与 detail 字段约定（P1 统一接口）。

扫描管线（tower_agent_pipeline）与 DXF hybrid 管线（hybrid_dxf_agent）原本
各自硬编码 stage id，导致 a2_vector（hybrid）vs a2_geom（扫描）这类不一致。
这里统一为规范常量，两条管线共用，消除魔法字符串、便于 steps.json 的
下游消费（批跑汇总、测试断言、Harness 审计）。

规范 stage id（按执行顺序）：
    a0_layout   版面/视图切分
    a1_labels   件号读取（MLLM 或 OCR）
    a2_geom     几何检测（MLLM / ezdxf / hough）
    a3_link     件号↔杆件关联
    a4_harness  编译验证

每个 stage 的 detail 字段约定见 STAGE_DETAIL_KEYS。
"""

from __future__ import annotations

from typing import Dict, Tuple

# 规范 stage id。
STAGE_LAYOUT = "a0_layout"
STAGE_LABELS = "a1_labels"
STAGE_LABELS_OCR_FALLBACK = "a1_labels_ocr_fallback"
STAGE_GEOMETRY = "a2_geom"          # 统一：hybrid 旧名 a2_vector 已并入
STAGE_LINK = "a3_link"
STAGE_HARNESS = "a4_harness"

# 历史别名 → 规范 id（向后兼容旧 steps.json）。
STAGE_ALIASES: Dict[str, str] = {
    "a2_vector": STAGE_GEOMETRY,
}

# 各 stage 的 detail 关键字段约定（下游消费 steps.json 时按此读）。
# 值为 (关键字段, ...)，仅文档性质；消费方仍用 .get() 容错读取。
STAGE_DETAIL_KEYS: Dict[str, Tuple[str, ...]] = {
    STAGE_LAYOUT: ("views", "whole_sheet", "method"),
    STAGE_LABELS: ("labels", "mllm_labels", "dxf_text_labels", "method"),
    STAGE_GEOMETRY: ("method", "bars", "nodes", "ezdxf_bars", "mllm_geom_bars"),
    STAGE_LINK: ("labels", "bars", "matched", "association_rate"),
    STAGE_HARNESS: ("rules", "summary"),
}


def canonical_stage_id(stage_id: str) -> str:
    """把历史别名规范化为规范 stage id（未知 id 原样返回）。"""
    return STAGE_ALIASES.get(stage_id, stage_id)


# 规范执行顺序（用于校验/审计 steps.json 的完整性）。
STAGE_ORDER: Tuple[str, ...] = (
    STAGE_LAYOUT,
    STAGE_LABELS,
    STAGE_GEOMETRY,
    STAGE_LINK,
    STAGE_HARNESS,
)
