# 35A1-JC1 未实现与待优化任务清单 (UNIMPLEMENTED PLAN)

本文档记录 **35A1-JC1 铁塔三维重建与评测体系** 的正式能力基线、已完成里程碑、以及所有**尚未实现 / 待优化**的任务清单、技术方案与优先级。

> 更新时间：2026-08-31（审查闭环后定稿）。历史版本（A2 188 时代及更早）见
> `docs/UNIMPLEMENTED_PLAN.md` 归档。本文档是唯一权威行动清单。

---

## 一、正式能力基线（2026-08-31 审查闭环锁定）

### 0. 官方口径与数字（不可再变更语义）

```text
数据集：35A1-JC1（development set，非 blind test）
口径：  multi-caliber pure
视图：  front 2D 投影
匹配：  Hungarian 一对一
代价：  d1 + d2（两端点误差之和）
门限：  cost < 500mm（严格小于）

TP=54   FP=173   FN=1017
Precision=23.8%   Recall=5.0%
```

**口径纪律**：
- `full = 279 TP / 39.4% P / 26.1% R` **必须** 标注为「含 GT canonical 标高辅助
  （level-assisted 223 TP，占 full 80%）+ 参数化补全」的内部归因结果，
  **不得** 作为纯图纸重建能力对外汇报。
- front 投影结构性不可测：y_member 87 根（投影退化为点）、depth_diag 与 leg
  投影重合损失 126 根（1:1 匹配下最多召回一半），合计 213 根进分母但不可达
  （front 理论上限 858/1071 = 80.1%）。
- JC1 是 development 集：所有数字存在对该塔型调参的过拟合风险，泛化结论
  必须等 ZC1 盲测（P4 批次）。

### 1. 五层口径归因（A2 TP@500，front，d1+d2<500）

| 口径 | 模型杆 | TP | FP | P | R | 说明 |
|---|---|---|---|---|---|---|
| pure | 227 | 54 | 173 | 23.8% | 5.0% | 纯 DXF 直接识别（正式主口径） |
| reconstructed | 238 | 58 | 180 | 24.4% | 5.4% | + 证据驱动重建（dtd/collinear_stitch） |
| level_assisted | 688 | 274 | 414 | 39.8% | 25.6% | + GT canonical 标高辅助（横隔/节间细分） |
| parametric | 21 | 5 | 16 | 23.8% | 0.5% | 参数化底段外推 |
| full | 709 | 279 | 430 | 39.4% | 26.1% | 物理全量（内部归因口径） |

- A2-effective（z≥6500mm，剔除底段无源区）：TP 274 / P 39.8% / R 27.8%。
- **full TP 来源拆解**：level-assisted 223（80%）+ DXF 直接 47 + 证据驱动 4
  + 参数化 5。主要能力结构 = canonical 层高辅助，非纯识别。

### 2. 分角色缺口（full 口径）

| GT 角色 | n_gt | TP | FN | R | 判断 |
|---|---|---|---|---|---|
| horiz_x | 208 | 162 | 46 | 77.9% | ✅ 已做好，不是主问题 |
| diagonal | 272 | 52 | 220 | 19.1% | 🔴 最大可操作缺口之一 |
| leg | 252 | 48 | 204 | 19.1% | 🔴 最大可操作缺口之二 |
| depth_diag | 252 | 17 | 235 | 6.8% | ⚠️ 与 leg 投影重合，front 1:1 下结构性损失 126 |
| y_member | 87 | 0 | 87 | 0% | ⚠️ front 投影退化为点，几何不可测 |

### 3. 关键模块效率（full 口径来源）

| 模块 | 生成 | TP | FP | 命中率 | 判断 |
|---|---|---|---|---|---|
| 06 段斜材拓扑（diagonal_topology） | 88 | 58 | 30 | 65.9% | ★ 当前最有效的非横隔算法 |
| 横隔（diaphragm_reconstructed） | 330 | 150 | 180 | 45.5% | 最大 TP 也是最大 FP 来源（层位重复/塔头过生成） |
| DXF 直接识别（dxf_geom） | 227 | 47 | 180 | 20.7% | FP 高：05 分册 76 根为最大污染源 |
| 共线拼接（collinear_stitch） | 12 | 4 | 8 | 33.3% | 效果一般，需按角色重构 |

- 06 段拓扑当前 fan_pairs=11 / twist_pairs=0——twist 路径在真实生产输入
  上尚未触发；且 11 个 fan 解释**全部生成**（候选竞争未择优）。
- DXF FP 分册分布：05=76 / 07=35 / 04=28 / 02=28 / 06=13（06 已被拓扑解释
  替换掉大部分错误投影杆，05 尚未）。

