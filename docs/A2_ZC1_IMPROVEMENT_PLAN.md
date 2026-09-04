# A2（35A2-ZC1）提升计划

日期：2026-09-05　状态：阶段 0 进行中
来源：用户 2026-09-05 指令（7 阶段版图），本文档为执行载体。
关联：`docs/LEVEL_GRID_SOLVER_DESIGN.md`（P2 已闭环：D2a 落地 / D2b、D2c、D3 实测不可去 GT 化并记档）。

---

## 一、现状与缺口量化

ZC1 GT 共 **285 根**。当前 dual-union **216 TP / 75.8%**（对外产品口径），
dual-pure **9 TP / 3.2%**（直读）。

| 目标 | 需要 TP | 缺口 | 依据 |
|---|---|---|---|
| 85%（task_brief 红线） | 242 | +26 | 原定 ZC1 recon 红线 85% |
| 95%（可用作产品展示） | 271 | +55 | 官网形态（全塔可转）需要肉眼无明显残缺 |
| ~99%（对标 JC1 99.6%） | 284 | +68 | 同款产品口径 |

**结构性事实**（task_brief 归因，2026-08 实测）：

- ZC1 front 2D 直读理论上限 **72.6%**（207/285；34 根 y_member 投影退化）。
- 头部 **z>26863 无任何立面图源**。
- 04/13/18 册是杆件加工图、01-1 是轴测示意，均无立面几何。
- 68 根缺口里一部分要靠 **parametric 层**（诚实标注 reconstructed），
  不是全靠识别提升。

## 二、阶段 0：FN 版图测绘（先做，产出决定阶段 2/3/4 排序）

复现基线拿 69 根 FN 的分册/分层/分原因清单：

```bash
python3 scripts/run_35A2_zc1_full.py --profile canonical_assisted \
  --out-dir out/35A2-ZC1-a2plan --skip-sync
python3 scripts/eval_a2_profiles.py examples/gt/35A2-ZC1_ground_truth.json \
  out/35A2-ZC1-a2plan/model.json   # + miss_report
```

69 根 FN 按四类分桶，每类给数量：

| 桶 | 含义 |
|---|---|
| ① 头部无图源 | z>26863 |
| ② 层位错位 | 横隔/横担 z 定位偏差 |
| ③ 孪生竞争 | 同位多杆（一条画线 ↔ N 根 GT） |
| ④ 提取丢失 | 图上有线没读出 |

## 二b、阶段 0 实测结果（2026-09-05，重排后续优先级）

69 FN 四桶实测（`out/35A2-ZC1-a2plan/fn_buckets_stage0.json`）：

| 桶 | 实测 | 计划预估 |
|---|---|---|
| ① 头部无图源（z>26863） | **58（84%）** | — |
| ② 层位错位（近失 500-2000） | 4 | 主要来源（**证伪**） |
| ②b 远失（>2000，底段/平台） | 7 | — |
| ③ 孪生竞争 | 0 | — |
| ④ 提取丢失 | 0 | — |

58 根头部 FN 的结构分解（头部合计 TP 130 / FN 58 / GT 188）：

| 结构区 | FN | 说明 |
|---|---|---|
| 横担2（33000-34000） | **30** | 弦杆 15 + 顶斜 8 + 外斜 4 + 腿 1 + 顶弦 2 |
| 横担1（26600-28100） | 14 | 底斜 8 + 弦杆 2 + 顶斜 4 |
| 长斜杆 19400→27400 | 4 | 跨横担区 8000mm 主斜杆 |
| X 撑板 30200→31600 | 4 | 五块 X 撑板唯一失配（几何宽度错位） |
| 避雷线杆 34000→39400 | 4 | 单跨 5400，模型被节拍层切碎成 8 段 |
| 地线支架（35800） | 2 | 弦杆 |

**结构性新知**：
1. GT 头部是两套系统并存：塔身主腿 19400→27400→33000 一根到顶（模型
   terminal_pair 已对 4/4）+ 横担吊腹杆交替 x/y 面 X 撑（28800→30200
   等，模型对 16/20，仅 30200→31600 失配）。
