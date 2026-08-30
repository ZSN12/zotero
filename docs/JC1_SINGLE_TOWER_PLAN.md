# 35A1-JC1 单塔修复执行计划（修订版）

> 基于《35A1-JC1单塔修复计划》+ 2026-08-30 归因实测修订。目标与执行原则不变：
> 只做 35A1-JC1；先修正确性再提高召回；一次只改一个阶段；每阶段输出前后指标；
> 不注入 GT、不放宽容差、不加假杆；derived/helper 不进 P/R；BOM 只核验不覆盖。

## 0. 对原计划的评估结论

**方向正确，予以采纳**：单塔聚焦、阶段门禁、"停止后处理破坏"优先、候选中心线+视觉分类的
检测范式、可见杆人工 GT——都对。三处修订：

1. **阶段 1.1 的"分桶节点 ID 覆盖"假设不成立**（见 §1 归因）。dz>8m 的真因是
   **合并阶段 region→z_offset 错配**（06 段节点被映射进 04 段 Z 范围）+ **图纸角部
   图号章被当成杆件**。阶段 1.1 改为 region→z 映射 fail-closed 审计。
2. **Z 锚点映射（原阶段 5.1）的影响前移**：全塔 734mm 累积漂移 + 06/05 边界 16500–17000
   泄漏说明 z_offset 本身就不准，是 A2 直接根因之一。阶段 1 先做"越界钳制+剔除"止血，
   阶段 5 做锚点正解。
3. **A2 必须评四面展开前的模型**（`jc1_complete_front.json`），基线 684 根 recognition
   杆与 2736 物理杆的关系（684×4）已确认，展开是纯 4 倍复制，A2 口径不因展开失真，
   但仍按原计划以展开前模型为准。

## 1. 阶段 1.0 归因记录（2026-08-30 实测，已完成）

| 项 | 实测 |
|---|---|
| dz>8m 杆 | 484 根 = **213 根唯一根杆 × 4 面展开**（06 段 440、07 段 44） |
| 06 杆引用节点 z 分布 | 双峰：**11000–17000（正确）** + **25000–30000（04 段 23–30m 的范围！）** |
| 伪杆样例 | `bar_JC1_front`：图纸角部 205×285 图面单位的小区域（origin [34445,-10495]），图号章文字 "JC1" 被 TEXT_SNAP 贴为件号 |
| 合并前 06 模型 | 全部节点 z=None（纯 2D），无 bar_JC1_front ⇒ 污染全部发生在 merge 阶段 |
| 同名节点 ID 冲突 | 0（原计划 1.1 假设的覆盖不存在） |

**归因结论**：(a) 06 的部分 region/节点在合并时被赋了 04 段的 z_offset（z=25000–30000 簇）；
(b) 图号章/小示意区域混入 front region，产生 `bar_JC1_front` 类伪杆；(c) 06 顶部
16500–17000 泄漏到 05 段范围，z_offset 本身有累积偏差（≈734mm，另一线程实测）。

## 2. 修订后的阶段执行表

### 阶段 0：冻结可信基线（进行中）
- 0.2 运行清单 `run_manifest.json`（run_id/SHA/阶段计数/缓存标记）→ 子代理 69a114a3 实现中，
  白名单 `delivery.py` / `processing_graph.py` / 新建 `run_manifest.py` / `test_run_manifest.py`
- 0.3 基线冻结 → `docs/baseline/jc1_baseline.json`（含口径与命令，已完成）
- 验收：同输入确定性阶段哈希一致；评测命令唯一化（全部走 `scripts/evaluate_ground_truth*.py`
  + `diagnose_recall.py --miss-report`，禁止口径漂移）

### 阶段 1：停止后处理破坏（2 天）
临时关闭：`enable_4_face_expansion` / `close_face_intersections` / `stitch_boundaries` /
`add_diaphragms` / `snap_diagonals`（注意：`add_diaphragms`、`weld_corner_legs` 现在是函数
参数不是 spec 键，需补 spec 通道）。
- **1.1'（替代原分桶修复）**：merge 后杆件来源段门禁 fail-closed——每根杆两端 z 必须落在
  source_sheet 的 z_range 内（interface_bar 除外），越界剔除 + 计数进 manifest/review_queue；
  region→z_offset 查表加断言：一个 sheet 的 z_offset 必须与该 sheet 段范围一致，不一致报错
  而非静默采用
- 1.2：拆分杆 `root_bar_id`/`derived_from`/`split_index`，原杆与拆分杆不共存（213 根污染杆
  的 root 归并验证正好用它）
- 1.3：杆属性补 `source_sheet`/`source_z_range`/`interface_bar`（40↔07↔06↔05↔04↔02 邻接
  才允许接口杆）