---

## 二、已完成里程碑（提交链）

| 提交 | 内容 | 效果 |
|---|---|---|
| `9777f22`/`459c6cf` | GLB 法线烘焙 + viewer 法线补算 | 节点板发黑修复 |
| `5d14b71` | P2 diff 溯源可视化四件套 | 待复核橙/GT 叠加/高度切片/图纸下拉 |
| `f4ccbe8` | P5/P6 LOD2 角钢实体 + LOD3 节点板螺栓样板 | 独立目录产物 |
| `257c69d` | P3 横隔几何去重 + 主腿节间守恒审计 | 审计 9 腿/23 段/max_rel_err=0.0 |
| `98444d6` | P0 版本钉扎 + P1 06 段斜材拓扑闭环 | **A2@500 221→279（+58），P 35.4→39.4%，R 20.6→26.1%**；diagonal R 10.3→19.1% |
| `2ca4456` | P4 底段参数化透明化 | 80/80 杆带 parametric_struct + viewer 免责声明 |
| `c45b7fd` | LOD 阶段 1+2：L 截面参数化挤出 + 确定性朝向解算 | solid_angle_tower.glb（任务 E/F） |
| `5c0ecaa` | LOD 阶段 3：节点板薄壳全塔锚定 | gusset_attached.glb（任务 G） |
| `c2fa710` | LOD 阶段 4：六角螺栓群 + 热镀锌 PBR + 汇总装配 | assembly.glb（任务 H） |

早期里程碑（z 偏移修复 58d3af7、横隔 physical 化、S1 系列节点 ID 碰撞修复、
S2b canonical 层对齐、S6 主腿节间化、悬空门禁、假 bolt_group 清零等）见
`docs/UNIMPLEMENTED_PLAN.md` 归档与 `PHASE_PROGRESS.md`。

**历史累计**：A2 full 召回从 1.9% → 4.8% → 17.6% → 20.6%（P1 前）→ **26.1%**。

### 双视图联合口径（已实现，待提交）

`eval_a2_dual_view`（front ∪ side，杆粒度并集语义）：
**full TP 477 / P 39.4% / R 44.5%**（vs front 单视图 279/39.4/26.1）——
+198 TP、P 不降、R +18.4pp。含 side 视图 (y,z) 投影轴修复与 b 面排除
（1:1 失衡防护）。**未提交**（metrics.py + tests/test_a2_dual_view.py，
6 用例全绿）。该口径是诚实扩展（l/r 面杆件在 side 视图投影为真实形状，
与 GT 全塔投影对称），可作为并列报告口径，**不替代** front 主口径。

---

## 三、未实现任务清单（五批次，审查定稿后优先级不再变更）

### 批次 1：评测可信度（P0，绝对前置，不提分但保证后续提升是真的）

- [x] **P0.1 dual/multi pure 统一** ✅ 已实现待提交：`eval_a2_dual_caliber`
      的 pure_dxf 改走 `_bar_caliber_class` 唯一判定（此前 mode="recognition"
      混入 25 根 collinear_stitch/panel_cross@gt 杆，TP 64 vs 54 同名不同数）。
      修复后实测两端完全一致：227 杆 / TP 54 / FP 173。
- [ ] **P0.2 修复 --tol**：`evaluate_ground_truth.py` 的 CLI `--tol` 实际
      未生效（内部固定 DEFAULT_TOLS sweep）。改为 `--tol 500` 单档 /
      `--tols 50,100,200,500` sweep，报告写入实际容差。
- [ ] **P0.3 固化代价语义命名**：`segment_cost` 返回 d1+d2（两端点误差和），
      docstring「每端点最大允许误差」不准确。主指标名称固化为
      `endpoint_sum_cost_lt_tol`，docstring 修正，输出附带 cost_semantics
      字段；可选 A/B 诊断（sum vs max）不改变主指标。
- [ ] **P0.4 拆分 production_dxf / canonical_assisted 两套 profile**：
      当前 overlay 默认 `use_gt_platform_levels=true`（223 个 assisted TP
      来源）且传播到主腿细分/panel-cross/06 拓扑/横隔层位。生产 profile
      必须 `use_gt_platform_levels=false`（`use_gt_half_width=false` 同理）。
- [ ] **P0.5 unscorable / 生成失败统计**：缺 from_node/to_node/坐标/
      semantics 的杆件目前被评测静默跳过（生成失败混入 FN）。新增
      `unscorable_report.json`（unscorable_missing_node/coordinate/
      semantics 分类）+ `generation_status.json`（分册候选数/通过/拒绝/
      pending/failed）；pending 不再算成功、0 候选不再保存空壳成功退出。
