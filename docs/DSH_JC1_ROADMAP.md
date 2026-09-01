# 35A1-JC1 单塔推进计划（给 DSH 评审）

> **日期**：2026-09-01  
> **范围**：先钉死一座塔（35A1-JC1），再泛化到其他塔型  
> **技术路线**：DXF 矢量为主 + 视觉语义补漏（hybrid），不以 GT 坐标污染生产模型  
> **可复现产物**：`web/demo/35A1-JC1/latest_deliver/model.json`（全链路最新）  
> **评测脚本**：`python3 scripts/eval_a2_profiles.py examples/gt/35A1-JC1_ground_truth.json <model.json>`

---

## 一、一句话现状

**几何召回已达标，可信交付未达标。**

我们已经能把 JC1 全塔 3D 骨架拼出来，A2 full 口径双视召回 **99.72%**（S10 最新，TP 1068 / FP 3016 / P 26.2%）；但「从图纸直接读出来的杆」只有 **2.4%**，Harness 交付状态仍是 **failed**（BOM 长度校验未过）。距离官网「从一张图，到可供 AI 使用的工程上下文」，差在 **verified delivery**，不是差在 full recall 数字本身。

> **S10（2026-09-01）横担桁架模板补全**：02 册塔头立面横担外段轨迹散（上弦整族缺失，横担区 40 杆 GT 中 18 FN），
> `complete_crossarm_truss` 按「宽节点 z 簇层位 + 体锥线根部 + 既有弦杆 x 端点 + 03 册端宽 600」诚实推导五站位
> 悬臂桁架（A 根上弦/B 中折/C 尖端/D 根下弦/E 中折下弦），双侧 40 杆确定性重建，根部节点吸附既有塔身角点。
> dual full TP 1050→1068（FN 21→3），front full TP 903→939（R 84.3%→87.7%）。剩余 3 FN 为塔顶地线支架
> （02 册 z≈32000-34500 局部畸变 ±400-700mm，纯几何不可恢复，证据天花板）。幂等性验证通过。

---

## 二、当前指标（必须分清口径，避免误读）

评测对象：`latest_deliver/model.json`，GT：`examples/gt/35A1-JC1_ground_truth.json`，容差 500mm，匈牙利 1:1 匹配。

> **数据源注**：full 行为 S9.1 最新记录（`docs/A2_RECALL_98_PHASE2.md`，TP 1050 / FP 2979）；
> 其余行仍为 f78d2031 同步产物实测（`out/jc1-headline.json`，full 当时 1026/95.8%），
> 分层归因待下次全链路重跑刷新。**注意 `metrics_multi_caliber.json` 是 front 单视口径，
> 与 headline 双视口径并存，引用时认准视图列。**

| 指标 | 视图 | TP / 1071 | Recall | Precision | 含义 |
|------|------|-----------|--------|-----------|------|
| **A2-full** | **双视 front∪side** | **1068** | **99.72%** | 26.2% | 含模板补全 + 标高辅助 + 拼接，**内部归因口径**（S10 代码最新，FN=3 为证据天花板：塔顶地线支架图纸畸变） |
| A2-full | front 单视 | 939 | 87.7% | 23.7% | 单视上限约 80.1%（y_member / depth_diag 投影退化） |
| A2-level_assisted | 双视 | 603 | 56.3% | 51.2% | 含 GT canonical 标高辅助的横隔/细分 |
| A2-parametric | 双视 | 821 | 76.7% | 33.2% | S8 桁架模板补全（K-fan / X 面板 / 塔头链） |
| **A2-pure** | 双视 | **26** | **2.4%** | 21.0% | **纯 DXF 直接识别，对外诚实口径** |

**分层贡献（full = 并集去重）：**

```
（f78d2031 产物口径；S9.1 最新 full=1050，分层待重跑再归因）
pure（DXF 直接识别）           26 TP  →   2.4%
+ reconstructed（拼接/四面展开）  +57
+ level_assisted（标高辅助横隔） +520  →  56.3%
+ parametric（S8 模板补全）     +423  →  76.7%
─────────────────────────────────────────────
full 并集                      1026 TP →  95.8%
```

**已通过的关卡：**

| 关卡 | 状态 |
|------|------|
| 拓扑闭合（`r_topology_closed`） | ✅ |
| 节点三轴解算（`r_node_fully_solved`） | ✅ |
| BOM 截面匹配（`r_bom_section_match`） | ✅ |
| GLB 几何门禁（悬空物理节点 ≤ 4） | ✅（latest review_queue=0；baseline 曾 2） |
| S7 锥线拟合（腿半宽残差中位 ≤ 30mm） | ✅（24mm） |
| GT 泄漏检查 | ✅（`model_has_gt_alignment=False`） |

