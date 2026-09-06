# Phase 2c 意图路由接线：分类驱动管线选择（2026-09-04）

> 模块：`traceability/intake/intent_router.py`（Phase 2c 交付物）
> 接线点：`tower_spec.view_regions()`（单点）+ 三个管线入口注册
> 测试：`tests/test_intent_router.py`（19 用例）；全套 pytest **774 passed / 5 skipped**（2b 时 755）
> 结果：**JC1/ZC1 剥离全部意图声明（kind/axes）后，意图分类补挂的 regions
> 与原 overlay 声明逐字段等价，merge-stem 集合完全一致**——Phase 2 验收条件
> 「无 per-stem 手工 overlay 端到端跑通且指标不回退」的路由层已达成。

## 1. 架构：单点接线，全局生效

意图分类不直接改管线分支——它补挂 `view_regions`，让全部既有判定函数
自然变为意图驱动：

```
管线入口（intake_tower_batch / build_project_from_directory /
         _build_hybrid_project）
    └─ register_sheet_intents(dxf_paths, overlay)     ← 一次性、幂等、进程内缓存
         └─ classify_batch_intents（Phase 2b 判据链，缓存落 out/sheet_intent/）
              └─ 逐 stem 合成 view_regions → 注册表
tower_spec.view_regions(stem, overlay)                ← 唯一接线点
    ├─ overlay 声明带 kind/axes → 原样返回（字节级不变）
    ├─ 声明缺 kind/axes（Phase 2e 副本）→ 意图补挂，几何继承声明原值
    └─ 无声明（第三梯队）→ 聚类合成（无 scale/z）
         ↓ 下游全部自动意图驱动（零改动）：
    sheet_is_spatial_mergeable / sheet_role_for_stem / cross_file_merge_stems
    resolve_drawing_kind / extract_tower_from_dxf（B6 兜底自动失效）
```

## 2. 三条接线原则（顺序不可变）

1. **overlay 声明优先**：stem 有带 kind/axes 的声明时意图完全不干预。
   JC1 47 stem / ZC1 6 stem 的 committed overlay 在注册前后**逐字节相同**
   （单测 `TestRealOverlaysUnchanged` 断言 json.dumps 相等）——红线零风险。
2. **只补意图**：剥离实验副本（声明缺 kind/axes）时，意图补挂 kind/axes，
   **几何全部继承声明原值**（origin/region/scale_x/scale_y/z_offset/
   z_span_mm/z_axis_up——这些是标定与塔级路由，不是意图）。国网版式多视图
   按位置定序：首区 front、次区 side、第 3+ 区保守 detail（不产杆件）。
3. **不产 z/比例**：聚类合成路径（无声明 stem）不含任何 z_offset/
   z_span_mm/scale——scale 由既有的 DIMENSION 比例标定（calibrate_region_
   scales）补，z 塔级路由（cross_file_views.sheets + z 堆叠）保持
   overlay/人工通道。图纸内 z 歧义有实测证据：ZC1 07 册标注带
   [5482,11292] 与 12 册 [10500,18814] 重叠，不可从图纸自证。

## 3. 剥离实验（Phase 2e 验收的路由层前置验证）

剥离 `kind`/`axes`（保留全部几何/标定/z_offset）后意图补挂 vs 原 overlay：

| 图册 | stem | 分类意图 | 补挂 regions | 几何继承 | mergeable |
|---|---|---|---|---|---|
| JC1 | 02/04/05/06/07 | assembly_elevation_front | front(+side) | SAME·scale keep·z keep | True ×5 |
| JC1 | 00-1/00-2/01-1/01-2/03 | fabrication_detail | detail/空 | — | False ×5 |
| ZC1 | 05/08/09/10/12 | elevation(front/side 标签互换) | front+side 双区 | SAME·scale keep·z keep | True ×5 |
| ZC1 | 07（单立面册） | elevation | front 单区 | SAME | True |

merge-stem 集合与 committed overlay **完全一致**：
JC1 {02,04,05,06,07,35C2-SJG1-ML}、ZC1 {05,07,08,09,10,12}。

注：`35C2-SJG1-ML`（plan 册）不在本批 DXF 目录（JC1 交付范围 D4 过滤），
其 plan 意图路径由单测 `test_plan_intent_first_plan` 覆盖。

