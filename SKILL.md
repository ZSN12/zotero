---
name: engineering-traceability
description: >
  把工程图纸（扫描图、PDF、DWG、DXF）转换为可追溯、可验证、可变更管理的
  工程上下文。适用于 P&ID、结构图、电气单线图等需要「来源追溯 + 规则验证
  + 变更传播」的场景。不要直接给出"看起来对"的答案，而是产出带来源和状态
  的结构化对象。
version: 0.1.0
---

# Engineering Traceability Skill

## 核心理念

> 从一张图，到可供 AI 使用的工程上下文。

工程制图的关键不是「画得像」，而是**每个对象都能回答四个问题**：

1. **来自哪张图？** → `source.reference` + `source.detail`（文件、版本、原始位置）
2. **实测还是猜的？** → `Dimension.origin`（measured / assumed / derived / placeholder）
3. **哪些规则验证过？** → `Rule.status` + `Connection.validation_status`
4. **改了之后哪些作废？** → 依赖 DAG + `staleness`（current / stale）

## 三阶段工作流

### 阶段 1：DRAWING INTAKE（多源图纸接入）

接收扫描图、PDF、DWG、DXF 等存量资料，为每一份资料建立 `SourceRef`：

```json
{
  "source_type": "drawing",
  "reference": "P&ID-102-Sheet3",
  "detail": "坐标 (320, 1400)",
  "confidence": 0.92,
  "extracted_by": "drawing-intake-v0.1"
}
```

**保留文件、版本与原始位置**。永远不要丢弃来源信息，哪怕置信度很低。

### 阶段 2：ENGINEERING COMPILATION（工程信息编译）

从图纸读取三类对象：

- `Component` 构件：泵、阀、管道、设备……
- `Dimension` 尺寸：**必须标注 origin**（实测/假设/派生/占位）
- `Connection` 连接：两端构件 + 待验证的 `rule_ids`

与物料表（BOM）、设备清单等工程资料**交叉核验**，把冲突记录为待验证项，
而不是悄悄改掉。

### 阶段 3：VERIFIED DELIVERY（可信结果交付）

由**工程 Agent Harness** 编排专业 Skills、工具与验证流程：

1. 对每条规则执行验证，写回 `Rule.status`（passed / failed）
2. 验证通过的对象恢复 `current`
3. 输出可进入 CAD、PLM、数字孪生和 AI 系统的工程上下文

## 使用本项目的 Python 引擎

```bash
# 校验引用完整性
python -m traceability.cli validate examples/pipe_network.json

# 查看追溯报告（尺寸来源、连接验证、失效清单）
python -m traceability.cli report examples/pipe_network.json

# 改动某个节点，自动作废下游
python -m traceability.cli invalidate examples/pipe_network.json --node d_pipe_od

# 验证规则，恢复 current
python -m traceability.cli verify examples/pipe_network.json --rule r_pressure_rating
```

## AI 工作时的硬性要求

0. **MLLM 铁塔输出硬约束**：只允许 `tower_bar` / `tower_node` / `drawing_view`
   三种 kind；`tower_bar` 必须给 `bar_id` 与 `from_node`/`to_node`，
   `tower_node` 必须给 `node_id`；坐标只认 `x_px/y_px` 或 `x/y/z`，
   缺坐标写 null + placeholder。非法 kind 按策略 A 丢弃该条并记
   `parse_warnings`，不整批拒。详见 `traceability/intake/mllm_tower_prompt.py`。
1. **禁止凭空编造工程值**：每个 `Dimension` 必须带 `origin` 和 `source`。
2. **禁止悄悄改数据**：交叉核验发现冲突 → 新建 pending 项，不覆盖原值。
3. **改动必须传播**：改了任何节点，调用 `invalidate` 标记下游 stale。
4. **交付前必须验证**：所有 pending 的规则/连接都要走验证流程才能标 passed。
5. **置信度分级**：`confidence < 0.7` 的对象在报告中要醒目标注「低置信度，需人工复核」。

## 数据模型速查

| 对象 | 关键字段 | 回答的问题 |
|---|---|---|
| Component | id, kind, source | 这是什么？来自哪张图？ |
| Dimension | value, unit, origin | 数值多少？实测还是猜的？ |
| Connection | from, to, rule_ids, validation_status | 谁连谁？验证过吗？ |
| Rule | status, message | 这条规则过了没有？ |
| dependencies | node -> upstreams | 改了它会作废谁？ |
| staleness | current / stale | 现在还有效吗？ |