- 1.4：测试（越界剔除、拆分去重、06 杆不连 29m、关闭展开后 front 几何不变）
- **验收**：dz>8m 484→0；跨段错连=0；悬空引用=0；关闭后处理后跑 miss_report 对照
  （区分"后处理造成"vs"识别本身"的 FN/FP，作阶段 2 的干净起点）

### 阶段 2：只攻克 06 段（5–8 天，关键路径，原估 3–5 天偏乐观）
按原计划：region 拆分（正立面/大样/剖面/BOM/标注，根治 `bar_JC1_front` 类污染）→ 节间
切片（按平台/节间 3–5 块，重叠 10–15%，带局部 Z 范围）→ 缓存按 crop_sha/bbox/prompt_sha
失效 → **候选中心线 + 视觉分类**（DXF 高召回线段→双轮廓配对→中心线候选→MLLM 逐候选
判定；作为 hybrid 的 geom_method 模式落地，不建第三条链；每 crop 一次批量调用返回候选
数组，控成本）→ 通长斜材重建（X 交叉默认不是节点）→ 06 可见杆 2D 人工 GT。
- 验收：06 段 P≥85%、R≥85%（可见杆口径）；碎片长度分布接近可见杆；无 <300mm 碎片堆积；
  无同杆重复识别

### 阶段 3：其余五段（5–7 天）
顺序 05→07→02→04→40。02 塔头分开投影、禁 abs(x) 当深度、横担不无条件四面复制；
04/40 重点清双轮廓重复/尺寸线/大样/短碎片。每段独立 P/R，可见杆 R≥80%。

### 阶段 4：比例尺（3 天）
DIMENSION 生成标定报告（显示值/测量距/DIMLFAC/方向/中位数/离散度/置信度）；
hybrid 与 ezdxf 共用同一 region scale；证据不足 → `scale_status: review_required`。

### 阶段 5：拼完整二维立面（3 天）
Z 锚点映射（段底/段顶结构锚点→z_offset/z_span，锚点优先级：标高尺寸>接口横杆>主腿端点>
平台节点）替代 `z_global = z_offset + view_y`；只允许邻接模块拼接；门禁后出
`jc1_complete_front.json`。**此阶段产出即 A2 正式评测对象**。

### 阶段 6：可信 3D（5–7 天）
按原计划 6.1–6.5。现状已满足：四态语义、derived_from/projection_refs 悬空校验、
GT 半宽隔离打标；待补：symmetry_rule_id（JC1_SQUARE_4FACE）、half_width(z) 分平台拟合
证据、横担专用处理、recognized front 坐标不变回归测试。

### 阶段 7：件号与 BOM（2–3 天）
A1 以 BOM 图纸件号为 GT（禁 PM_XXXX 对数字件号）；A3 多条件评分（view/引线方向/距离/
长度/截面/合法件号/模块范围），不确定 → `association_status: review_required`。
现状基线：A1 P=82.3%/R=56.9%；A3 30 对中 2 对（6.7%）。

### 阶段 8：分阶段评测与交付
按原计划 8.1–8.3。评测对象与口径固定：
- A1：BOM 口径（`evaluate_ground_truth.py --bom`）
- A2：`jc1_complete_front.json` recognition 杆 vs GT 投影（严格 Hungarian，不放宽）
- M3：物理杆（recognized+reconstructed），derived 排除，附 precision_by_face
- 诊断：`diagnose_recall.py --miss-report`（FN 五类/FP 三类）
- 目标：第一目标 A2 R≥70%/P≥50%、M3 R≥50%/P≥30%、跨段=0、证据链有效率=100%；
  第二目标按原计划。区分：直接识别 / 对称重建 / 官方模型 / 缺失待复核。

## 3. 每阶段反馈模板
修改文件 / 删除的旧逻辑 / 前指标 / 后指标 / 新增测试 / 测试结果 / 是否用缓存 / 是否用 GT /
未解决问题 / 下一阶段进入条件是否满足。

## 4. 阶段 1 执行记录（2026-08-30）

- 1.1' `enforce_source_segment_gate`（tower_views.py）+ JC1 六段 `DEFAULT_MODULE_Z_RANGES`
  （tower_views.py），四面展开前调用（tower_batch.py），报告进 merge_report。
- 1.2 `close_face_intersections` 拆分杆加 `root_bar_id` / `split_index` / `split_count`
  溯源；原杆只截断不残留整根。
- 1.3 物理杆写 `source_sheet` / `source_z_range` / `interface_bar`。
- 1.4 测试：test_segment_gate.py(8) + test_tower_4face_reconstruction.py 新增 3
  （拆分溯源 / 递归拆分 root 保持 / front x/z 展开不变）。
- 真实数据门禁前后：dz>8m 484→0；A2 P@500 4.4%→5.5%、FP 654→479；剔除 708 根
  （06:652 / 07:56），864 个越界端点全部 z≥20000，0 边界漂移误伤。
