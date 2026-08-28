# P0/P1 进展报告：Ground Truth 建立 + 物理杆件重建

> 更新日期：本次会话。目标：把「自验证 100% 贴号」换成「权威 GT 驱动的真实 Precision/Recall」。
> **架构已重构为 L0/L1/L2（见文末）**：完整铁塔 3D 只来自 L0 CanonicalTower（GIM），DXF 只做 L1 图纸索引。

## 一、已完成（已提交推送）

### L0 CanonicalTower — 权威几何（唯一 3D 真值）✅（commit `df3ce0a` / `e6adb9d`）

**数据源（非手标、非 BOM）：国网官方资料包的 GIM 解析成果 `.mod` + 计算文件 `.NODE`。**
完整铁塔 3D 以 GIM 为唯一来源，不再从 DXF 施工图「发明」3D。

| 文件 | 内容 |
|---|---|
| `traceability/solve/canonical_tower.py` | L0 权威几何：`{nodes, bars, units:mm, up:Z}` schema；从 `.mod`/`.NODE` 加载；只走正确渲染路径导出 GLB/线框 OBJ |
| `scripts/build_ground_truth.py` | `.mod` + 计算 `.NODE` → 单座标准 30m 呼高独立塔 GT（剔除 8 塔重叠） |
| `examples/gt/35A1-JC1_ground_truth.json` | **1071 物理杆段 + 358 节点**（mm 坐标），**单连通子图、严格门禁通过** |
| `scripts/evaluate_ground_truth.py` | front 投影 Precision/Recall（L1 配准可量化，不作为完整塔 3D 门禁） |

GT 关键数据：36.6m 塔高、5.5m 塔宽、z∈[0,36600]mm。标准 30m 呼高组合（Body2+Leg8），
从 `.mod` 原始 2069 根（8 塔重叠的"鸟巢"）提纯为单座独立塔 1071 根，**全连通**。

### GLB 实体化根因修复 ✅（commit `df3ce0a`）

- **`_align_matrix` 变换矩阵写反**：世界基向量被塞进矩阵【行】，而 trimesh 按【列】
  当基向量用，导致杆轴（局部 Z）不对准 from→to。改为 `column_stack([x,y,z])`。
- **`_angle_steel_mesh` 网格原点在杆一端**：截面沿局部 Z 从 0 拉到 length，平移却对准
  中点 → 每根杆再偏移半杆长。改为局部 [-L/2,+L/2] 居中，杆两端精确落节点（偏差 0.000mm）。
- **`classify_members` 倾角符号漏判**：`_inclination_deg` 带符号，自上而下的腿倾角为负，
  用 `abs(incl)` 后主腿不再漏判。

**验收：GT 完整塔肉眼是塔（四棱台格构），节点处杆端汇交；`gt_reference.glb` 正确导出。**

### DXF 施工图 — L1 DrawingIndex（图纸索引，只做 2D）✅（commit `df3ce0a`）

DXF 只产出：视图 region、图层角色、文字/尺寸、BOM 件号；**不作为完整塔 3D 来源**。
其「合成 side / 四面展开 / 门禁放宽」属启发式，仅在无 GIM 时（Phase 4）才作为独立产品线。

### P1 — 图元分类 + 分轴比例 + 配准工具 ✅

| 文件 | 内容 |
|---|---|
| `tower_dxf.py::_filter_non_member_segments` | 尺寸线/图框线剔除（bar_layers 优先） |
| `tower_spec.py::region_scale_xy` | 分轴比例 scale_x/scale_z |
| `scripts/calibrate_view.py` | GT 反投影自动标定视图 scale/origin |

## 二、关键发现（坐标对齐，P1 核心难点）

### 35A1-JC1-02 的视图布局被 overlay 定义错了

- **真实布局**：02 图是**多个视图簇横排**，bar 层线中点 x 分成 6 簇（间隙 >15 单位）：
  - 簇0 x[34357,34414]、簇1 x[34442,34449]、簇2 x[34470,34495]
  - 簇3 x[34550,34606]、**簇4 x[34681,34789]（=front 立面）**、簇5 x[34820,34860]
- **overlay 原始定义**：front 框在 x[34340,34530]、side 框在 x[34533,34698]
  —— 把簇0~3 当成 front/side，而**真正的 front 立面（簇4）完全没被框到**。

### 配准工具自动确认：簇4 = front 立面

用 GT front 的梯形特征（底宽/顶宽比）自动匹配：

| 簇 | 底/顶宽比 | 判定 |
|---|---|---|
| 簇0 | 0.90 | 方向反（顶宽>底宽），非 front |
| 簇2 | 1.18 | 非梯形 |
| **簇4** | **6.93** | **最接近 GT 11.75，判定 front** ✅ |

标定结果：`scale_x=50.2, scale_z=85.1, origin=(34735, -7244)`。

### 为什么单一 scale_ratio 会差 8 倍

国网立面图**横向（塔宽）与竖向（塔高）是不同比例**：
- 塔宽 5524mm 对应图纸 110 单位 → scale_x ≈ 50
- 塔高 36600mm 对应图纸 430 单位 → scale_z ≈ 85
- 单一 scale_ratio=10 对两个轴都用 10，横向差 5 倍、竖向差 8.5 倍。

## 三、已完成（本轮）

### P1 碎段合并已接入管线 ✅

- `tower_spec.py::collinear_merge_config`：按 stem 读共线合并参数
  （`colinear_tol=2.0 / gap_tol=30.0 / max_angle_deg=8.0`）。
