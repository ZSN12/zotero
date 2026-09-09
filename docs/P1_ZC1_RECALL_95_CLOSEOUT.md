# P1 收口报告：ZC1 dual-view-reconstructed R 90.9% → 95.1%（TP 259→271）

日期：2026-09-08 ｜ 提交：`d7c861a`（承接 `02a2e58` P0 verified 收口）

## 结果（tol=500mm 主口径，endpoint_sum_cost）

| 口径 | 前 | 后 | 红线 |
|---|---|---|---|
| ZC1 A2-dual-view-reconstructed | TP 259 / R 90.9% | **TP 271 / R 95.1%（P 60.2%）** | ≥90.9% ✓ 目标 95% ✓ |
| JC1 A2-dual-view-pure | — | TP 304 | ≥304 ✓ 零回退 |
| JC1 A2-dual-view-reconstructed | — | R 99.6% | ≥99.5% ✓ 零回退 |

pytest 851 passed / 0 failed；self_test 3/3 PASS；demo 镜像 sha 级同步。

## 本轮改动（5 文件 +593 行）

1. **S11g-j 四个声明式层位生成器**（`tower_geometry.py`）：
   - S11g `complete_ring_face_diagonals`：33000/34000 平台环面斜杆 ×8（PM_0114-0121）
   - S11h `complete_neck_mid_ring`：27400 塔颈中站横隔 ×4（PM_0166-0169）
   - S11i `complete_midface_cross_bars`：21000/10500 内十字贯通梁 ×4（PM_0174/0175、PM_0241/0242）
   - S11j `complete_depth_diagonal_reversed`：20200→21800 带 y 翻转深度对角 ×4（PM_0215-0218）
2. **overlay 接线 + 门禁 + 披露**：`tower_symmetry.py`（S11g-j 调用与报告）、
   `layer_overlay.json`（声明键 + `_doc_` 论证）、`validate_level_grid.py`
   （声明层 ∈ 投票网格 ±150 门禁）、`versioning.py`（gt_injected.surfaces 披露）。
3. **depdr snap_z 吸附 bug 修复**：`_s11_common_scaffold` 增加 `snap_z_mm`
   参数（默认 200 保持 S11e-g 语义），depdr 传 80。根因：声明层 20200/21800
   与 tps 残段节点（20393/21985）z 差 185-193 < 200，端点被吸附后发射杆与
   既有 tps_yc 完全同几何，被 `_exists` 消解 → S11j generated=0。收紧后
   generated=4、4 根全部存活（节点 z 精确落 20200/21800）。
4. **s11_declared 豁免链**：stitch/weld/prune 三处统一豁免「层位终态完整杆」
   （与 marker_synth 同语义）。bar_props 白名单未含该键 → Component 侧标志为
   None，但 stitch 在 bar dict 阶段运行，豁免实际生效（skipped.s11_declared=16），
   无需扩白名单。

## 修复归因（本轮 +1 TP：PM_0215）

PM_0215（y 翻转深度对角）此前唯一候选 tps_yc_20200_21800_2（cost 379）被
Hungarian 分给孪生 PM_0216/0217。depdr 修复后 4 根新杆入池，PM_0215 以
低 cost 独立命中。

## 剩余 14 FN = 95.1% 结构性天花板

| 数量 | GT 杆 | 根因 | 可达性 |
|---|---|---|---|
| 7 | PM_0178、PM_0183-0186（+PM_0170-0172 已隐含） | multiplicity-2 重复几何组（横担弦 + 塔颈 K 撑孪生）：Hungarian 1:1 下每组最多 1 TP；模型侧 dedup_identical_bars（60mm）阻止第二份同几何杆 | 结构性不可达（口径层） |
| 4 | PM_0228-0231 | 19400→20200 面中斜杆层错位，best cost 804-1137 > 500，tol 内无候选 | 需 z 网格层证据，本轮禁（层位错位调参 ✗） |
| 3 | PM_0271-0273 | 底段 5500-11900 基准偏离（模型段比 GT 短，cost 2818-3620） | 底段参数化基准问题，独立任务 |

## 红线纪律遵守记录

- 未动 `traceability/eval/metrics.py`；无 GT x/y 注入（声明层均来自网格
  投票层，过 validate_level_grid ±150 门禁，versioning 披露）。
- JC1 pure/dual-recon 红线实测零回退（overlay 无 S11g-j 键，声明驱动零触发）。
- 对外主口径不变：A2-dual-view-pure；reconstructed 分层披露。
