# 口径 Profile（P0.4，2026-08-31）

> 背景：生产 overlay（`examples/external/guowang_35A1/layer_overlay.json`）默认
> `use_gt_platform_levels=true`——GT canonical 平台标高（z-only 注入）会传播到
> 横隔层位、主腿节间细分、panel-cross 重建与 06 斜材拓扑。当前 full A2 的
> 279 TP 中 223 个（80%）来自该辅助（level-assisted 口径）。该配置适合研究
> 对照，不适合真实部署与盲测汇报。

## 两个 profile

| profile | 用途 | 关键开关 | 输出目录 | 汇报口径 |
|---|---|---|---|---|
| `canonical_assisted`（默认） | 研究对照 / 归因分析 | `use_gt_platform_levels=true` | `out/35A1-JC1-full-deliver/` | 仅内部归因，**不得**对外作为纯识别能力 |
| `production_dxf` | 生产真实能力 / 盲测 | `panel_level_source=dxf`、`use_gt_platform_levels=false`、`use_gt_half_width=false` | `out/35A1-JC1-production/` | 可对外（标注 dataset_split） |

## 用法

```bash
# 默认（canonical_assisted，历史行为不变）
python3 scripts/run_35A1_jc1_full.py

# 生产真实能力口径
python3 scripts/run_35A1_jc1_full.py --profile production_dxf
```

## 语义要点

1. **共享 overlay 文件不被修改**：production 覆盖发生在脚本层（写
   `_overlay_production.json` 临时文件），测试与其它调用方不受影响。
2. **输出目录隔离**：production 写 `out/35A1-JC1-production/`，且
   **不同步演示资产**（`web/demo/35A1-JC1/` 只跟 canonical_assisted 主线）。
3. **口径分层标签**：产物中 `level_source=gt_canonical` 的杆件属
   level_assisted 口径；production 产物应全部为
   `dxf_derived` / `recognized` / `reconstructed` / `parametric`。
4. **盲测前置条件**（P4 批次）：ZC1 盲测必须使用 `production_dxf`，
   冻结全部参数，且盲测后不得据结果调参仍称 blind。

## 历史对照（2026-08-31 口径审计，JC1 development）

| 配置 | TP@500 | R(full) | 说明 |
|---|---|---|---|
| canonical_assisted（当前生产默认） | 188（旧审计）/ 279（P1 后） | 17.6% → 26.1% | 含 GT 标高辅助 |
| production_dxf（纯 DXF 层推导） | 114（旧审计） | 10.6% | DXF 推导层膨胀到 25 层 vs GT 15 层，横隔 FP 733 |

（P0.4 落地后应重跑 `--profile production_dxf` 更新此表——旧审计的 114
是 P1 斜材拓扑与 P3 横隔去重之前的数据。）