- 验收复核：门禁后残留 28 根"跨段"杆 = 28/4=7 根唯一构件，全部是**合法段接口杆**
  （跨段 dz 仅 2–5.6m，如 40 段 z 246→5853 跨 5500 分界、05 段 z 19806→23045 跨
  23000 分界），非污染（污染为 dz 9m+ 深度错位）。这些是阶段 5 模块拼接要显式
  处理的对象，届时打 `interface_bar=True`，不属阶段 1 剔除范围。
- **阶段 6 归档（待修）**：`expand_4_face_symmetry_model` 在传入拟合
  `half_width_fn` 时，`|leg_x| == half_width` 的左右主腿会被角腿去重合并成单根
  （solve 层 `expand_4_face_symmetry` 无此问题，问题在包装层与拟合半宽交互）。
  真实塔 `|leg_x| ≈ half_width` 正是常态，阶段 6 主腿重复/缺失修复必须覆盖此 case。

## 5. 阶段 2 归因修正 + 首项修复（2026-08-30）

**归因修正**：早前把 `bar_JC1_front` 归为"图纸角部图章区域"，经重查 06 的
`view_regions`（205×285，与兄弟段 05/07/04/40 同构，非异常小区域）后更正——
该区域**就是正立面段**，`bar_JC1_front` 是**正立面区域内的图名文字 "JC1"**
（`35A1-JC1` 的片段）被兜底件号正则 `[A-Za-z]{0,3}\d{1,5}` 命中后 TEXT_SNAP 贴到
400mm 内最近杆件所致。

**首项修复（已提交 707f400）**：`_stem_designation_tokens` + `_extract_bar_label`
的 `exclude_tokens` 参数，排除图号中「既含字母又含数字」的片段（JC1/SJG1/35A1/
35C2）。真实数据 06 重解析：`bar_id='JC1'` 0 根、图号片段件号 0 根。

**z 污染（25000-30000）的真正入口**（待阶段 5 根治）：`close_face_intersections`
（overlay 已启用 `intersection_snap_tol_mm=30`）+ `stitch_boundaries`（默认 True）
在**跨 sheet** 的合并模型上把不同 sheet 的节点按坐标容差合并（如 07 杆
`bar_UNLABELED_51A_front` 端点吸附到 06 节点生成共享节点 N00084，z=31753），
把某 sheet 的错误 z 传播到相邻 sheet。阶段 1 门禁已 fail-closed 兜底（dz>8m→0），
阶段 5 锚点映射 + 跨 sheet 合并按 source_sheet 隔离是根治。

## 6. 阶段 2.2 关键归因：region 裁剪是二维召回低的第一杠杆

**决定性证据链**：hybrid 生产路径（`hybrid_dxf_agent.py` L328-340）用 overlay
`view_regions` 的 bbox **裁剪图片喂给 MLLM**——MLLM 只看到 region bbox 内的像素。
region bbox 太窄 → 立面右半/材料表之外的真实杆件 MLLM 根本看不到 → 直接造成
A2 二维召回偏低（基线 R@500=2.8%）。

**审计结果（`scripts/audit_regions.py`，已提交 a6a0360）**：6 段声明 region 对
结构图层 LINE 的覆盖全部 <80%，且 x 直方图暴露多栏布局（立面分栏 + 材料表 +
标题栏）：

| 段 | 覆盖 | 声明 x 范围 | 实际结构线 x 范围 |
|---|---|---|---|
| 40 | 76% | 34415-34585 | 34364-34738 |
| 07 | 77% | 34350-34590 | 34354-34763 |
| 06 | 56% | 34445-34650 | 34449-34936 |
| 05 | 52% | 34380-34555 | 34312-35052 |
| 04 | 41% | 34410-34545 | 34422-34900 |
| 02 | 32% | 34540-34620 | 34355-34867 |

**下一步（2.2 收尾）**：逐段确定「立面栏 vs 材料表/标题栏」的精确分界（x 直方图
已给簇边界，如 06 的 34450-34600 / 34700-34750 / 34850 三簇），把 front region
拆成覆盖真实立面的一个或多个 bbox，排除材料表/标题栏。此改动直接影响 MLLM 输入，
须逐段目检确认分界后再写 overlay。

### 2.2 修正（渲染目检后部分推翻上表）：06 的 region 是正确的

渲染 06 成 PNG 目检 + 三簇 y 跨度/TEXT 内容分类：

| 簇 | x 范围 | y 跨度 | TEXT 内容 | 身份 |
|---|---|---|---|---|
| 簇1 | 34450-34650 | 293（全高） | 尺寸数字 1962/3124/1256 | **正立面**（region 覆盖 ✓）|
| 簇2 | 34650-34800 | 170（局部） | 件号 513/516 + 截面 + 螺栓 | 节点大样（region 正确排除）|
| 簇3 | 34800-34936 | 382 | kg / Q235 / 表头中文 | 材料表（region 正确排除）|

