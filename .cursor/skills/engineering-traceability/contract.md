# Skill 输出契约（contract）

代码落地：`traceability/skill/contract.py`

硬性规则：

1. **每个对象必须有 SourceRef**：没有来源就不进模型（confidence=0 兜底）。
2. **读不到的尺寸**：`value=null` 强制 `origin=placeholder`，绝不猜值。
3. **置信度封顶**：模型识别 `confidence` 永远 < 1.0（默认封顶 0.9，扫描图 0.6）。
4. **冲突不覆盖**：同一 id 重复候选保留低置信度并标记。
5. **输出必须是 EngineeringModel**：禁止裸 JSON 直出，必须走 `to_engineering_model()`。

铁塔专用 Prompt + Schema：`traceability/intake/mllm_tower_prompt.py`。
