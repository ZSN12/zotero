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