**未通过的关卡：**

| 关卡 | 状态 | 说明 |
|------|------|------|
| BOM 长度匹配（`r_bom_length_match`） | ❌ | 58 根物理杆超差 3%（旧实例口径 210） |
| 项目级 BOM（`r_project_bom_master`） | ❌ | 项目 Harness 未绿 |
| 件号去重（`r_no_duplicate_bar_id`） | ⏳ | 92 组重复件号（cross_file 合并口径）待核对 |
| **整体 deliver_status** | **failed** | 三条证据链未同时通过 |

---

## 三、与官网终态的差距（诚实版）

官网目标：**接入 → 编译 → Verified Delivery**，加上多页/多视图/多模块统一 + 交互 GLB + 三条证据链（图纸读数 / BOM 核验 / 几何验证）。

| 维度 | 官网要求 | 当前 | 差距 |
|------|----------|------|------|
| L0 权威 3D（GIM） | 100% 真值 | `canonical.glb` 已有 | ✅ |
| A2 几何召回（full） | 高覆盖 | **98.04%** 双视（S9.1；产物 95.8% 待重跑） | ✅ |
| A2 几何识别（pure） | 从图读懂 | **2.4%** | ❌ 核心短板 |
| A2 精度 | 可工程使用 | **28.8%**（FP 2537） | ❌ |
| A1 件号 | 高召回标注 | P≈91% R≈31% | ⚠️ |
| BOM 独立交叉核验 | 长度+截面全绿 | 截面 ✅ 长度 ❌ | ❌ |
| Verified Delivery | Harness 全绿 | **failed** | ❌ |
| 跨视图身份 | front/side/plan 统一 | 双视评测有增益，多投影证据仍弱 | ⚠️ |
| 产品主路径 | A0→A4 hybrid Agent | 默认仍 ezdxf 旁路 | ❌ |
| 全塔高程 | 0–36600mm | latest z=[0,36600]（S8 参数化外推 2591 根，不计 pure）；baseline 仍 6500 起 | ✅（产物口径已闭合） |

**结论**：工程脚手架和 full 召回已到位；**产品价值**卡在 pure 识别、精度剪枝、BOM verified、Agent 主路径切换。

---

## 四、战略原则（请 DSH 确认）

1. **单塔优先**：35A1-JC1 做到 verified 交付，再复制到 JC2 / 其他塔型。
2. **口径诚实**：对外汇报 **A2-pure**；**A2-full** 仅内部归因，不包装成「图纸识别能力」。
3. **矢量为主、视觉补漏**：DXF 提候选中心线 → 视觉/MLLM 做语义分类与补漏，不做 vision-first。
4. **不用 GT 坐标污染生产**：GT 仅用于评测与 canonical 对照；标高辅助层必须显式标注 `level_source=gt_canonical`。
5. **先减后加**：优先删 FP、统一口径、收敛入口；不在识别未达标前堆 3D 特效。
6. **检测阶段关后处理**：06 段 2D 未达标前，禁用 4face 展开 / 交点闭合 / 横隔模板对指标的「刷分」。

---

## 五、分阶段计划

### Phase 0：口径与产物统一（1 周）

**目标**：团队对同一组数字说话，消灭「48% vs 10% vs 95%」混读。

| 任务 | 交付物 | 验收标准 |
|------|--------|----------|
| 冻结唯一权威产物 | `latest_deliver` = 全链路最新输出；`35A1-JC1-baseline` 标注为「P0 无 S8 对照组」 | 文档 + RunManifest 写明 SHA |
| 统一汇报模板 | `scripts/eval_a2_profiles.py` 输出即周报口径 | 四套 profile 每次跑全出 |
| 决策 D1–D3 落盘 | 更新 `JC1_SINGLE_TOWER_PLAN.md` §5 | pure/full/横隔语义三方签字 |

**不做**：改评测语义刷分。

---

### Phase 1：06 段 2D 中心线攻坚（2–3 周）⭐ 最高优先级

**目标**：在 **关 4face / 关模板补全** 的前提下，06 段 A2-pure 达到 **P≥85% R≥85%**（该段 GT 子集）。

