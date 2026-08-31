# PRODUCTION_REGRESSION_ANALYSIS.md

> 2026-08-31 晚间回归分析（基于本地 review-latest/main 集成分支代码，
> commit 9044ad2+，含 6cee418/976e5b9 两修复）。
> 用户报告的回归：production pure 54→10 TP，full 241→79 TP，horiz_x 162→13。

## 一、结论摘要（按归因贡献排序）

production full 79 TP vs canonical 基线 241 TP 的 **162 TP 差距**分解：

| # | 回归因子 | 贡献 | 证据（消融实验，均绑定 commit） |
|---|---|---|---|
| 1 | **GT 平台层关闭**（panel_level_source=dxf） | **−81 TP** | 消融C：全关 120 → 开GT层 201 |
| 2 | **centerline keep_drop 几何过滤器** | **−41 TP** | 消融D：有过滤 79 → 无过滤 120 |
| 3 | **GT 半宽关闭**（use_gt_half_width=false） | **−40 TP** | 消融C(201) vs A-旧(241)，剩余差 |

三个因子几乎独立可加（120+81+40 ≈ 241 ✓）。

### 因子 1：GT 平台层关闭 → 横隔层位偏移（最大单因子）

- production 的 `derive_panel_levels_detailed` 推导层：7400/8400/9200/10400/11600/12600/13200/14400/16800/18200/19300/20500/…
- GT canonical 层：6500/8500/11500/14000/16000/19000/20883/22800/24000/30024/…
- 偏移量 ±400~1100mm → 横隔（每层 8 根）端点误差超 500 容差 → horiz_x 大量 FN
- 横隔层位是「节点 z 聚类中位数」，DXF 提取噪声（±100~600mm）直接传导为层位偏移

### 因子 2：centerline keep_drop 过滤器误杀真实杆件（P2.4 回归）

- 05 分册单册实测：426 杆 → 136 杆（删 290：too_short=232、dim_like=58）
- **too_short 阈值误杀真实短斜材**——GT 中 57~917mm 短杆真实存在（中位 917mm）
- pure 口径实测伤害：TP 10→26（+16）、FP 89→112
- **过滤器是净伤害**：全链路 full TP 79→120、FP 496→485（FP 还降了）
- 注意：canonical 18:22 跑批时 overlay 尚无 keep_drop 键（20:04 才加入），
  用户看到的 "canonical 241 vs production 79" 实为「无过滤 vs 有过滤+全关」的
  复合对比，不是纯 profile 差异

### 因子 3：GT 半宽关闭 → hw 拟合偏差（DXF 锥线拟合）

- 拟合 hw vs GT hw 实测偏差：中位 586mm、max 1026mm（402 个拓扑杆角点采样）
- hw 偏差 → 节点 y 坐标漂移 + 斜材端点吸附偏差 → front 匹配受损
- diaphragm_depth_filter 的 endpoint_hw_mismatch 删了 252 根横隔（hw 偏差所致，
  删除本身部分合理——那些横隔确实建错了位置）

## 二、完整消融矩阵（本地代码 commit 976e5b9 = 9044ad2+ 两修复）

全部独立输出目录 + `_overlay_sel_*.json` 快照核验（B 为用户跑批，eval_binding 9044ad2）：

| 实验 | 层位 | 过滤器 | selection | pure n/TP | full TP | horiz_x | topology |
|---|---|---|---|---|---|---|---|
| G | GT | **开** | p11 | 96/10 | 168 | 37 | gen 192 |
| E | GT | 关 | **none** | 147/31 | **225** | 47 | gen 312, fan 35, 拒 0 |
| C | GT | 关 | p11 | 138/26 | 201 | 47 | gen 224, fan 24, 拒 11 |
| D | DXF | 关 | p11 | 138/26 | 120 | 35 | gen 272, fan 30, 拒 28 |
| B | DXF | 开 | p11 | 99/10 | 79 | 13 | gen 304, fan 35, 拒 38 |

**干净配对结论**（同代码、同 overlay 其余键）：

