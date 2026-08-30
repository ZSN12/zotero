# P0 正确性收口交付记录

> 本次工作围绕「官网验收标准」做正确性收口：**跨页证据连续、跨视图身份一致、
> 跨模块装配闭合、Agent Harness 状态真实可信**。原则：**不堆新管线，优先修正确性、
> 保留证据链、删除重复执行路径。**

---

## 1. 语义冻结（阶段 0，P0）

构件四态语义已落地并贯穿评测：

| 语义 | 定义 | 进哪些 P/R |
|------|------|-----------|
| `recognized` | front 面直接识别 | recognition P/R |
| `reconstructed` | 确定性重建（含 mirrored 镜像面） | physical P/R，**不进** recognition |
| `derived` | corner_leg / diaphragm / center 轴 | **不进任何** P/R |
| `canonical` | GT 权威塔（gt_aligned / gim） | 仅评测基准，**不进生产** |

实现：`traceability/eval/metrics.py` 的 `is_derived_bar` / `is_recognized_bar` /
`is_reconstructed_bar` / `is_physical_bar` / `is_canonical_bar`；语义标记在
`traceability/intake/tower_symmetry.py` 四面展开时冻结（front→recognized，
b/l/r→mirrored，corner/diaphragm/center→derived）。

## 2. GT 隔离彻底化（阶段 0.2，P0 红线）

**问题**：GT 剖面数值（`gt_tower_half_width` / `gt_crossarm_half_width`）曾**硬编码
在生产四面展开算法里**（`expand_4_face_symmetry` / `inspect_model_topology` 直接调用），
绕过开关，属 GT 泄漏。

**修复**：
- GT 数值函数移入 `traceability/debug/gt_profile.py`（评测/调试专用，生产默认不 import）。
- 生产算法改为显式参数 `half_width_fn` / `crossarm_half_width_fn`，默认 `None`。
- `use_gt_half_width` overlay 默认改 `false`；仅 debug/eval 显式开启，且产物打
  `gt_aligned=True`（正式评测检测到后拒绝，exit 3）。

**代价（已确认接受）**：移除 GT 半宽后，四面展开退回「信任被横担污染的立面图 x」，
3D 重建质量可能下降。这是「GT 只能评测」的必然代价——评测变诚实，分数可能变难看。

## 3. 评测重写（阶段 1，P0）

- **Hungarian 一对一最优匹配**（`scipy.linear_sum_assignment`），非贪心。
- 代价含双端点距离 + 角度 + 长度比 + 共线重叠。
- tolerance sweep（2D 50/100/200/500mm，3D 200/500/800mm）。
- 四套指标**独立**，不混算：
  - `eval_a2_geometry_2d`（2D 几何，recognition 口径）
  - `eval_a1_labels`（件号集合 Exact Match）
  - `eval_a3_association`（几何匹配对上的件号对齐率）
  - `eval_m3_physical_3d`（3D 物理，physical 口径，含 `recall_by_type` 与
    `model_semantic` 分解）

## 4. 装配闭合（阶段 5，P1）

- 提取 `delivery._select_assembly`；`module_definitions` 配置存在但 M1-M6 失败时，
  返回 `assembly_failed=True` + `status="failed"`，**拒绝静默回退 demo**。

## 5. 删除与收敛（阶段 6，P1）

| 项 | 处置 |
|----|------|
| 独立 `run_agent_sheet.py` 业务实现 | 已并入 hybrid Agent 链（统一入口） |
| `pipeline_stages.py` 硬编码 stage id | 已接入（STAGE_LAYOUT/... 常量统一） |
| `tower.glb` / `out/` / `web/demo/**/*.glb` | 已 `.gitignore`，不再作为源码交付 |
| GT 对齐（`gt_align`） | 移入 `debug/`，默认关闭，仅 debug/eval |
| 静默 demo 回退 | 已修复（见 §4） |

## 6. 阶段 7 测试收口（P1）

- `test_eval_metrics.py`：19 个测试覆盖 Hungarian / tolerance sweep / 三态语义 /
  GT 泄漏 / 装配回退 / A1-A3 独立。
- **MLLM 依赖修复**：`test_scan_writes_five_agent_steps`、
  `test_scan_model_pending_review`、`test_cli_compile_drawing_tower_scan` 曾因
  `MLLMBackend()` 捡宿主 API key 走真实 MLLM 而失败/超时。改为显式
  `MLLMBackend(api_key="")` / `--backend rule-based-scan`，走确定性霍夫兜底，
  隔离宿主环境。
- **完整套件：228 passed**（修复前 227 passed / 1 failed）。

## 7. 召回诊断（阶段 2，只定位不调容差）

`scripts/diagnose_recall.py` 按杆件类型 / Z 段 / 长度区间分桶，输出 FN/FP 样例，
支持 `--save` 导出 hard-case 回放数据集。

## 8. 数据切分（阶段 7）

`examples/dataset_split.json`：calibration（演示塔）/ development（35A1-JC1）/
blind_test（空）。**当前所有指标均属 development，禁止同塔调参后对外汇报最终成绩。**

---

## 9. 阶段性进展与诚实指标汇总

### 9.1 阶段 1~3 & 阶段 5 关键修复完成情况

1. **比例尺自动标定模块已落地**：
   - 新增 `scale_calibration.py`，从 DIMENSION 实体自动识别图纸真实比例（单测 17 passed）。
   - 修复 02 图纸将 1:100 单线图与 1:20 结构图混叠的问题，将结构图 region 隔离至 `[34440.0, 34867.0]`，对称原点调整至 `34578.0`。
2. **阶段 5.3 六段塔接口自动缝合**：
   - 在 `tower_views.py` 中实现 `_stitch_multisheet_boundary_nodes`，在 25mm 阈值内自动缝合段边界共享节点，重指杆件引用并清理段间重复横杆。
   - 📌 2026-08-30 订正：当时为「六段」（含 40）。`40` 后实测为加工详图并移除，现为**五段**（02/04/05/06/07）。
3. **阶段 2 证据链完整闭环**：
   - `tower_views.py` 与 `tower_symmetry.py` 完整写入 `projection_refs` 数组、`geometry_class` 与 `geometry_origin`，`bar_inventory.py` 补充真实图纸证据覆盖统计。
4. **阶段 8 测试分层规范**：
   - 引入 `@pytest.mark.slow` / `@pytest.mark.integration` 分层体系，默认快速测试在秒级完成（238 passed）。

### 9.2 关键发现与诚实待办（如实列出）

1. **阶段 3 二维几何召回修复（斜材/塔头）**：
   - 塔头 30–35m 召回与斜材低召回率（5.1%）需要进行线段交点闭合、重叠 crop 去重与防错误 stitching 算法优化。
2. **阶段 4 件号与 A1/A3 关联识别**：
   - 当前件号 Exact Match 仍为 0%，待统一坐标系与引入引线方向/BOM 约束关联评分。
3. **blind_test 集**：
   - 引入独立塔型（如 35C2-SJG1）并单独标注 GT 后启用。

## 10. 比例尺标定与区域隔离说明

详见 [`SCALE_CALIBRATION_DIAGNOSIS.md`](SCALE_CALIBRATION_DIAGNOSIS.md)。国网图纸存在主视图与节点板大样在同张图混叠的情况，必须通过 DIMENSION 实体自动识别大尺寸主标尺并精确切分正立面结构图 region。
