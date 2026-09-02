# 任务书：35A1-JC1 与 35A2-ZC1 全塔 Pure 召回率突破 60%+ 攻坚计划

## 0. 仓库与运行环境

- **代码仓库**：`/Users/zsn/Documents/zotore/engineering-trace`
- **红线测试门禁（必须保持 100% 全绿）**：
  ```bash
  python3 -m pytest tests/ -q
  # 预期输出：595 passed, 0 failed
  ```
- **JC1 转角塔管线与评测**：
  ```bash
  # 全量流水线（约 1 分钟）：
  python3 scripts/run_35A1_jc1_full.py --out-dir out/35A1-JC1-pure60 --skip-sync

  # 快速标准评测（秒级）：
  python3 scripts/eval_a2_profiles.py examples/gt/35A1-JC1_ground_truth.json out/35A1-JC1-pure60/model.json
  ```
- **ZC1 直线塔管线与评测**：
  ```bash
  # 全量流水线：
  python3 scripts/run_35A2_zc1_full.py --profile canonical_assisted --out-dir out/35A2-ZC1-pure60 --skip-sync

  # 快速标准评测：
  python3 scripts/eval_a2_profiles.py examples/gt/35A2-ZC1_ground_truth.json out/35A2-ZC1-pure60/model.json
  ```

---

## 1. 核心目标与验收指标

| 塔型 | 构件总数 ($N_{\text{GT}}$) | 当前 Pure TP (基准) | 目标 Pure TP (60%+) | 目标 Pure Recall | 必须严守的红线指标 |
|---|---|---|---|---|---|
| **35A1-JC1 (转角塔)** | 1071 根 | 152 TP (14.2%) | **≥ 642 TP** | **≥ 60.0%** | `A2-dual-view-reconstructed` $\ge 95.0\%$ |
| **35A2-ZC1 (直线塔)** | 285 根 | 26 TP (9.1%) | **≥ 171 TP** | **≥ 60.0%** | `A2-dual-view-reconstructed` $\ge 85.0\%$ |

---

## 2. 纪律红线（违背视为作弊，直接判不合格）

1. **Pure 口径证据纪律**：
   - Pure 评测池**只接收**具有直接图纸证据的构件（`geometry_origin` 为 `dxf_geom`, `leg_synth`, `collinear_stitch`, `marker_synth`，且 `geometry_class == "recognized"`）。
   - 禁止将推断构件（`diaphragm_reconstructed`, `parametric_base`）伪造或篡改为 pure。
2. **严禁注入 GT 坐标与拓扑**：
   - 不得在代码中读取 GT 的三维坐标 $(x, y)$ 或构件拓扑连接关系；
   - 仅允许使用图纸设计常数（如 `beam_marker_levels_mm` 标高层位表、`leg_synth_spans_mm` 腿跨分段）。
3. **严禁修改评测器**：
   - 不得篡改 `traceability/eval/metrics.py` 中的端点几何匹配距离计算（匈牙利 500mm 容差门禁）或容差判定逻辑。

---

## 3. 核心攻坚战线一：35A1-JC1 冲刺 60%+（TP ≥ 642）

### 抓手 1：四面确定性水平环梁（Diaphragm Ring）Pure 识别放行（预计 +87 TP）
- **物理事实**：图纸立面识别出的水平梁（`marker_synth` 或 `dxf_geom`）属于正四面截面闭合环梁，其在 Left / Right 面的水平横梁直接对应 GT 的 87 根 `y_member`。
- **代码实施**：
  1. 在 `traceability/intake/tower_symmetry.py:1375-1405` 中，当四面展开遇到 `role in ("HORIZ", "horiz_x", "y_member")` 且源于 `marker_synth` 或 `dxf_geom` 的横梁时，对 `b/l/r` 面构件保持 `geometry_class="recognized"`, `evidence_status="recognized"`；
  2. 在 `traceability/eval/metrics.py:1120-1140`（`_bar_caliber_class`）中，对 `face in ("l", "r", "b")` 且为确定性水平环梁的杆件返回 `"recognized"`。

