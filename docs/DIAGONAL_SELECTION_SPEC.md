# 06 段斜材解释择优规范（P1.1 / Phase 0）

> 锁定 `select_interpretations()` 的语义，禁止静默吞掉 TP。  
> 算法**不得**使用 GT；本文档中的 GT 数字仅用于验收对照。

## 1. 问题背景

06 段 front 视图斜线是**绘图惯例投影**（半交叉 / 中途截断 / full-cross），不是 3D 结构节点连线。  
`build_interpretations()` 把证据线聚合成 fan/twist **解释对**，每对生成 8 根 3D 斜材。

P0 回归：`score < 4000` 全生成 → 11 个 fan 候选无竞争 → 88 杆 / 28 FP。  
P1.1 引入 `select_interpretations()` 做冲突图择优。

## 2. 输入 / 输出

**输入**

- `interps`：`build_interpretations` 产出的解释对列表（含 `kind`, `z_lo`, `z_hi`, `score`, `evidence`）
- `panel_levels`：平台层 z 列表（仅用于文档/审计；**节拍单位不再取自 panel 层差**）

**输出**

- `(kept, audit)`：`kept` 为选中解释；`audit` 必含：
  - `kept`：保留数量
  - `rejected[]`：每条 `{kind, z_lo, z_hi, score, reason, ...}`
  - `beat_unit`：自校准节拍 d（或 `null` 表示跳过节拍筛）
  - `rules`：当前规则参数快照

**拒绝必须显式记录**（P0.5）：不许静默删除解释。

## 3. 筛选规则（按顺序）

### 3.1 跨度节拍 `span_off_grid`

**节拍单位 d（自校准，非 panel 层差）**

```
d = median(fan 候选跨度) / 3
```

- 仅当 fan 候选 **≥ 4** 个时启用；否则跳过节拍筛（`audit.note` 说明原因）
- JC1 真结构跨层 fan 跨度 ∈ {2d, 3d, 4d}，d≈1000
- panel_levels 中位层差常为 2000（粗平台位），**禁止**用作 fan 节拍网格

**接受条件**

```
|span − k·d| ≤ beat_tol_mm   （k ∈ {2, 3, 4}，默认 beat_tol_mm=450）
```

- `twist` 解释不受节拍规则影响

### 3.2 同高度冗余 `duplicate_h`

同一螺旋高度 `h = z_lo` 最多保留 **2** 个 fan（按 `score` 升序）。  
真结构存在跳层 fan（如 h=12000 同时扇 14000 与 16000）。

超出部分标记 `reason=duplicate_h`。

### 3.3 面板交叉保险 `panel_crossing`

按 **h 升序**扫描 fan 解释，维护 `max_P = max(已保留 fan 的 z_hi)`：

- 若当前 fan 的 `z_hi < max_P − ε` → 拒（h 更大却扇向更低平台）
- **保留先到者**，拒后到者（非按 score）

示例：`(12000→16000)` 与 `(13000→14000)` 并存 → 保留前者，拒后者。

## 4. selection_mode（A/B 对照）

| mode | 行为 |
|------|------|
| `none` |  baseline：仅 `score < 4000`，不调用 `select_interpretations` |
| `p11` | 默认：本节全部规则 |
| `relaxed` | `beat_tol_mm=650`（其它同 p11） |

入口：`build_interpretations(..., selection_mode=...)` /  
`reconstruct_diagonal_topology(..., selection_mode=...)`

## 5. 验收指标（06 段，canonical_assisted，tol=500）

| 指标 | baseline (none) | P1.1 (p11) 目标 |
|------|-----------------|-----------------|
| 生成杆 | ~88 | ~72 |
| dtd 独占池 TP | ~60 | ≥55 |
| dtd 独占池 FP | ~28 | ≤15 |
| full 口径 TP | 279（整体） | ≥276（−3 容忍） |

运行对照：

```bash
python3 scripts/ab_diagonal_p11.py
python3 -m pytest tests/test_diagonal_topology.py -q
```

## 6. 已知限制（Phase 0 可接受）

- P=16000 仍可能保留 5 个 fan vs GT 3 个（mid-edge 度数偏高，但经容差仍命中 GT，不产生 FP）
- 节拍规律来自 JC1 development 经验；盲测 ZC1 需 per-sheet 自校准（见 UNIMPLEMENTED_PLAN P4）
- `twist_pairs=0`（06 front 无 FULL）→ **P1.2 已落地**：多面 twist 收集，待全管线复验

## 7. 变更记录

| 日期 | 变更 |
|------|------|
| 2026-08-31 | Phase 0：节拍单位从 panel 中位差改为 fan 跨度自校准；修复 panel_crossing 断言方向 |
| 2026-08-31 | P1.2：`collect_twist_candidates` 多面 (f/l/r) + 异号 MID 截断；yflip depth diagonal 从 L/R 面触发 |

## 8. 后续批次（P1.3–P4）

| 批次 | 落地 |
|------|------|
| P1.3 | `infer_z_window_from_candidates` + 多分册 `reconstruct_diagonal_sheets` |
| P1.4 | `diagonal_topology_sheet_config`（05/06/07 独立参数） |
| P2.1 | `leg_chain_builder.build_leg_chains` |
| P3.2 | `diaphragm_max_z_mm` |
| P4 | `profiles/frozen_jc1_development.json` + `run_frozen_eval.py` |
| Phase 1 eval | `scripts/eval_a2_profiles.py` |