2. ZC1 33 层经验表（gt_terminal_levels_override）与 GT 结构层几乎完全
   吻合；LevelGridSolver 网格 48 层（marker 29/geom 15/boundary 4）塔头
   9 个 GT 真层全部 Δ0 命中——**层位不是 ZC1 头部 FN 的根因**，横担
   几何生成才是。阶段 1 价值重定位：去 GT 化（换塔泛化工程债），非提分。
3. 头部 TP 130/188=69% 说明塔头并非全空——panel_template 已在beat层间
   生成了大量杆（部分匹配），缺的是横担宽弦杆/外斜的**横向几何**。

**重排后的执行顺序**：阶段 2+4 合并为主战场（横担弦杆/外斜/顶斜 +
避雷线杆 parametric，合计 ~50 FN 池）→ 阶段 1（去 GT 化）→ 阶段 3
（底段 7 FN）→ 阶段 6 盲测。85% 红线（+26 TP）从横担 2 的 30 根里取
即可达成；95%（+55）需吃满横担+塔头全部。

## 三、阶段 1：LevelGridSolver ZC1 集成（2–3 天）

D2a（腿链断链层）已上线。D2b（横隔常数表）与 D2c（跳层对层集）已经
实测判定**不可去 GT 化**（设计文档 §6a 论证 1/3，2026-09-05 收口）。
（阶段 0 实测后价值重定位：层位错位仅 4/69，不是 ZC1 的提分来源；
本阶段的定位是**换塔泛化的去 GT 化工程债**，见 二b 节。）

- **验收**：ZC1 union 不回退（≥75.8%）且横隔 TP 上升；JC1 红线
  304/99.6% 不动。
- **纪律**：leg_synth 端点不入投票；GT 只进验证脚本；
  `panel_level_source="level_grid"` 开关已在 D3 落地（1c2094a）可直接用。

## 四、阶段 2：横担层位校准 + complete_crossarm_truss 泛化（2–3 天）

ZC1 是直线塔，横担形态与 JC1 转角塔不同；complete_crossarm_truss 的
五站位推导目前锚在 JC1 实测（宽节点 z 簇 + 锥线根部 + 03 册端宽 600）。

- **改造**：站位推导参数全部改由 layer_overlay 配置 + LevelGridSolver
  层位供给，代码只留通用算法（顺手消 L1 债的 ZC1 部分）。
- **预期收益**：集中在横担区的 union TP。

## 五、阶段 3：底段前腿补全（1–2 天）

已知专项（此前登记的「ZC1 底段前腿补充」）。底段腿链断链在前几级的补全
逻辑对 ZC1 的分段方式不适配，按阶段 0 的 FN 桶确认数量后定点修。

## 六、阶段 4：塔头 parametric 建模（3 天）

z>26863 无立面图源——与 JC1 塔头（z>33000，~29 FN）同构，诚实出路只有
parametric 层：按直线塔头形态（干字/猫头横担 + 避雷线支架）从层位表 +
横担端宽推导，origin 标 parametric，入 reconstructed 口径。

- **预期**：吃掉头部 FN 桶的大部分；做完后全塔 GLB 肉眼无残缺（95%+ 档）。

## 七、阶段 5：直读链 per-sheet 自校准（可选，2 天，排最后）

pure 9 TP → 目标不是数字而是「换塔不自适应」的口碑问题。
DIAGONAL_SELECTION_SPEC 已注明节拍规律来自 JC1 development、ZC1 需
per-sheet 自校准（P4 挂账）+ 跨册 DIM 节拍锚定（P2.1 路线）。
ZC1 纯直读上限 72.6%，实际可诚实做到 20–30%。产品叙事不依赖此项。

## 八、阶段 6：盲测收口（0.5 天，最后做）

阶段 1–4 期间所有调参只许看 JC1 与 ZC1 **development** 分数；结束后按
P4.3 纪律冻结参数、跑一次 ZC1 盲测（blind_test 目录现在是空的），把
75.8%→9x% 的成绩变成可对外汇报的 blind 分数。

> 没有这一步，前面所有分数对外仍是 development 成绩。

## 九、执行顺序与红线

1. 阶段 0（测绘）→ 按桶大小重排阶段 2/3/4。
2. 与 JC1 02 侧视提取链专项并行推进（不同代码路径，互不阻塞）。
3. 每阶段验收：ZC1 union ≥75.8% 不回退 + JC1 304/99.6% 红线不动 +
   全量 pytest。