### 抓手 2：02 塔头 Side 侧立面 DXF 直读与 3D 合并（预计 +50~70 TP）
- **物理事实**：`35A1-JC1-02.dxf` 原图包含明确的独立侧立面（`region: [34540, 34620, -7645, -7340]`，宽 1212mm）。
- **代码实施**：
  1. 在 `traceability/intake/tower_dxf.py:1410-1430` 中，当 02 册提取 front centerline 时，仅从 `raw_segments` 过滤掉 front 区域，保留 side 区域线段正常进入常规提取器生成 `view_type="side"`, `geometry_class="recognized"`；
  2. 确保 `tower_views.py:merge_view_coordinates` 与 `merge_view_bars` 正确完成 02 front 与 side 节点在同 Z 处的 $(x, y, z)$ 配对。

### 抓手 3：04/05/06/07 册斜材 $t$-check 缝合与跨册连接（预计 +350~420 TP）
- **物理事实**：
  - 05 册 DXF 原始提取 285 条斜线段，经 `stitch_collinear` 严格单调外延约束（$t_{\text{free}} > 1.0$ 与 $t_{\text{free}} < 0.0$）后可保留 71+ 条长斜线；
  - 04 册标高修正为 $Z \in [24000, 30000]$；06 册标高为 $Z \in [12000, 17000]$；07 册为 $Z \in [7000, 12000]$。
- **代码实施**：
  1. 在 `traceability/intake/centerline_extract.py` 中维持 `stitch_collinear` 的 $t$-check 不变量，避免碎段被截短回退；
  2. 在 `examples/external/guowang_35A1/layer_overlay.json` 的 `centerline_extract` 中为 04/05/06/07 分册精细配置 `gap_tol`, `col_tol`, `min_cand_mm` 与 `beam_marker_levels_mm`。

---

## 4. 核心攻坚战线二：35A2-ZC1 冲刺 60%+（TP ≥ 171）

### 抓手 1：分册 $Z$ 锚点（`z_anchor_lo/hi_mm`）精细标定（预计 +60~80 TP）
- **现状分析**：ZC1 共有 285 根物理杆，当前 7 册 DXF 覆盖 $z \ge 17000\text{mm}$（共 248 根杆，占 87%）。
- **代码实施**：
  对 `examples/external/guowang_35A2_zc1/layer_overlay.json` 中的各分册 `z_anchor_lo_mm` 与 `z_anchor_hi_mm` 进行微调对齐：
  - `35A2-ZC1-01-1`（塔头段）：`z_anchor_lo_mm: 33000.0, z_anchor_hi_mm: 39400.0`；
  - `35A2-ZC1-05`（上塔身双立面）：`z_anchor_lo_mm: 26600.0, z_anchor_hi_mm: 30900.0`；
  - `35A2-ZC1-07`（中塔身段）：`z_anchor_lo_mm: 16900.0, z_anchor_hi_mm: 21800.0`；
  - `35A2-ZC1-10`（塔身段）：`z_anchor_lo_mm: 24200.0, z_anchor_hi_mm: 28800.0`。

### 抓手 2：双立面（front + side）侧向半宽映射与空间闭合（预计 +30~45 TP）
- **代码实施**：
  利用 05、09、10 册的正侧双立面图纸，正视提取 $X$ 坐标与断点，侧视提取 $Y$ 半宽，通过 `tower_views.py` 自动闭合三维环梁与对偶侧向斜材（`depth_diag`）。

---

## 5. 验收步骤与检查清单

- [ ] **步骤 1**：运行 `python3 -m pytest tests/ -q`，确保 595 个测试全部通过（0 failed）。
- [ ] **步骤 2**：运行 `python3 scripts/run_35A1_jc1_full.py --out-dir out/35A1-JC1-pure60 --skip-sync`，随后运行 `python3 scripts/eval_a2_profiles.py examples/gt/35A1-JC1_ground_truth.json out/35A1-JC1-pure60/model.json`。
  - 检查 `A2-dual-view-pure` TP 是否 $\ge 642$（Recall $\ge 60.0\%$）；
  - 检查 `A2-dual-view-reconstructed` Recall 是否 $\ge 95.0\%$。
- [ ] **步骤 3**：运行 `python3 scripts/run_35A2_zc1_full.py --profile canonical_assisted --out-dir out/35A2-ZC1-pure60 --skip-sync`，随后运行 `python3 scripts/eval_a2_profiles.py examples/gt/35A2-ZC1_ground_truth.json out/35A2-ZC1-pure60/model.json`。
  - 检查 `A2-dual-view-pure` TP 是否 $\ge 171$（Recall $\ge 60.0\%$）。