front↔side 标签互换（ZC1 05/07/08 判 side、09/10/12 判 front）是 Phase 2b
已知接线等价：都映射 sheet_role=elevation；同册 front/side 的区分由
`_infer_assembly_views` 的 x 中位切分（B6 路径）或剥离副本的「首区 front/
次区 side」位置定序（本 Phase）完成——意图只负责「是立面」。

## 4. 聚类合成路径（第三梯队通用化）

无任何声明的 stem 走聚类合成（`_synth_from_clusters`）：

* **孪生立面判据**（front→front+side 拆分）：两显著塔形簇高度差 ≤10%
  （真并排立面同塔同基准：ZC1 05/08/09/10、JC1-02 实测高度差 0~0.4%；
  JC1-07 塔段+右侧大样高差 26% 被拒）+ x 区间不重叠 + 次簇线数 ≥25%
  主簇 + 次簇跨度 ≥40% 主簇；
* detail/plan：主簇 bbox 单 region；
* 显著簇门槛 = max(8, 30%×最大簇线数)——与 sheet_intent 的
  `_CROP_COMPONENT_RATIO` 语义一致。

## 5. run_manifest 审计块

`deliver_project` 落盘 `run_manifest.json` 新增 `sheet_intent_routing` 块
（`registration_report()`）：

```json
{"registered": true, "n_sheets": 16,
 "elevation_stems": ["35A1-JC1-02", ...],
 "plan_stems": [...], "detail_stems": [...],
 "intents": {"35A1-JC1-02": {"intent": "assembly_elevation_front",
             "confidence": 0.87, "reason": "MLLM 视觉判图：...", ...}}}
```

Phase 2e 验收时可直接从交付目录复核每张图「为什么进了/没进空间合并」。

## 6. 验收（Phase 2e 剥离全管线端到端）

`scripts/run_phase2e_stripped.py <jc1|zc1>`：生成 intent 剥离副本
（`layer_overlay.phase2e-stripped.json`，写回**源 overlay 同目录**——
overlay 相对引用如 `crossarm_headless_bom: full_bom.json` 从
overlay.parent 解析，见 tower_symmetry 的候选链）、独立 out-dir
（`out/phase2e/<tower>-deliver`）跑全管线，与 committed overlay 基线红线比对：

| 红线 | JC1 基线 | JC1 剥离 | ZC1 基线 | ZC1 剥离 |
|---|---|---|---|---|
| A2-front-full TP | 913 | **913 ✓** | 223 | **223 ✓** |
| A2-front-full P/R | 27.1% / 85.2% | **27.1% / 85.2% ✓** | 8.6% / 78.2% | **8.6% / 78.2% ✓** |
| dual-view-reconstructed TP | 1067 | **1067 ✓** | 258 | **258 ✓** |
| dual-view-pure TP | 304 | **304 ✓** | 9 | **9 ✓** |
| A1 件号识别 | 168/197 P=100% | **168/197 ✓** | 190/202 P=100% | **190/202 ✓** |
| skeleton/assembly/canonical GLB | — | **sha256 相同 ×3** | — | **sha256 相同 ×3** |
| model.json 组件属性 | — | **0 差异** | — | **0 差异**（4775 组件） |

逐图 sheet JSON 的全部差异（JC1 998 / ZC1 9373 处）均为输出目录
路径引用（`_dxf_scope/*.dxf` 绝对路径等），无任何几何/属性实质差异。
ZC1 首跑曾 TP 223→199，根因是剥离副本写到 `out/phase2e/` 导致
`full_bom.json` 解析失败、横担头退化 hw_fallback——副本必须与源
overlay 同目录，已在脚本内固化。

## 7. 兼容与风险控制

* 特征缓存版本化（`_FEAT_VERSION=2`）：components 新增 bbox 字段，旧缓存
  自动失效重算（不再需要手动清缓存——此前判据链改动要手动删
  out/sheet_intent/*.json，这次起新版本号内建失效）；
* 注册失败（DXF 读不了/分类异常）在三个入口均被捕获，回退旧行为
  （B6 兜底），绝不杀跑批；
* `_REGISTRATIONS` 按 overlay 身份（resolved path / dict id）隔离 +
  DXF 列表签名幂等：一次管线只分类一次，多入口重复注册零成本。
