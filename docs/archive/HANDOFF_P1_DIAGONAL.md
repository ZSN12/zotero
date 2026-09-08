# HANDOFF：P1 斜材拓扑批次交接计划（2026-08-31，未完成状态）

> **交接原因**：P1.1 实现进行到一半，发现节拍单位推导错误（panel 层中位差
> =2000 ≠ 真实 fan 节拍 1000），已找到正确方向（候选跨度自校准）但未实施。
> 本文档包含：已验证的完整分析数据、当前未提交代码状态、精确修复指令、
> 后续任务。接手模型按顺序执行，不需要重新分析。

---

## 0. 仓库状态快照（2026-08-31 交接时点）

```text
HEAD = 2be473f feat(eval): P0 评测可信度批次
提交链（本线程）：e2d09b0(T0.1 双视图+pure统一) → 3d8e4f9(计划文档) → 2be473f(P0批次)
另一线程进行中：c78147a(T1 锚点review_required) 已提交，
               scripts/generate_assembly.py + tests/test_bolt_assembly.py 为其 T2 WIP —— 禁动
未跟踪：web/demo/35A1-JC2/（非本线程产物，忽略）
```

**未提交的半成品（本交接核心）**：
- `traceability/solve/diagonal_topology.py` —— 已加入 `select_interpretations()`
  + `_panel_grid_unit()`，**节拍单位推导是错的**（见 §2），2 个测试失败
- `traceability/intake/tower_symmetry.py` —— report 增加了 "selection" 键（正确，保留）
- `tests/test_diagonal_topology.py` —— 加了 `TestSelectionP11` 5 用例 +
  `build_interpretations` 返回值改元组的适配

**正式基线**（P0 批次已锁定，不得变更语义）：
```text
A2 multi.pure: TP=54 FP=173 P=23.8% R=5.0%（d1+d2<500, front, Hungarian 1:1）
A2 full:       TP=279 FP=430 P=39.4% R=26.1%（内部归因口径，含 80% GT 标高辅助）
06 拓扑模块：88 生成杆 / 58 TP(full池) / 60 TP+28 FP(dtd独占池离线)
```

---

## 1. 已验证的完整分析数据（接手后直接用，勿重算）

### 1.1 生产候选 11 个 fan 解读 + GT 真值判定（dtd 独占池 Hungarian, tol=500）

| # | h | P | score | n_ev | 跨度 | TP/FP | 判定 |
|---|---|---|---|---|---|---|---|
| 1 | 16488.6 | 19000 | 801.8 | 2 | 2511 | 0/8 | ✗ 假高度 |
| 2 | 14349.4 | 16000 | 818.6 | 8 | 1651 | 4/4 | 边缘（≈真14000→16000） |
| 3 | 13797.4 | 16000 | 1009.1 | 10 | 2203 | 8/0 | ✓ ≈真14000→16000 |
| 4 | 12143.0 | 14000 | 1015.2 | 9 | 1857 | 8/0 | ✓ ≈真12000→14000 |
| 5 | 13229.5 | 16000 | 1687.7 | 7 | 2771 | 8/0 | ✓ ≈真13000→16000 |
| 6 | 15957.9 | 19000 | 1863.3 | 2 | 3042 | 8/0 | ✓ ≈真16000→19000 |
| 7 | 12683.4 | 16000 | 1948.5 | 11 | 3317 | 4/4 | 边缘（≈真13000→16000） |
| 8 | 12143.0 | 16000 | 2745.5 | 8 | 3857 | 8/0 | ✓ ≈真12000→16000（跳层，真实存在） |
| 9 | 15417.8 | 19000 | 2943.6 | 2 | 3582 | 4/4 | 边缘 |
| 10 | 14898.0 | 19000 | 3220.2 | 4 | 4102 | 8/0 | ✓ ≈真15000→19000 |
| 11 | 14349.4 | 19000 | 3873.1 | 4 | 4651 | 0/8 | ✗ 无真实对应 |

注意：**score 与质量完全无关**（#1 score 最好却是 0/8）；n_ev 也不能区分。

### 1.2 GT 真实结构（z∈[11000,19000]，扩展窗口分析，含两端点可超窗）