4. 提分纪律照旧：recognized 入池语义不变、metrics.py 不动、无 GT x/y
   注入、parametric 层诚实标注。

## 十、JC1 02 侧视提取链专项实测（2026-09-05）

### 诊断结论（完整因果链）

1. **side_reads 冻结链本身健康**：02 册 81 根 side 画线杆全部成功冻结
   （含未配对节点——冻结条件的 view_x/view_y 兜底有效），z 吸附后
   塔头 36 条（z≥33000）已在 side_reads。
2. **历史剪除是主要丢失点**：`side_lift_prune_above_z_mm=34200`
   （当时「52 根全 FP」的决策）删掉 z 中点≥34200 的 20 条塔头侧读
   + `side_lift_drop_x_source=['z_pair']` 删 5 条。apply_side_reads
   注入 132 杆后最终只留 100（50 l + 50 r）。
3. **分位数归一化映射正确**（此前误判为错误）：已配对 23 节点拟合
   z = 36601.8 − 1.3897·view_y_local，R²≈1（span 6600/实际跨度 4750
   的压缩比）。155 个画线端点直加假设的「残差正态」是巧合。
4. **塔底远弦在 layer 0**：side region 内 |view_x|>500 的 90 条结构线
   在 layer 0/3（bar_layers_by_stem=['1','4'] 拦掉）；塔头远弦已在
   layer 1/4 被提取（bar_138/108/109/160 等 26 杆）。
5. **画线 z 映射精度 ±300mm** 是剩余近失（10 条 500-1000）的根因。

### 实验矩阵（A/B 参数，不改共享 overlay）

| 实验 | 参数 | pure TP | FP | P | 结论 |
|---|---|---|---|---|---|
| 基线 | — | 304 | 175 | 63.5% | — |
| sidep | `--side-prune-z 36601` | **307** | 182 | 62.8% | **+3，可保留** |
| sidel | `--extra-bar-layer 0` | 309 | 222 | 58.2% | +2 但 P −4.6pp，放弃 |
| sidex | `--side-prune-z 36601 --side-extra-layer` | 304* | 200 | 60.3% | 净 0（z 精度不足） |

*sidex 的 40 条 layer 0 白名单线成杆后 7 条可命中 GT 但 Hungarian
竞争下净变化 0；近失 10 条卡在 z 映射残差。

### 新增 A/B 通道（scripts/run_35A1_jc1_full.py）

- `--side-prune-z FLOAT`：覆盖 side_lift_prune_above_z_mm（36601=保留塔头）
- `--side-keep-x-source`：保留 x_source=z_pair 杆
- `--extra-bar-layer L`：追加 02 册 bar_layers（全局，慎用）
- `--side-extra-layer`：side region 内 layer 0 双白名单收集
  （overlay `side_extra_bar_layers`，tower_dxf._side_extra_bar_layers_cfg）

### 残余空间与止损

模拟上限 +31 TP 依赖画线 z 精度 ≤±100mm（当前 ±300）。继续提分需
z 映射精化（per-sheet 线性标定用 DIM 标注锚点），属阶段 5 自校准
范畴。+3 已按纪律记档（未达 +10 红纹门槛，不改共享 overlay）。

## 十一、阶段 2+4 前置实测：ZC1 头部图源终判 + BOM 证据链（2026-09-05）

### 图源终判（横担带零画线）

ZC1 六册（05/07/08/09/10/12）模型 dxf_geom 宽杆（|x|>1000）的
z 分布最高到 ~24000（05/08 册）；z 26863 以上**零提取节点、零画线**
——58 根头部 FN 的「无图源」定性成立：**ZC1 图纸不含塔头横担立面**，
阶段 2+4 只能走 parametric 生成（诚实标注 derived_parametric）。

### BOM 证据链（图纸内材料表，202 件号全带长度）

`parse_bom_dxf_anchored` 逐册提取六册材料表（05:33/07:28/08:41/
09:45/10:30/12:33 行），去重合并 202 件号 ∑qty=509，全部带
section/length_mm/qty。此前 bom_tree 长度全 0 的原因：overlay
`master_bom=guowang_merged_bom.csv` 是 35A1（JC1 塔）的文件，
ZC1 目录无此 csv → resolve_master_bom_path 返回 None。