1. **C vs E（selection: p11 vs none，其余全同）**：201 → 225，
   **p11 择优净损失 −24 TP**。p11 拒掉 11 个 fan 解释（span_off_grid 7 +
   panel_crossing 4），少生成 88 根 → 用户任务 3 的怀疑在当前代码上**成立**
2. **E vs G（过滤器开关，GT 层 + none/p11 混合）**：225 → 168，
   过滤器损失约 −57 TP（含 selection 差异的 −24，纯过滤约 −33）
3. **E vs D（GT 层 vs DXF 层，无过滤）**：225 → 120，
   **GT 层位关闭损失 −105 TP（最大单因子）**——DXF 推导层偏移
   ±400~1100mm 致横隔错位（horiz_x 47→35）
4. **B vs D（过滤器开关，DXF 层 + p11）**：79 → 120，
   过滤器损失 −41 TP（pure 10→26）

三个因子叠加（105+33+24 ≈ 146 ≈ 225−79 ✓ 自洽）。

## 三、06 择优策略现状（selection_mode）

- 本地代码已实现 `selection_mode`: none / p11 / relaxed（p11=跨度节拍自校准
  median/3 + h序交叉保险，即 HANDOFF_P1_DIAGONAL.md 的方案，9e3dc3e 落地）
- canonical 06-only p11：generated 24（fan 3）——从 88 压缩到 24
- production 多分册（05/06/07）p11：generated 304（fan 35 + twist 3）
- **canonical p11 241 vs selnone 225**：p11 在 canonical 上是正贡献（+16 TP）
  ——用户担心的「24 根择误杀真实 TP」在当前代码上不成立；
  24 根是 canonical 06-only 的数字，production 多分册生成 304 根
- 但注意 E（selnone+无过滤+canonical hw=false overlay）horiz_x=47 远低于
  A-旧 162：**overlay 的 use_gt_half_width 已是 false**——A-旧基线的 GT hw
  配置已不可复现（并行线程改写了 overlay），A-旧数字仅作参考

## 四、建议行动（按用户「先稳定再提升」框架）

1. **立即**：`mllm_keep_drop_sheets` 从 overlay 移除或默认 false——
   实测净伤害（−41 TP），违背「pure TP 不下降超过 2」保留规则
2. **短期**：production 横隔层位改进——derive_panel_levels 的聚类证据
   加权（水平杆证据优先）、或对 cluster 中位数做横隔层位吸附
   （DXF 证据层位 → 最近的整百/整千栅格）
3. **短期**：hw 拟合改进——Theil-Sen 锥线拟合（half_width_taper=true
   已在代码中，overlay 未开）优先于分段单调
4. **中期**：A-旧基线不可复现问题——overlay 关键旗标应写入 eval_binding
   （gt_levels/hw/keep_drop/selection_mode），目前只有 commit SHA 不够
5. geometry gate（悬空节点 10>4）：未在本次分析范围，另行处理

## 五、环境注意事项（并行线程风险）

- 工作区存在并行线程（另一模型）同时跑批/改 overlay/切分支——
  bash-16 已被污染作废，共享 overlay 文件在实验期间被改写两次
- 跑批核验手段：output_dir 的 `_overlay_sel_*.json` 快照 + eval_binding.commit
  + model.json 的 drawing_file.properties（keep_drop 过滤会留 centerline_geom_filter 痕迹）
- production profile 的 overlay 快照写到固定目录 out/35A1-JC1-production/
  （不受 --out-dir 影响）——runner 的小 bug，待修

---

# 附录 B：2026-08-31 第二轮收敛（层位实验 + FP 归因 + 后处理闭环）

## B.1 本轮改动与最终状态

| 改动 | 效果 | 决定 |
|---|---|---|
| DXF 模式移除横隔 z_cap（`diaphragm_z_cap_enabled=False`） | full 198→206，horiz_x 83→91（+8） | **保留**（塔头 6 个真实平台层恢复） |
| `derive_panel_levels_v2`（断点锚定） | full 198→140（图碎裂，断点仅 1 个） | **拒绝**（实验代码保留，默认 v1） |
| 簇内密度谷分割（σ=200/vr=0.4） | 层 25→27（30700/32700 恢复）但 horiz_x 91→75 | **回滚**（详见 B.3） |
| `run_diff` 显式传 `--old/--new/--out-dir` | diff.glb 落在各自 out_dir，postprocess 4/4 | **保留** |