- **fan 到 P=14000**：h=11000, 12000（+同层 14000→14000）
- **fan 到 P=16000**：h=12000, 13000, 14000（+同层 16000）
- **fan 到 P=19000**：h=15000, 16000, 17000（+同层 19000）
- 同层 fan（corner→mid 同 z）：11500, 14000, 16000, 19000 各 8 根
- **twist 对**（corner↔corner，当前模型 0 触发）：11500↔14500×12, 11800↔14400×12,
  12000↔14500×12, 14000↔17000×12, 14400↔17000×12, 14500↔17000×24,
  16000↔19000×16, 17000↔19400×12, 等
- **关键规律：跨层 fan 跨度 ∈ {2000, 3000, 4000}**（= 细网格 1000 的 k∈{2,3,4} 倍）
- h=12000 同时扇 14000 和 16000（跳层 fan 真实存在）→「同 h 最多 2 个 fan」规则正确

### 1.3 节拍筛选量化验证（已用全部 11 候选验算）

**正确单位推导：unit = median(候选跨度)/3 = 3042/3 ≈ 1014**
节拍 = |span − k·unit| ≤ 450, k∈{2,3,4}：
- 拒 #1（2511, 最小误差 489>450）✓
- 拒 #11（4651, 误差 595）✓
- 拒 #9（3582, 误差 474）✓（边缘 4/4，损失 4TP 换 4FP）
- 保 #2(377) #4(171) #3(175) #5(271) #6(0) #7(275) #8(199) #10(46)
- **结果：9 fan × 8 = 72 杆，56 TP / 8 FP**（当前 60/28 → 目标 TP≥55 FP≤15 达成）

**错误单位（当前未提交代码）**：`_panel_grid_unit` 用 panel_levels 中位差
= canonical 15 层 [6500,8500,11500,14000,16000,19000,20883,22800,24000,30024,
30800,32700,33525,34200,36600] 的中位差 **2000** → 节拍 {4000,6000,8000} →
把几乎所有真 fan 拒掉（synthetic 测试因此失败）。panel 层是粗糙平台位，
不是 fan 跨度节拍网格，**此函数必须替换**。

---

## 2. P1.1 剩余步骤（接手第一件事）

1. **改 `select_interpretations` 的节拍单位**：
   - 删 `_panel_grid_unit`（或保留但不再用它做 beat）
   - unit = median(fan 候选跨度)/3；**护栏**：fan 候选 < 4 个时跳过节拍筛选
     （样本不足，自校准无意义）——这同时让 synthetic 小测试自然通过
   - beat_k∈{2,3,4}, tol 450 保持
   - 同 h ≤2、panel_crossing 保险规则保留（当前实现可用）
2. **修两个失败测试**：
   - `TestSelectionP11::test_panel_crossing_rejected`：断言写反了——交叉保险按
     h 序保留先到者 (12000→16000)，拒 (13000→14000)。把断言改成
     `(12000.0,16000.0) in pairs` 且 `(13000.0,14000.0) not in pairs`
   - `TestInterpretations::test_fan_twist_pairs`：换自校准单位 + <4 候选护栏后
     自然通过（synthetic 只有 1 个 fan 候选 → 跳过筛选）
   - `test_span_off_grid_rejected`：改成造 ≥4 个 fan 候选（跨度混合真假）再验
     单位自校准拒掉离拍的；audit 里 `beat_unit` 断言改为 ≈1014 或按造数计算
3. **验证（三步，不可跳）**：
   ```bash
   python3 -m pytest tests/test_diagonal_topology.py -q          # 全绿
   python3 scripts/run_35A1_jc1_full.py                          # 全管线重建
   python3 scripts/evaluate_ground_truth.py \
     examples/gt/35A1-JC1_ground_truth.json \
     out/35A1-JC1-full-deliver/model.json --tol 500
   ```
   验收标准：
   - full 口径 TP ≥ 276（279 − 3 容忍；dtd 独占池预期 56 TP/8 FP）
   - `out/35A1-JC1-full-deliver/metrics_by_origin.json` 中
     `diagonal_topology_reconstructed` 的 n_model=72、FP ≤ 15
   - model.json 里 drawing_file.diagonal_topology_report.selection 有
     rejected 记录（含 reason，P0.5 语义：拒绝必须显式）
4. **回归**：`python3 -m pytest tests/ --ignore=tests/test_bolt_assembly.py`
   （另一线程 T2 WIP 会 collection error，忽略即可）
5. **提交**（一次）：`feat(solve): P1.1 fan 候选冲突图择优——跨度节拍自校准`