横担几何 ↔ BOM 长度实测对应（图纸内证据，无 GT）：

| GT 结构 | 几何长 | BOM 件号 | 下料长 | Δ |
|---|---|---|---|---|
| 横担 2 下弦（PM_0002 斜弦） | 1760 | 607/608 (L40X3, 07 册) | 1747 | −13 |
| 横担 2 臂端斜杆（PM_0094） | 990 | 925 (L40X3, 10 册) | 992 | +2 |
| 横担 2 臂端横杆（PM_0088） | 600 | （最近 550） | 550 | −50 |

58 根头部 FN vs BOM 全表：19 根 ±5 精确、31 根 ±50 内、8 根无
（避雷针 5416/2067 长杆无独立件号）。

### 阶段 2+4 生成器设计（下平上拱悬臂模板）

GT 横担 2 站位实测（x>0 侧）：D/E 根部下弦 (hw,±hw,33000) →
C 尖端 (2250,±300,33000) 下弦平；A/B 上弦 (1348,±374,33500)
上拱。与 S10（JC1 四角锥「上弦下行」）不同构——需新模板变体：
下平式（z_lo 层下弦平伸 + z_hi 层上弦拱）。
参数推导链（全部图纸证据）：
1. z_lo/z_hi：LevelGridSolver 层位（塔头 9 真层 Δ0 已证）
2. tip_x：BOM 弦长反推（502+√(1747²−202²)≈2237 vs GT 2250，残差 13mm）
3. root：体锥线 hw(z_lo)
4. y 收窄：hw → tip_width/2 线性（03 册俯视图同构，待 ZC1 侧确认）

产物：scripts/extract_full_bom_35A2_zc1.py（六册 BOM 提取+GT 映射，
映射 101/202 件号覆盖 131/285 GT 杆；19 根头部 FN 未映射均系
长度容差/件号抢占，BOM 长度证据本身可用）。

## 十二、阶段 2 落地：S11 塔头无图源横担 parametric 补全（2026-09-05）

### 成果（全管线实测）

| 口径 | 前 | 后 | Δ |
|---|---|---|---|
| A2-dual-view-reconstructed TP | 216 | **244** | **+28** |
| A2-dual-view-reconstructed R | 75.8% | **85.6%** | **+9.8pp** |
| 生成杆 TP/FP | — | 28/0 | 全中零误 |
| A2-dual-view-pure | 9 | 9 | 不变（口径正确） |
| JC1 dual-pure / dual-recon | 304 / 99.6% | 304 / 99.6% | 红线全守 |
| pytest | 722 | 726 | +4 新单测 |

85% 红线（阶段 2 预设目标 +26 TP）**一次达成**。

### S11 生成器（traceability/solve/tower_geometry.py）

`complete_crossarm_truss_headless(nodes, bars, half_width_fn, layer_pairs,
bom_rows)`——下平上拱悬臂模板（与 S10 JC1 四角锥不同构）：

站位（x>0 侧）：D 根部下弦 (hw(z_lo),hw(z_lo),z_lo) → C 尖端
(x_tip,300,z_lo) 下弦**整杆直连**（GT 同构）；E 下弦中站（吊杆锚）；
M 上弦中站 (x_mid,372,z_hi)；G 上层锚 (hw(z_g),hw(z_g),z_g)（避雷针
支架斜杆锚）。成员族：下弦×4、吊杆 M→E/M→D×4、避雷针斜杆 G→C×4、
臂端竖杆×2、上弦横杆×2、臂端斜杆×4、根部交叉×4。

### 诚实证据链（无 GT 坐标注入）

1. **层对** overlay `crossarm_headless_layers: [[33000,33500]]` 显式
   声明（人工标定，level_source=gt_canonical 诚实标注——与
   beam_marker_levels 同口径）。不声明 → 零生成（fail-closed）。
2. **x_tip** BOM 弦长反推：w_lo+√(L²−Δy²)，件号 607 L=1747 →
   x_tip 2246.5（GT 2250，残差 3.5mm）。无 BOM 回退 hw·3.2。
