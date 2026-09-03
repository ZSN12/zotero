---
name: angle-tower
description: >
  输电线路角钢塔（L-angle steel tower）从 DXF 图纸到可验证三维模型的
  领域包。六层契约：drawing → hypothesis → rebuild → semantic-ir →
  validation-gate → complete-tower。每一层只消费上一层的产物，
  每一层都能独立审计。适用于国网 35kV~500kV 单回路/双回路角钢塔
  图册（立面分段册 + 节点详图 + 杆件加工图 + BOM）的重建与追溯。
version: 0.1.0
license: 与主仓一致
---

# Angle-Tower 领域包（六层契约）

> 一个领域包 = 一份 SKILL 契约 + 引擎 + 自检门禁 + 示例。
> 本包消费多册 DXF 图纸，产出**可追溯、可验证、可变更管理**的全塔
> 三维模型（GLB + model.json + 评测报告）。

## 0. 铁律（违反任何一条 = 交付无效）

1. **GT 注入边界**：允许注入 z-only 设计常数（层表/跨度表，overlay 声明，
   version.json `gt_injected.surfaces` 登记）；**严禁注入 GT x/y 坐标或拓扑**。
2. **口径诚实**：对外只报 `A2-dual-view-pure`（直读并集）；
   `reconstructed/level_assisted/parametric` 分层呈报，不得冒充直读。
3. **评测器不可改**：`traceability/eval/metrics.py` 的匹配语义与容差
   判定是门禁，不是可调参数。
4. **每物必有源**：所有 Component/Dimension 带 SourceRef；
   改动必须沿依赖 DAG 传播 staleness。

## 1. 六层契约 ↔ 引擎模块

| 层 | 输入 | 输出 | 引擎模块 | 产物标记 |
|---|---|---|---|---|
| **L1 drawing** | 多册 DXF | 区域化 2D 构件（杆/节点/件号/DIM 标注） | `traceability.intake.tower_dxf` + `tower_views` + `tower_batch` | `geometry_class=recognized`，`SourceRef` 逐实体 |
| **L2 hypothesis** | L1 构件 + 节拍证据 | 结构解释候选（fan/twist/kchain）+ 四态状态机 | `traceability.intake.evidence_layer` + `solve.diagonal_topology` | `kind=hypothesis`（proposed/accepted/rejected/superseded） |
| **L3 rebuild** | L1+L2 + z-only 层表 | 参数化补全（面板链/横隔/终止节间/K-fan） | `traceability.solve.tower_geometry` + `intake.tower_symmetry` | `geometry_origin=panel_template_completion / diaphragm_reconstructed / terminal_pair_gen …` |
| **L4 semantic-ir** | L1~L3 几何 | 语义工程模型（BOM 交叉核验/件号/截面/连接） | `traceability.model.EngineeringModel` + `solve.tower_solver` | schema `schema/engineering_model.json`（含证据层 observation/hypothesis） |
| **L5 validation-gate** | L4 模型 | 规则裁决 + 变更传播 | `traceability.rules.*` + 依赖 DAG `invalidate` | `Rule.status`（passed/failed）+ `staleness` |
| **L6 complete-tower** | 通过 L5 的模型 | GLB / model.json / report / steps.json | `traceability.io` + `project.delivery` | version.json 指纹 + `gt_injected.surfaces` 披露 |

## 2. 快速开始

```bash
# 门禁 1：自检（单测 + 内置示例端到端）
python3 domains/angle-tower/scripts/self_test.py

# 门禁 2：公共 IR 校验（schema + 口径纪律 + 证据层完整性）
python3 domains/angle-tower/scripts/validate_public_ir.py out/35A1-JC1-full-deliver/model.json

# 全塔重建（单塔约 1 分钟）
python3 scripts/run_35A1_jc1_full.py            # 35A1-JC1
python3 scripts/run_35A2_zc1_full.py            # 35A2-ZC1（多塔 overlay 泛化）

# 评测（对外口径 = A2-dual-view-pure）
python3 scripts/eval_a2_profiles.py examples/gt/35A1-JC1_ground_truth.json out/35A1-JC1-full-deliver/model.json
```

## 3. 换一座塔要改什么（多塔泛化纪律）

只改配置，不改代码。以 35A2-ZC1 为例（对照 `examples/external/guowang_35A2_zc1/layer_overlay.json`）：

| 配置项 | 作用 | 纪律 |
|---|---|---|
| `view_regions`（分册） | 每册立面 z 域 | 从图纸实测，不猜 |
| `gt_platform_levels_override` / `gt_terminal_levels_override` / `gt_diaphragm_levels_override` | 本塔 z-only 层表（覆写 JC1 canonical 默认） | **只许 z，不许 x/y**；version.json 自动登记 |
| `terminal_pair_span_whitelist` | 终止节间跨度白名单 | 从本塔 GT 长斜杆端点对聚类推导（≥2 refs） |
| `diagonal_topology_sheets` + `sheet_config` | 假设层（L2）多册声明 | 每册 auto_z_window 自校准 |
| `centerline_extract`（分册） | 缝合容差 | 节拍锚定优先 |

## 4. 口径与呈报（诚实性契约）

| 口径 | 入池条件 | 用途 |
|---|---|---|
| `A2-dual-view-pure` | `recognized` 直读（front∪side 并集） | **对外主口径** |
| `A2-dual-view-reconstructed` | + 确定性重建（镜像） | 内部归因 |
| `level_assisted` / `parametric` | + z-only 层表补全 / 参数化外推 | 内部归因，分层披露 |

当前基线（2026-09-03）：JC1 dual-view-pure TP 220 / P 58.2% / R 20.5%；
ZC1 dual-union（reconstructed）R 75.8%。GT 来源等级：`.mod/.NODE` 直出可
并列呈报；GLB 反提取（如 JC2 canonical）仅限内部回归，见 GT `caveats`。

## 5. 目录

```
domains/angle-tower/
  SKILL.md                    # 本文件（六层契约）
  scripts/self_test.py        # 门禁 1：自检
  scripts/validate_public_ir.py  # 门禁 2：公共 IR 校验
  docs/CALIBER_DISCIPLINE.md  # 口径纪律详表
../../traceability/           # 引擎（L1~L6 实现本体）
../../scripts/run_*.py        # 单塔管线入口
../../examples/gt/            # 各塔 GT（含来源等级 caveats）
```

## 6. 硬性要求（AI 工作时）

1. 动 `traceability/` 任何代码前先跑 `python3 domains/angle-tower/scripts/self_test.py`，动完再跑。
2. 评测指标只从 `scripts/eval_a2_profiles.py` / `evaluate_ground_truth.py` 产出，不手算。
3. 新增 GT 注入面必须同步登记 `project/versioning.py` 的 `_gt_keys`/override 清单，否则视为未披露。
4. 提交信息带上口径实测变化（如 "dual-union R 39.3%→75.8%"），无指标的改动说明为什么不影响指标。