冻结基线见 `out/baselines-frozen/BASELINE_MANIFEST.json`：
- canonical_assisted：full 336 / horiz_x 158 / gate ok / postprocess ok
- production_dxf：full 206 / horiz_x 91 / gate ok / postprocess ok

## B.2 生产 horiz_x 117 FN 的真实构成（修正此前误判）

match_provenance 的 `matched=None` 条目是「各口径并列记录」，此前按
`not matched` 过滤会把 full 口径已匹配的层误判为 FN。用
`matched_at_default` 重算后（2D front 投影、端点距离和 ≤500）：

| z | FN | 根因 |
|---|---|---|
| 11500 | 16 | 推导层缺失（12400 簇，Δ900） |
| 14000 | 16 | 层位偏 400（14400，和≈800>500） |
| 19000 | 16 | 层位偏 300（19300，双峰簇，和≈600>500） |
| 20700/21000 | 4+16 | 20883 层：hw 交叉点错位 + GT 重复条目 |
| 22800 | 10 | GT 16 半跨 vs 模型 8 可用（重复条目结构性缺口） |
| 30000 | 23 | **结构性**：横担平台梁 ±2200 跨度，横隔只覆塔身 hw（canonical 0/25 同样 FN） |
| 32700 | 4 | 同上（横担区平台） |
| 30800/33500/34200/36600 | 2+8+2+8 | hw 拟合偏差（29800 层左右不对称 675/886） |

关键结论：**30000 层 23 FN 在 canonical（GT 层位）下也是 0/25**——
横隔生成器无法表达横担平台梁布局，这不是生产层位缺口，是结构性 FN。
生产与 canonical 的真实 horiz_x 差距 ≈ 67 = 层位 z 偏差 48 + 顶部 hw ~15 + 缺层 4。

## B.3 谷分割实验回滚详情

簇内 KDE 谷分割（σ=200/vr=0.4/合并 1000/子簇≥4 杆）确实从
[30400~32800] 宽簇分出 30700（Δ100）+32700（Δ0）双平台层，但：
- 32700 横隔 hw 与 GT 层几何不匹配（4 FN 依旧，结构性）
- [13800~15500] 簇同时切出 15400 噪声子层（6 杆证据过阈），
  连锁破坏 16000 层横隔（16 TP→FN，机制待查：腿细分/角点候选串扰）
- 净退化 horiz_x 91→75 → 回滚

若未来重启，需先解决：噪声子层的证据量判据（17700 层 bars=57 与
真层不可分）+ 子层对相邻层横隔的干扰隔离。

## B.4 Phase 2 归因：reconstructed 845 FP（2D front/full/tol500）

| 来源 | n | TP | FP | P |
|---|---:|---:|---:|---:|
| diagonal_topology_reconstructed | 496 | 91 | 405 | 18.3% |
| diaphragm_reconstructed | 484 | 82 | 402 | 16.9% |
| dxf_geom | 131 | 22 | 109 | 16.8% |
| panel_subdivision | 20 | 5 | 15 | 25.0% |
| panel_cross_reconstructed | 13 | 0 | 13 | 0% |
| collinear_stitch | 11 | 1 | 10 | 9.1% |
| derived_parametric_base | 7 | 5 | 2 | 71.4% |

横隔 402 FP 细分：
- **噪声层横隔 242**（12 个非 GT 层 × 22 杆：7600/9500/10700/12400/
  13200/14400/17700/19300/25400/26300/27300/28800）
- GT 层附近几何/竞争失败 ~84（29800:20、31000:22、33500:20 等）
- GT 层重复段 ~76（如 22800 层模型 20+ 杆 vs GT 16 杆）

拓扑重建 405 FP 按区段：塔身中段(06) 189、塔脚(05) 112、上段(07) 104、塔头(04) 0。

下一步优先级（按用户计划）：06 selection_mode A/B → 拓扑 FP 收敛 →
横隔层位置信度（区分 19300 噪声 vs 20700 真层）→ 主腿链。