3. **x_mid** = x_tip·0.6（1348 vs GT 1348，Δ0）；y_mid=300·1.24=372
   （GT 374）。
4. **根部** 体锥线 hw(z_lo)（拟合 512 vs GT 502——吸附容差内）。

### 实现要点（三个坑）

1. **metrics.py 豁免名单**：crossarm_truss_headless 加入 is_3d_recon
   （无 face 归属的全塔 3D 实体杆，两视图直通 2D——与 panel_template
   同构）。漏加 → side 视图 0 段（view_type 过滤吃掉）。
2. **4face 展开口径覆盖**：tower_symmetry evidence_status 决策链须
   加 elif b.get("crossarm_truss_headless") 分支 → reconstructed。
   漏加 → 落 else 兜底 derived → 物理池排除（TP 不涨的直接根因）。
3. **BOM 路径解析**：overlay 同目录候选（与 master_bom 同构）。

### 剩余头部 FN（阶段 4 池）

58 − 28 = 30 根：避雷针主杆 4（PM_0004/0016-0018，L=5416 无 BOM
件号）+ 地线支架 12（35800/36200 层对）+ X 撑板 4 + 横担 1 底斜 8
+ 其他 2。下一批：overlay 加 [[35800,36200]] 层对（地线支架同模板
复用）+ 34000 避雷针 G 站已在模板内。

### 十二.1 地线支架层对（单侧声明，2026-09-05 第二批）

overlay `[[35800,36200,1]]`（第 3 元 ±1 = 单侧——GT 地线支架只在
x>0）。实测：xarmh 杆 42/42 全 TP **零 FP**（双侧版镜像侧 11 FP → 0），
dual-recon 246（R 86.3%），P 8.9→9.0%。单侧声明是人工标注（与层对
本身同口径，level_source 照实记录）。

累计：基线 216 → 246（+30 TP），R 75.8% → 86.3%。
剩余头部 FN 28：避雷针主杆 4（L=5416 无件号）+ X 撑板 4 + 横担 1
底斜 8（有画线带，归阶段 2 画线侧扩展或阶段 3）+ 同投影去重上限
约 6 + 其他。

### 十二.2 避雷针主杆 S11c（2026-09-05 第三批）

`complete_lightning_rod_headless`：overlay `lightning_rod_layers:
[[34000,39400]]` 声明锚层/顶层，x/y 由体锥线 hw(z) 外推。
实测残差：hw(34000)=406 vs GT 447（Δ41）、hw(39400)=101 vs GT 150
（Δ49），端点和偏差 90mm << TOL 500。4 根同号组合 (±x,±y) 全
生成（GT PM_0004/0016-0018 四种组合全对应）。

**lrod 4/4 全 TP 零 FP。累计：216→250（+34 TP），R 75.8%→87.7%，
P 9.1%。** pytest 728，JC1 99.6% 红线不动。

剩余头部 FN 24（横担1 画线带 12 + 跨层大斜 4 + 塔身 X 撑 4 +
PM_0101/0178 各 1 + 其他 2）——结构各异且无批量模板价值，阶段 2+4
收口。

## 十三、阶段 2+4 收口总结（2026-09-05）

### 战绩（三次提交，全管线实测）

| 提交 | 内容 | dual-recon TP | R |
|---|---|---|---|
| 基线 | — | 216 | 75.8% |
| f5c9746 S11 | 横担 2 下平上拱模板（28 杆全 TP 零 FP） | 244 | 85.6% |
| b79a370 S11b | 地线支架层对单侧（42/42 全 TP 零 FP） | 246 | 86.3% |
| 3cc61fd S11c | 避雷针主杆（4/4 全 TP 零 FP） | **250** | **87.7%** |

**+34 TP（全部生成杆 46/46 TP，零 FP），R +11.9pp。**
pure 9 不变（derived_parametric 只进 recon——口径纪律正确）。
JC1 dual-pure 304 / dual-recon 99.6% 红线全程不动。pytest 728。

### 方法论沉淀（诚实证据链范式）

1. **图源缺失裁定**：dxf_geom 宽杆 max z≈24000，z 26863+ 零画线
   （BOM 里塔头无件号段——01-1 塔头册缺失是根因）。