- [ ] **P0.6 版本绑定**：报告绑定 commit SHA / model SHA / GT SHA /
      overlay SHA / agent_mode / dataset_split（development|blind）/
      caliber / view / cost_semantics / tolerance。versioning.py 已有部分
      基础，补 GT/overlay SHA。
- [ ] 附带修正：GT 投影日志「去重后」改为「物理杆件 front 投影」；
      assisted_gain 标注为净增益（非严格因果归因，严格归因用联合匹配
      的 by_origin）；effective_z_min=6500 标注为 JC1 profile 专用。

**验收**：同一运行 dual pure == multi pure；连续两次运行 TP/FP/FN 与
matched pairs 完全一致（确定性）。

### 批次 2：斜材拓扑能力（P1，TP 增益最大方向）

- [ ] **P1.1 06 fan 候选冲突图**：当前「所有 score<4000 的解释全部生成」
      （h=14349/13797/13229/12683/12143 → 同一平台 P=16000 全部产出）。
      改为解释图（候选=节点，共享平台/高度/证据线=冲突边），全局最优
      组合（最大证据覆盖/最小总分/最少杆件/无高度交叉，可用加权独立集
      或小规模 ILP）。目标：TP≥55 且 FP 30→≤15；停止条件：TP 降超 3
      或 F1 不升。
- [ ] **P1.2 twist 真实触发**：twist_pairs=0（真实输入的 FULL 候选未
      通过门禁）。诊断 snapped FULL 线缺失根因并修复。
- [ ] **P1.3 推广 05 分册**（76 DXF FP 最大污染源）：自动 z_window 检测
      （不写死）、图纸端点聚类得螺旋高度、DXF 水平材证据得平台层、
      fan/twist 识别、替换原 2D 投影杆、保留完整 source_handles。
      保守目标：05 DXF FP -25~40，reconstructed TP +20~50。
- [ ] **P1.4 推广 07 → 04 → 02**（02 是横担/塔头，拓扑类型不同最后做）。
      每分册独立 A/B（baseline / 只开该分册 / 全塔），不许一次全开。
- 注：拓扑重建杆属 **reconstructed 口径**（非 pure），代表真实图纸证据
  驱动的空间恢复能力，应作为产品主能力单独报告。

### 批次 3：主腿粒度与纯识别（P2）

- [ ] **P2.1 主腿链**：按四角柱分组 → z 排序 → 母杆链 → 可信平台层切分
      → parent/child 关系（长度守恒审计已有基础 `subdivide_legs_at_levels`）。
- [ ] **P2.2 DXF 平台证据切分**：切分依据只准用 DXF 平台证据/水平材
      交点/节点板标记，**严禁 GT 层位进 pure/reconstructed**。
- [ ] **P2.3 角色专用共线拼接**：当前 collinear_stitch 12 根/TP4（gap=400/
      angle=10°/maxLen=4500/maxSeg=2）。改为分角色：主腿沿锥线多段拼接
      且平台层必断；斜材同 source region+同向+同证据链；水平材严格同层
      同面不跨中心。目标 pure TP +10~25。
- [ ] **P2.4 MLLM keep/drop**：多模态只做结构杆/尺寸线/重复双线/节点板
      轮廓/碎片判别，坐标仍 DXF。优先 05(76FP)/07(35)/04(28)/02(28)。
      目标 recognized FP -30~-60，pure TP 损失 ≤5。
- [ ] **P2.5 hybrid confidence gate 真正阻断**：当前 rejected 候选可能
      仍进入杆件注入（只记账不过滤）。改为 accepted→模型 /
      rejected→review_queue。附阈值曲线（0.3/0.5/0.7/0.85 的 TP/FP/FN/
      Review），重点观察 FP→FN 迁移。
- 瓶颈事实：GT 杆长中位 2005mm vs DXF 模型杆中位 888mm（图纸杆更碎）。

### 批次 4：Precision 清理（P3，不提 pure 但 full P 39.4→43%+）

- [ ] **P3.1 横隔去冗余**（330 根/150TP/180FP）：删除候选=同 z 重复层、
      四面投影重复、端点不落主腿、半宽不符锥线、<2 个不同 source handle。
      目标 FP -60~-100，TP 损失 ≤10。
- [ ] **P3.2 塔头伪横隔**：30m 以上（22/30/32/33/34/36m 层）横担区误生成。
- [ ] **P3.3 清理 24 根全 FP crossarm**。
- [ ] **P3.4 05 分册 76 DXF FP 重点清洗**（与 P2.4 MLLM 联动）。

### 批次 5：泛化盲测（P4）