已知残留（可接受，写入提交信息）：P=16000 仍 5 fan vs 真实 3（mid-edge
度数超真实结构，但经节点复用+容差全部命中 GT，不产生 FP）；节拍假设是
JC1 development 经验规律，盲测 ZC1 时随 panel 网格自适配有风险，已在
UNIMPLEMENTED_PLAN.md P4 冻结参数条款覆盖。

---

## 3. P1.2：twist 真实触发（P1.1 完成后）

现状：`twist_pairs=0`。根因线索（已查实）：**06 图 front 面证据线里没有
FULL 线**——line_kind 分布 {None:7, MID:12, HALF:4}，而 twist 只信 FULL
（角→对角 x1·x2<0）。GT 里 twist 是大头（该窗 104+ 根）。

待诊断方向（按优先级）：
1. twist 证据线可能不在 front 面 dxf_geom 集合里（b/l/r 面？被 face_only=True
   过滤？）——放宽 `collect_diagonal_candidates` 的 face 过滤试 twist-only 收集
2. 可能被 inclination 门限（20~70°）或 min_len 400 剪掉——对角线投影倾角
   恰在 45° 附近应过，但 split 段可能变短
3. FULL 分类要求两端 |x|/hw ≥0.6 且异号——若 hw_fn 在窗口上端偏差大，
   比值失准 → 检查 hw_fn 与 GT 锥线在 z>16000 的偏差
验收：twist_pairs ≥ 2 且 dtd 独占池整体 TP/FP 不降（fan 56/8 基线）。

## 4. P1.3：推广 05 分册（z_window 自动检测）

05 是最大 DXF FP 源（76 根）。要点：
- z_window 不写死：从 05 图纸证据线 z 聚类自动推窗（生产 overlay 现在
  写死 06 的 [11000,17500]）
- `diagonal_topology_sheets` overlay 键已支持列表，加 "35A1-JC1-05"
- 05 段 GT 平台层位不同（P 集合变化），节拍自校准单位也会不同——
  **per-sheet 跑 select_interpretations，不共享 unit**
- 验收：05 DXF FP −25~40，reconstructed 口径 TP +20~50，其他分册不退化
- A/B 纪律：只开 05 跑一次对比（只开 06 vs 06+05），不许一次全开

## 5. P1.4：07 → 04 → 02 推广（最后做 02，横担拓扑不同）

每分册独立 A/B；07 预期结构同 06；02 是塔头/横担，fan/twist 模板可能
不适用，先探查再定。

## 6. 批次 2 之后（详见 UNIMPLEMENTED_PLAN.md，已完成规划不再重复）

- P2 主腿粒度与纯识别（链分组/DXF 证据切分/角色拼接/MLLM keep-drop/gate 阻断）
- P3 Precision 清理（横隔 180FP/塔头伪横隔/24 crossarm/05 清洗）
- P4 盲测（冻结参数 → ZC1（勿用 JC2，骨架几乎相同）→ 不许看结果调参）

---

## 7. 硬约束（违反即返工）

1. **禁改文件**：`scripts/generate_assembly.py`、`tests/test_bolt_assembly.py`
   （另一线程 T2 WIP）；`web/demo/35A1-JC2/` 忽略
2. **评测语义已冻结**（P0 批次 2be473f）：--tol/--tols、COST_SEMANTICS
   (d1+d2 和语义)、profile 拆分（canonical_assisted/production_dxf）、
   unscorable_report、eval_binding SHA——只准用，不准改含义
3. **口径纪律**：full=279 是内部归因（80% GT 标高辅助）；对外只有
   multi.pure；06 拓扑杆 level_source=gt_canonical → level_assisted 口径，
   production profile 下 dxf_derived → reconstructed 口径
4. **不提交失败测试**；每任务独立提交；提交信息带实测数字
5. 节拍筛选只准用候选自身 + panel_levels，**严禁任何 GT 信息进算法**
   （本文档 §1 的 GT 分析只用于验证和验收，不进代码）

## 8. 验证脚本参考（dtd 独占池归因，接手后可直接重写）

要点：从 model.json 取 `geometry_origin=diagonal_topology_reconstructed` 杆，
front 投影 (x,z)，与 gt_bars_2d 全量 Hungarian（tol=500），按几何 pair
(min/max 端点 z) 分组统计 TP/FP。注意：报告里 interpretations 的 z_lo 与
几何分组 z 有偏差（_find_or_add_node 容差 300 复用真节点会把 corner z
吸走），归因用几何分组，不用报告 z。