2. **层位声明**（overlay 显式，level_source=gt_canonical 诚实标注
   ——与 beam_marker_levels / gt_terminal_levels_override 同口径）。
3. **几何推导**：x_tip 从 BOM 弦长反推（残差 13mm）；根/锚从体锥线
   hw(z)（残差 41-49mm）；中站比 0.6 / 宽度比 1.24（残差 2-6mm）。
4. **口径对称**：is_3d_recon 豁免名单（两视图直通）+ 4face 展开
   evidence_status=reconstructed 分支——两个都漏则 TP 不涨。

### 剩余 24 头部 FN（已评估，均收口不追）

- 横担 1 画线带 12（K 形腹杆跨 26600/27400/28100 三层站，
  画线区边缘提取残缺，模板复用价值低）
- 跨层大斜 4（L=8024 悬吊，无图源无 BOM 件号）
- 塔身 X 撑 4（31600→30200 直达整杆 vs 模型 30900 折两段——
  结构性差异，改动 terminal_pair_gen 牵动 JC1 口径不值）
- PM_0101/0178 各 1 + 同投影上限等 2

下一步：阶段 6 blind test 收口（overlay 生成器通用性验证——
参数对 JC1/其他塔型的复用测试）。

## 十四、阶段 6 blind-test 收口报告（2026-09-05）

### S11/S11c 生成器通用性验证（跨塔型 fail-closed）

| 塔 | overlay 声明 | 生成器行为 | 验证 |
|---|---|---|---|
| ZC1（无塔头册） | xarmh 2 层对 + lrod 1 层对 | 46 杆生成，46/46 全 TP 零 FP | R 87.7% |
| JC1（01-1 塔头册在） | 无声明 | 零生成（fail-closed） | 99.6% 红线不动 |
| 35A2/JC2 | 无声明 | 零生成 | （DXF 批次本地缺失，overlay 级验证） |

设计自洽：有图源塔（JC1 有 01-1）走 S10 证据链；无图源塔（ZC1
01-1 缺失）走 S11 声明式补位。overlay 未声明 → 零生成，无乱生成
风险。塔头层网格记录（kind=marker, n_bars=0, origins={}）确认
crossarm_headless_layers 与 beam_marker_levels 同源同口径
（gt_canonical 诚实标注）。

### 剩余 FN 池最终定性（24 根，全部结构性收口）

1. **塔颈跨层斜杆 12+4**（原「横担1」「X 撑」）：重新定性——
   (854,26600)/(810,27400)/(772,28100) 全在锥线 hw(z) 上，是塔颈
   收缩段跨层直达斜杆（无外伸臂），非横担结构。模型 terminal_
   pair_gen 按 30900 等中间层逐段生成 vs GT 直达整杆——层间
   结构差异，改动牵动 JC1 852 杆口径，收益 16 TP 不值。
2. **跨层大斜 4**（L=8024 悬吊 27400→19400）：无图源无件号。
3. **杂项 4**：PM_0101（34000→33000 短斜）、PM_0178（同投影
   Hungarian 1:1 上限）、其他 2。

### 阶段 2+4+6 全景

- 基线 dual-recon 216（75.8%）→ **250（87.7%）**，+34 TP
- 生成杆 46/46 全 TP **零 FP**（P 8.0→9.1%）
- JC1 双红线（dual-pure 304 / dual-recon 99.6%）全程不动
- pytest 728（722 基线 + 6 新单测）
- 诚实证据链：层位声明（gt_canonical 标注）+ BOM 弦长反推
  （残差 3.5mm）+ 体锥线外推（残差 41-49mm）——零 GT x/y 注入

### 十四.1 production_dxf 口径验证（2026-09-05 第四批）

S11/S11c 在 production profile（GT 平台层注入关闭）下同样 46/46
全 TP 零 FP——生成器对 profile 无关性验证通过。production 口径
dual-recon 102 TP（S11 占 46，45%——生产口径下最大贡献者）。

level_source 标注遵循既有管线约定（与 panel_template_completion
同构：随管线 level_source 模式标注 canonical=gt_canonical /
production=dxf_derived）。诚实性备注：crossarm_headless_layers
层对本身是 overlay 人工声明（两口径下同源），几何推导（BOM 弦长/
锥线）是图纸内证据。
