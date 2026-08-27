# NeuBE SR 对标叙事页（P0-6）

> NeuBE SR（Semantic Reconstruction）强调：从图纸到 3D 的每一步都要有
> **证据链**、**语义 IR**、**约束**和**复核门**。本项目在铁塔管线上
> 一一对应落地，不把「看起来对」当交付。

## 1. 证据链（Evidence Chain）↔ SourceRef

| NeuBE SR 要求 | 本项目实现 |
|---|---|
| 每个几何元素可追溯到图纸 | 每个 `Component` / `Dimension` 带 `SourceRef`（reference + detail + confidence） |
| 来源分级 | `SourceType`：drawing / measurement / assumption / derived / vendor |
| 不丢弃低置信度来源 | confidence 如实写低，报告醒目标注，绝不删除 |

示例：

```json
{
  "source_type": "drawing",
  "reference": "examples/tower_110kv.dxf",
  "detail": "handle=2F3, layer=LEG, view=front",
  "confidence": 0.85
}
```

## 2. 语义 IR（Semantic Intermediate Representation）↔ EngineeringModel

| NeuBE SR 要求 | 本项目实现 |
|---|---|
| 结构化对象，而非裸点云 | `Component`（tower_node / tower_bar / bom_row / scan_region） |
| 语义属性显式化 | `properties.bar_id / section / length_mm / from_node / to_node` |
| 尺寸来源分级 | `Dimension.origin`：measured / assumed / derived / placeholder |
| 关系显式化 | `Connection`（physical / logical）+ `dependencies` DAG |

工程模型不是「线段集合」，而是可查询、可变更、可验证的语义对象。

## 3. 约束（Constraints）↔ Rules + 依赖 DAG

| NeuBE SR 要求 | 本项目实现 |
|---|---|
| 拓扑闭合约束 | `r_topology_closed` |
| 长度一致性约束 | `r_bom_length_match`（3D 长度 vs BOM ≤ 3%） |
| 截面一致性约束 | `r_bom_section_match` |
| 坐标齐备约束 | `r_node_fully_solved` |
| 编号唯一约束 | `r_no_duplicate_bar_id` |
| 扫描人工复核约束 | `r_scan_reviewed` |
| 变更传播 | `invalidate` / `staleness`（current / stale） |

约束求解：`traceability/solve/tower_solver.py`（长度约束传播 + 最小二乘）。

## 4. 复核门（Review Gates）↔ pending_review / placeholder / harness

| NeuBE SR 要求 | 本项目实现 |
|---|---|
| 读不到就阻断，不猜 | 缺值 → `placeholder`，`solve_tower` 严格模式拒绝导出 |
| 低置信度进人工复核队列 | 扫描图 `solve_status=pending_review`，confidence ≤ 0.6 |
| 人工确认后才能终版 | `confirm_tower_scan` → `solve_status=verified` → 才可 `export strict` |
| 失败可人工标记 | `run-tower --retry` / `--human-review`，写回 message + 复核标记 |
| 每步状态可审计 | `steps.json`：status / duration / error 逐步骤记录 |

## 5. 端到端证据链示例

```
tower_110kv.dxf
  └─(intake)→ tower_bar M0001 (source=dxf, handle=2F3, conf=0.85)
       └─(cross_check)→ dim_bom_length_M0001 (origin=measured, vendor CSV)
       └─(compile)→ tower_node N01..N85 (origin=derived, 三视图解耦)
       └─(verify)→ r_topology_closed / r_bom_length_match ... [passed]
       └─(solve)→ tower_head.glb（L 型角钢截面，非圆柱近似）
       └─(export)→ model.json + tower.glb + report.md + steps.json + harness_summary.json
```

## 6. Demo 侧栏

`web/index.html` 第 5 栏即本页的摘要版；完整文档即本文件。