- v1 朴素覆盖率把大样+材料表误报成"立面被裁"，**06 的「56%」是假警报**（上表该行作废）。
  交叉印证：pre-merge 06 的 view_x [-1974,1960]mm = 196.7 图面单位 @scale20，
  恰好落在声明 region 宽 205 内——立面完整。
- `audit_regions.py` 升级 v2 簇感知（区域外按 50 桶算 y 跨度：全高桶=立面被裁
  **或**全高表格须目检甄别；局部桶=大样/表应排除）。v2 下 06 的 needs_split
  仍是表格误报（表格全高 382 > region 高 293）。
- **教训**：立面+大样+表格共用图层时朴素覆盖率不可用，必须簇感知+目检。

## 7. 三层归因链（合并外部分析，含逐层核实状态，2026-08-30）

A1 件号 P=83.0%/R=56.9% 而 A2 R@500 仅 2.8% 的三层链条：

**第1层 Intake 区域裁剪**（外部分析称丢 40%~70% 构件）：
- **06 段已被我方目检推翻**（见 §2.2 修正：丢失的是大样+材料表，立面完整）。
- **其余 5 段未核实**，且 v2 簇感知显示区域外确有全高内容：
  40: 31 根@桶[34650-34700]，07: 87@*[34600-34750]，05: 320@*[34700-35050]，
  04: 307@*[34600-34700]，02: 559@*[34350-34400 + 34700-34750]（**两侧**都有）。
  外部判断 02 右侧含"完整侧立面"、04 含"横担大样与并排立面"、05 有
  34400 主立面/34750 侧立面/34950 辅助剖面三栏——与全高簇位置吻合，
  **须逐段渲染目检+TEXT 分类定论**（立面=件号+尺寸链；表格=kg/规格表头；
  大样=局部 y 跨度+螺栓孔）。

**第2层 多栏立面未拆分**（front/side 并排被当单一 front）：与第 1 层同源，
v2 全高簇即候选栏。若证实，须按外部方案拆 kind: front / side(elevation_b)，
`tower_dxf.py` 多 region 分通道提取并写 view_type/face/projection_refs。
（注意：`cross_file_views.synthetic_side_from_front=false`、`side: null`，
当前流水线本就没有侧立面输入——若 02 右栏真是侧立面，它是**从未被提取过**
的整块召回。）

**第3层 空间拼接累计漂移——已精确证实**：
- 模型节点 z ∈ [-0.1, 35866.2]，GT [0, 36600] → **ΔZ=733.8mm** ✓（复算一致）
- 模型节点 x ∈ [-2338.1, 2338.1]，GT ±2762 → **ΔX=423.9mm** ✓
- 评测是严格 Hungarian 端点硬门禁：整体偏移 >500mm 时拓扑全对也判 ∞ 代价
  （不匹配）。**这是 A2 R=2.8% 的直接放大器**：哪怕第 1/2 层修好，漂移不除
  召回仍上不去。第 1 层门禁（dz>8m）只除粗污染，不管这个系统性的米级漂移。

## 8. 四阶段优化计划（采纳外部 Phase 1-4，含我方修正）

**Phase 1 Region 拆分与全覆盖**（召回主力）
1. 先逐段渲染目检（render_dxf_preview_with_mapping + 在 region 边界切图 +
   簇 TEXT 分类），定论 5 段区域外全高簇身份（立面/大样/表格）。
2. 按结论改 `layer_overlay.json`：真立面栏 → 纳入/拆为多 region（front 与
   side/elevation_b 分 kind）；表格/大样 → 保持排除。06 预计**不改**。
3. `audit_regions.py` v3 增加 TEXT 分类（自动判簇身份），目标各段立面覆盖率 ≥90%。

**Phase 2 视图角色分离**：front/side 分通道提取，写 view_type/face/
projection_refs(region_id)；侧立面杆进 side 通道参与四面展开（而非混入 front
造成伪杆）。

**Phase 3 接口高程与半宽对齐**（除 734mm 漂移）：
- 分段接口缝合改用 `stitch_segment_boundaries()` 以段底/段顶重合主材节点为基准
  对齐相邻段相对 Z，替代硬编码 z_offset 累计。
- 主腿外轮廓斜率拟合：各段自下而上按主腿倾角推导真实半宽与 Z 增量
  （与 §4 归档的阶段 6 角腿 bug 修复合并处理）。

**Phase 4 全量回归**：`scripts/run_35A1_jc1_full.py` + `pytest tests/`（303+）。
A1 保持 P≥80%/R≥55%；A2 R@200/R@500 预期随对齐显著提升；3D 门禁悬空节点下降。
原则不变：不注入 GT、不放宽门禁、不改测试标准。