| 任务 | 做法 | 验收 |
|------|------|------|
| 矢量高召回候选 | DXF layer 4/1/0 线段 → 双线配对 / 共线合并 / 斜率聚类 | 06 段候选 recall ≥ 95%（对 GT 中心线） |
| 视觉分类补漏 | hybrid agent 对 06 密集区 tile 补漏（仅分类，不从零画杆） | 06 段 pure TP 达标 |
| 子区域缓存契约 | 固定 tile bbox + SHA，禁止整图结果复制到 tile | 同杆不重复计数 |
| 06 段单独评测门禁 | `scripts/diagnose_recall.py --segment 06` | CI 可回归 |

**为什么先 06**：斜材最密、当前 diagonal pure 召回最低，是全塔瓶颈。

---

### Phase 2：五段复制 + 全塔 Z 堆叠修复（2 周）

**目标**：02/04/05/06/07 五段 pure 均达标；全塔 **Z ∈ [0, 36600]**。

| 任务 | 说明 |
|------|------|
| 补 **35A1-JC1-40** 进 `spatial_merge_stems`（~~或 07 参数化延伸到底~~ **已由 S8 base_segment 2591 根实现**，剩 40 册矢量路径一项） | 消除 Z 从 6500 起跳的缺口 |
| 统一 DIMENSION 比例标定 | pixel / drawing / view 坐标链一致 |
| 逐段复制 Phase 1 流程 | 每段独立 manifest + 指标，禁止混批缓存 |
| 五段全过后再开 4face | 4face 只扩已验证的 front 杆，不扩 FP |

**验收**：五段 pure 均值 R≥80%；全塔 Z bbox 覆盖 [0, 36600]±100mm。

---

### Phase 3：FP 剪枝 + 精度提升（2 周）

**目标**：在保持 full R≥95% 前提下，**P 从 28.8% 提到 ≥50%**。

| 杠杆 | 预期收益 | 风险 |
|------|----------|------|
| 锚源目标域收紧（M4a 已验证） | FP −473，TP 持平 | 低 |
| 同源同角色近邻去重 | P +2~5% | 14400/14500 双层端点差 100–250mm，需门控 |
| 图纸证据门控（斜率/位置比对） | 逐面板剪 FP | 中，需逐段验证 |
| 横担外伸端专项（FN 21 根，证据天花板） | R +2.0% | 图纸 |x| 仅到 1445，2200 端或无线段 |

**验收**：dual-view full R≥95% 且 P≥50%；FP < 1200。

---

### Phase 4：BOM + 件号 → Verified Delivery（2 周）

**目标**：`deliver_status: verified`，三条证据链全绿。

| 任务 | 说明 |
|------|------|
| 修 `r_bom_length_match`（58 根超差） | 按物理杆聚合四面镜像；长度来源对齐 BOM 净长 |
| 消减 92 组重复 `bar_id`（cross_file 口径） | A3 关联规则 + 人工 review_queue |
| A1 召回 31% → 60%+ | 跨图层 bar→text 关联（已验证矢量路径 56.8% 可达） |
| A3 杆↔件号绑定 | 每件号唯一对应一根物理杆 |

**验收**：`harness_all_passed=true`；`project_harness_all_passed=true`；web demo 可展示三链全绿。

---

### Phase 5：产品主路径切换（2–3 周）

**目标**：默认走 **hybrid Agent 链**，ezdxf 降为回归旁路。

| 任务 | 说明 |
|------|------|
| `deliver_project(agent_mode="hybrid")` 图册批处理 | Phase 2 of PRODUCT_PATH |
| A0 版面裁切稳定 | 已修空白 crop 问题 |
| Evidence UI | 杆点击 → 原图 overlay + SourceRef |
| `acceptance.sh --with-agent` | 自动化验收 |

**验收**：同一输入，hybrid 路径 A2-pure ≥ ezdxf 路径；steps.json 可追溯。

---

### Phase 6：泛化准备（JC1 通过后）

| 任务 | 说明 |
|------|------|
| Blind tower 评测集 | 无 GT 坐标，仅有 BOM + 图纸 |
| 110kV 基准塔回归 | 验证非国网图层也能跑 |
| 模块 M1–M6 交付 GLB 分色 | 已有骨架，补 verified 状态 |
| 文档与官网对齐 | 三页产品叙事 ↔ 实测指标一一对应 |

---

## 六、里程碑与时间表

```
现在          Phase 0        Phase 1       Phase 2       Phase 3       Phase 4
 │  口径统一   │  06段pure达标  │  五段+全高Z   │  P≥50%       │  verified
 │  (1周)      │  (2-3周)      │  (2周)        │  (2周)        │  (2周)
 ▼             ▼              ▼              ▼              ▼
full R=98.04% 06 pure≥85%   全塔pure≥80%   P≥50%         deliver=verified
pure R=2.4%   关4face/模板   开4face        保持R≥95%     三链全绿
deliver=fail
```

