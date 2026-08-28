# P0/P1 进展报告：Ground Truth 建立 + 物理杆件重建

> 更新日期：本次会话。目标：把「自验证 100% 贴号」换成「权威 GT 驱动的真实 Precision/Recall」。

## 一、已完成（已提交推送）

### P0 — 权威 Ground Truth ✅（commit `2deb51e`）

**数据源（非手标、非 BOM）：国网官方资料包的 GIM 解析成果 `.mod` + 计算文件。**

| 文件 | 内容 |
|---|---|
| `scripts/build_ground_truth.py` | `.mod` → GT JSON 转换器 |
| `examples/gt/35A1-JC1_ground_truth.json` | **2069 物理杆件 + 1707 节点**（mm 坐标） |
| `scripts/evaluate_ground_truth.py` | Precision/Recall/ExactMatch 评测 |

GT 关键数据：36.6m 塔高、5.5m 塔宽、13 种截面（L40X3~L110X8）、材质 Q345/Q235。
`.mod` 的 3473 根杆段按「端点相接+同截面+同材质」合并成 2069 根物理杆件。

**评测结果（当前基线）：Precision 0% / Recall 0%** —— 诚实暴露了管线输出（71 根碎段）
与 GT（2069 根物理杆件）的巨大差距，取代了之前虚假的「158 根 100% 贴号」。

### P1 — 图元分类 + 分轴比例 + 配准工具 ✅

| 文件 | 内容 |
|---|---|
| `tower_dxf.py::_filter_non_member_segments` | 尺寸线/图框线剔除（bar_layers 优先，避免数字图层重叠误杀） |
| `tower_spec.py::region_scale_xy` | 分轴比例 scale_x/scale_z（国网立面横向/竖向比例不同） |
| `scripts/calibrate_view.py` | GT 反投影自动标定视图 scale/origin（commit `3ac8295`） |

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

- `layer_overlay_jc1_album.json`：front 区域改为 `x[34681,34789]`、
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

### side 立面定位（仍未定论）

GT side 投影（y-z 面）同样是梯形（底/顶宽比 11.75，与 front 相同——
四腿塔 x/y 对称）。但 02 图上**唯一成梯形的簇是簇4（front）**：
- 簇0 x[34355,34416]、簇3 x[34548,34608]：原始 x 展宽 61 单位恒定
  （ratio≈1.0），但**最长线是两腿内收的斜线**（dx=20/dy=293），
  说明它们是「节间放大详图」或「side 拆成左右两段」而非单一梯形立面。
- 用 GT side 投影（y-z）对这些簇做精确匹配是下一步。

### 对称省略 vs 全塔重建

图纸右半（80 根）相对 GT 全塔（941 去重）是 1/12；即使镜面对称补全，
也只覆盖 ~455 根（半塔）。要逼近 Recall 需确认图纸到底省略了多少辅材，
或改用官方 `35A/35A1/35A1-JC1/*.dwg` 全量杆件图。

## 五、环境说明

- 权威数据在 `~/Downloads/输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)/`
- 咸鱼 DXF 是展示图；官方 `35A/35A1/35A1-JC1/*.dwg` + `计算文件` + `GIM` 才是权威
- 测试基线：144 passed（含本次改动，无回归）
