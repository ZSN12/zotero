# 口径纪律详表（angle-tower 领域包）

> 本文是 `SKILL.md` §4 的展开。评测口径是诚实性契约，不是技术细节。

## 1. 五层口径（`traceability/eval/metrics.py` `_CALIBER_SETS`）

| 口径 | 入池条件 | 允许的 geometry_origin 示例 | 对外？ |
|---|---|---|---|
| `pure` | 直接识别（单视图 2D） | `dxf_geom`, `leg_synth`, `collinear_stitch`, `marker_synth` | ✅（A2-dual-view-pure 为主口径） |
| `reconstructed` | 确定性重建 | `diagonal_topology_reconstructed`, `panel_template_completion`, `crossarm_truss_completion`, `terminal_pair_gen`, 镜像杆 | ⚠️ 内部归因 |
| `level_assisted` | z-only 层表辅助 | `diaphragm@gt_levels`, `subdiv@gt_levels` | ❌ 内部归因 |
| `parametric` | 参数化外推 | `derived_parametric_base`（底段裙部） | ❌ 内部归因 |
| `full`（physical） | 以上全部（排除 derived 展示几何） | — | ❌ 调试用 |

## 2. 对外主口径：A2-dual-view-pure

定义：front∪side 双视图直读并集，**仅 recognized 入池**，GT 杆按 3D id 去重。
front 投影退化的杆（y_member）由 side 补、side 退化的（x 向）由 front 补。

呈报格式（示例）：

```
A2-dual-view-pure: TP 214 / P 57.2% / R 20.0%（35A1-JC1，2026-09-03）
```

**禁止事项**：
- 禁止把 dual-union（含 level-assisted 的并集）说成直读能力；
- 禁止只报 R 不报 P（pure 池小，R 容易虚高）；
- 禁止不同 GT 来源等级的塔并列排名（见 §4）。

## 3. GT 注入边界（z-only 纪律）

| 允许（z-only 设计常数） | 禁止 |
|---|---|
| 平台层表 `gt_platform_levels_override` | GT 节点 x/y 坐标 |
| 终止层表 `gt_terminal_levels_override` | GT 杆拓扑连接 |
| 横隔层表 `gt_diaphragm_levels_override` | 按杆 id/坐标反推任何几何 |
| 跨度白名单 `terminal_pair_span_whitelist` | 修改评测器容差/匹配语义 |

所有 z-only 注入面必须在 `version.json` 的 `gt_injected.surfaces` 登记
（`traceability/project/versioning.py` 自动写入）。未登记 = 未披露 = 违规。

## 4. GT 来源等级

| 等级 | 来源 | 并列呈报 |
|---|---|---|
| A | `.mod` + `.NODE` 计算文件直出（35A1-JC1 / 35A2-ZC1 / JC2_mod / JC3） | ✅ 可互比 |
| B | canonical GLB 中心线反提取（35A2-JC2） | ❌ 仅内部回归对照 |

B 级 GT 的 `caveats` 字段列出限制；`evaluate_ground_truth.py` 会在报告头
打印 ⚠️ 警示并写入 `eval_binding.gt_caveats`。

## 5. 基线数字（2026-09-03，commit 系）

| 塔 | A2-dual-view-pure | dual-union (reconstructed) | front 2D 天花板 |
|---|---|---|---|
| 35A1-JC1 | TP 214 / P 57.2% / R 20.0% | 1065 / 99.4% | 80.1%（858/1071） |
| 35A2-ZC1 | TP 9 / P 6.5% / R 3.2% | 216 / 75.8% | 72.6%（207/285） |

天花板 = 投影退化上限（y_member 退化为点 + depth_diag 与 leg 重合损失），
超出部分任何直读算法都不可达——这是口径诚实的一部分，不是借口。

## 6. 变更纪律

1. 动口径相关代码前先跑两道门禁：
   `python3 domains/angle-tower/scripts/self_test.py`
   `python3 domains/angle-tower/scripts/validate_public_ir.py <model.json>`
2. 指标变化写进提交信息（如 "dual-union R 39.3%→75.8%"）。
3. 新增 GT 注入面 → 同步登记 versioning.py 清单 + 本文 §3 表。
4. 新塔接入 → 按 SKILL.md §3 的 overlay 模板配置，禁改引擎代码。