**乐观**：10–12 周 JC1 verified。  
**保守**：+4 周（06 段矢量配对难度超预期、BOM 规则需业务确认）。

---

## 七、需要 DSH 确认的决策

| # | 决策 | 选项 | 建议 |
|---|------|------|------|
| **D1** | 对外主指标用哪个 | A2-pure / A2-full / 双报 | **双报**：pure 对外，full 内部 |
| **D2** | 横隔是否算物理杆进 pure | 改判 reconstructed / 保持 derived | **改判 physical**（模型已生成，仅语义排除） |
| **D3** | GT 标高辅助是否保留在生产路径 | 保留但显式标注 / 评测专用 / 删除 | **评测专用**；生产路径只用 DXF 推导层位 |
| **D4** | S8 模板补全定位 | 生产默认开 / 仅兜底 / 评测不计 | **兜底**：图纸空白区才补，计入 parametric 层 |
| **D5** | 06 段达标线 | P/R 各 85% / 80% / 其他 | **85%**（用户已定） |
| **D6** | 交付验收标准 | full R≥95% 即交付 / 必须 verified | **必须 verified**（BOM 三链全绿） |

---

## 八、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| full 高召回掩盖 pure 低识别 | 对外过度承诺 | 强制双口径周报；demo 分色显示来源 |
| 模板补全 FP 爆炸 | P 长期 <30% | Phase 3 专项目标；图纸证据门控 |
| 06 段矢量双线配对难 | Phase 1 延期 | 先单 segment GT 子集迭代，不跑全塔 |
| BOM 长度规则与工程实践不一致 | Phase 4 卡住 | 早期拉 DSH / 业务方对齐净长 vs 弧长 |
| 两套产物（baseline vs latest_deliver）混读 | 决策反复 | Phase 0 统一权威产物 + SHA 绑定 |
| 横担端 2200mm 图纸无线段 | full R 上限 <100% | 文档诚实记录 FN=21，不硬补 |

---

## 九、资源需求

| 类型 | 需求 |
|------|------|
| 算力 | DXF 全册重跑 ~30min/次；MLLM hybrid 按 sheet 计费 |
| 数据 | 35A1-JC1 全册 DXF（已有）+ GT（已有）+ BOM CSV（已有） |
| 人力 | 1 人主程 + DSH 每周 30min 决策评审 + 必要时 03 节点大样人工抽检 |
| 工具 | `eval_a2_profiles.py`、`diagnose_recall.py`、web demo compare 页 |

---

## 十、本周可执行的三件事（立即可做）

1. ~~跑一遍权威评测留档~~ ✅ 已完成（2026-09-01）：`out/jc1-headline.json` 已生成，
   pure 26/2.4%/21.0%、dual-full 1026/95.8%（f78d2031 产物口径）与本文档一致

2. **确认 D1/D6**（对外报 pure + 交付必须 verified）

3. **启动 Phase 1**：06 段关 4face/模板，只跑矢量候选 + 段内 pure 评测

---

## 附录：关键文件索引

| 文件 | 用途 |
|------|------|
| `web/demo/35A1-JC1/latest_deliver/model.json` | 权威同步产物（f78d2031：full 1026/95.8%；S9.1 代码已达 1050/98.04%，待重跑落产物） |
| `out/35A1-JC1-baseline/` + `out/baselines-frozen/` | P0 对照组（无 S8：canonical_assisted R=31.4% / production_dxf R=19.2%；P0.2 冻结 manifest full R=19.7%） |
| `docs/A2_RECALL_95_MILESTONE.md` | full 95% 达标记录与优化阶梯 |
| `docs/A2_RECALL_98_PHASE2.md` | S9/S9.1 第二阶段 98.04% 突破记录（FN=21 天花板） |
| `out/jc1-headline.json` | eval_a2_profiles 四 profile 权威留档（2026-09-01 实跑） |
| `docs/PRODUCT_VISION.md` | 官网终态定义 |
| `scripts/run_35A1_jc1_full.py` | 全册交付入口 |
| `traceability/intake/hybrid_dxf_agent.py` | hybrid 单张路径 |
| `traceability/solve/tower_geometry.py` | S8 模板补全实现 |

---

*本文档由工程团队生成，供 DSH 评审。指标均为 2026-09-01 实测，后续以 RunManifest SHA 为准。*