- `tower_dxf.py::extract_tower_from_dxf`：region 赋值后、节点聚类前，
  **按 view_type 分组做共线合并**；合并后按整根杆长再过滤 `min_bar_len`
  （碎段本身短，min_bar_len 不能前置，否则误杀）。
- `tower_spec.py::region_scale_ratio`：无 `scale_ratio` 时回退
  `scale_x×scale_y` 的几何平均（各向同性代理），修复分轴比例下
  min_bar_len / eps 换算返回 1.0 的 bug。

**效果**：front 立面 284 碎段 → 82 根物理杆件（median 长度 5.0 → 695mm），
退化杆从 28 → 8，重复件号组从 28 → 20。

### 坐标标定已落地 ✅

- `layer_overlay.json`：front 区域改为 `x[34681,34789]`、
  `scale_x=50.2 / scale_y=85.1`、`origin=(34735,-7244)`、`z_flip=true`。
- 重建后 front 节点 view_x ∈ [-2686, 2689]（GT x ∈ [-2762, 2762]）、
  view_y ∈ [3.8, 36620]（GT 高 36600）——**坐标已对齐 GT**。

### 评测：真实 Precision/Recall（从 0% 起步）✅

`scripts/evaluate_ground_truth.py` 升级：GT 投影按端点去重（2069→941 根
去重杆件）、模型按 view_type 过滤（front 评测不混入 side）。

| 指标 | 基线 | 本轮 |
|---|---|---|
| Precision | 0% | **58.8%**（47/80） |
| Recall | 0% | **5.0%**（47/941） |

Recall 低是**结构性的**：图纸只画了塔的**右半**（关于 x=34735 镜面对称，
左半省略），且只覆盖部分杆件；80 根重建杆对应 941 根全塔去重杆的自然上限。

## 四、未完成（下一轮重点）

### side 立面定位（结论：塔 x/y 对称，side ≈ front，可合成）

**02 立面图只有簇4（front）一个梯形，没有独立 side 立面**：
- 簇0 x[34355,34416]、簇3 x[34548,34608]：4 条内收长腿（底宽61/顶宽22），
  是「节间放大详图」而非 side 梯形立面。
- 官方 DWG 全量（ODA 转换 `/tmp/oda_out/`）：01-1 总装图 layer 0 的
  cl1/cl2 是 A1 图框矩形（841×594），非塔几何；01-2 是材料/螺栓 BOM 表。

**关键定量结论**：GT 塔 x/y 近似旋转对称（节点反射 85.5%、旋转 84.6%），
把 front 立面旋转 x→y 能恢复 **89%（826/928）的 side 立面杆件**：
- 漏掉的 102 根 = 横担（cross-arm）在 x 方向伸出、y 方向不伸出的不对称部分
- 多出的 115 根 = front 横担在 side 投影里不存在

**下一步**：启用 overlay 的 `synthetic_side_from_front=true`（塔 x/y 对称，
`side_x_scale≈1.0`），移除 02 的假 side region（指错簇），让
`_synthesize_side_nodes_from_front` 合成 side 节点 → 打通 front+side 3D 解耦，
使 GLB 门禁（bbox 跨度 >2800mm）通过。横担的 11% 误差由 GT 评测揭示，接受为
「合成侧视」的已知局限。

### 对称省略 vs 全塔重建

图纸右半（80 根）相对 GT 全塔（941 去重）是 1/12；即使镜面对称补全，
也只覆盖 ~455 根（半塔）。要逼近 Recall 需确认图纸到底省略了多少辅材，
或改用官方 `35A/35A1/35A1-JC1/*.dwg` 全量杆件图。

## 五、Phase 2 收尾（跨文件 3D 合并 + GLB 门禁打通）✅

### 配置统一（消除两套矛盾 overlay）

- **revert** 未提交的 `layer_overlay.json`（旧假双 region x[34340,34698] +
  scale_ratio=10 + synthetic_side=false，指错簇）。
- 把已标定的 `layer_overlay_jc1_album.json` 内容**合并进 `layer_overlay.json`**，
  删除重复文件。所有脚本/测试统一指向 `layer_overlay.json`。

### view_kinds 补 'side'

- `_synthesize_side_nodes_from_front` 合成 side 节点后，把 `'side'` 补进
  `drawing_file.view_kinds`（此前缺 side，`require_front_and_side` 门禁误判）。

### GLB 门禁（现行，L0 架构）

- **完整塔以 L0 CanonicalTower（GIM）为准，DXF 只做 L1 图纸索引**。
- GT（标准 30m 呼高单塔）：**单连通子图（100%）、严格门禁通过**；杆端-节点偏差 0.000mm。
- DXF 管线输出（02 图右半塔碎片）**不放松门禁去换取 `ok=True`**；门禁如实失败，
  `deliver ok: False`，因为 02 图本身只编码了 ~61 根杆、右半塔、无辅材，不足以组成完整塔。
- `tower.glb` 只作为图纸索引/溯源产物，不是完整塔交付。

### 测试对齐

- `test_canonical_tower.py`：L0 schema、导出、**GT 通过严格门禁** 断言。
- `test_tower_geometry.py`：`_align_matrix` 杆轴映射 + 杆端落节点 <1mm 回归。
- 全量 **172 passed**。

## 六、环境说明

- 权威数据在 `~/Downloads/输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)/`
- 咸鱼 DXF 是展示图；官方 `35A/35A1/35A1-JC1/*.dwg` + `计算文件` + `GIM` 才是权威
- 测试基线：172 passed（含本次改动，无回归）