- [ ] **P4.1 冻结 JC1 参数**（角色阈值/硬编码 z_window/门禁全部不动）。
- [ ] **P4.2 ZC1 盲测**：选结构明显不同的 ZC1（**不要** JC2——骨架几乎
      相同）。直接运行，不改参数。报告 A2-pure / A2-reconstructed /
      FP by sheet / unscorable / 拓扑门禁。
- [ ] **P4.3 盲测纪律**：不允许根据 ZC1 结果重新调参后仍称 blind。

---

## 四、LOD 3 装配 correctness 问题（与 A2 无关，另一线程领域，待修）

> 当前 generate_assembly.py 输出 `degraded=true`（missing
> solid_angle_tower.glb），只能称「节点装配样板」，非完整全塔 LOD 3。

- [ ] **H1 螺栓群世界原点堆叠风险**：`generate_assembly.py` 传
      `plate_center=[0,0,0]`，`bolt_assembly_meshes()` 用节点大样局部孔位
      ——16 组/56 颗可能全叠在原点。必须改为 detail local holes → gusset
      local transform → tower node world transform。验收：螺栓 bbox 与
      对应节点板 bbox 相交；不同组不重叠；螺杆轴与板法向一致。
- [ ] **H2 无证据锚点回退**：`gusset_anchor.py` 无有效 node_id/anchor/z
      selector 时选字典序第一个塔节点（无证据硬挂载）。改为
      failed / review_required。
- [ ] **H3 截面解析静默回退**：`∠100*8`/`100x8`/`L100*8`/`L100X100X10`
      等历史格式解析失败后按角色静默猜（LEG→L100×7）。拆
      recognized/normalized/fallback_section + section_confidence；
      strict 模式解析失败进 review。
- [ ] **H4 凹多边形三角化**：`make_gusset_shell()` 扇形三角化只对凸可靠，
      凹节点板可能三角穿出轮廓/自交。改 ear clipping + 凹多边形测试。

---

## 五、两轮目标（全部以不增容差/不放宽门禁/不用 GT 数值进 pure/reconstructed
/不换评测定义/实验绑定 SHA 为前提）

| 指标 | 现值 | 第一轮 | 第二轮 |
|---|---|---|---|
| pure TP | 54 | 65~80 | 80~110 |
| pure FP | 173 | 120~150 | — |
| reconstructed TP | 58 | 100~140 | 150~220 |
| full TP | 279 | 310~340 | 350~430 |
| full Precision | 39.4% | 43%+ | — |
| full Recall | 26.1% | — | 32%~40% |

（双视图联合口径若采纳为并列报告：现值 477 TP / 44.5% R，同等约束下提升。）

---

## 六、关键代码位置（改错会翻车）

| 事项 | 文件 | 函数 |
|---|---|---|
| 五层口径唯一判定 | `traceability/eval/metrics.py` | `_bar_caliber_class` |
| 2D 投影（front x-z / side y-z） | 同上 | `bars_from_model_2d` |
| 双视图联合口径 | 同上 | `eval_a2_dual_view`（待提交） |
| 代价（d1+d2）与门禁 | 同上 | `segment_cost` / `segment_gates` |
| 06 段斜材拓扑 | `traceability/solve/diagonal_topology.py` | `reconstruct_diagonal_topology` |
| 横隔生成 | `traceability/solve/tower_geometry.py` | `generate_diaphragms` |
| 主腿节间细分 | 同上 | `subdivide_legs_at_levels` |
| GT 平台层开关 | `examples/external/guowang_35A1/layer_overlay.json` | `use_gt_platform_levels` |
| 评测 CLI | `scripts/evaluate_ground_truth.py` | `--tol`（待修 P0.2） |
| 版本钉扎 | `traceability/project/versioning.py` | run manifest |
| LOD 装配 | `scripts/generate_assembly.py` + `traceability/connection/*` | H1~H4 待修 |

---

## 七、历史结论修正记录（防再错乱，摘最新）

| 旧认知 | 修正后 |
|---|---|
| dual-caliber pure = 64 TP | mode="recognition" 混入 25 根非 pure 杆；统一后 54/227 ✅（P0.1） |
| side 视图 = face 过滤即可 | 必须投影到 (y,z)；此前错用 (x,z)（已修，待提交） |
| full 279 可对外汇报 | 含 80% GT 标高辅助，只能内部归因；正式口径 multi.pure 54 |
| 容差=每端点 500mm | 实为 d1+d2<500（和语义），命名需固化（P0.3） |
| A2 提升靠端点吸附/全局拼接 | 06 拓扑解释（投影线→空间拓扑模板）才是有效方向 |

（更早的修正记录见 `docs/UNIMPLEMENTED_PLAN.md` §7 归档。）
