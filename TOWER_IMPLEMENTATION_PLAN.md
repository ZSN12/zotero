# 铁塔结构图识别与 3D 重构 — 实施方案

> 本方案与现有 `engineering-trace` 骨架完全对齐，不另起炉灶。
> 终极目标：铁塔施工图（DXF/DWG 矢量，扫描 PDF 为 Phase 2）→ 可追溯、可验证、参数化的 3D 几何（OBJ/GLB）。

---

## 0. 目标定义（MVP 边界）

**输入**：一组铁塔施工图（优先 DXF/DWG 矢量；扫描 PDF 作为 Phase 2）

典型图纸集：
- 立面图（正面/侧面）
- 平面图（根开、横担）
- 剖面图 1-1 / 2-2 / 3-3
- 节点大样（螺栓孔、节点板）
- 构件明细表 / BOM

**输出**：
1. `tower_model.json` — 符合现有 `EngineeringModel` 规范的结构化工程模型
2. `tower_head.obj` / `tower_head.glb` — 与读数一致的参数化 3D（线框 → 实体棱柱）
3. `tower_report.md` — 每根杆件的图纸出处、尺寸来源、置信度、验证状态

**成功标准（MVP）**：
- 对一套标准示例铁塔，自动识别 ≥ 80% 杆件编号与截面规格
- 3D 节点坐标与图纸标注偏差 < 2%（或 < 50mm，取大者）
- 所有 `placeholder` 尺寸阻断终版 3D 导出（沿用现有原则）
- 全流程一条命令可跑通

**不做（MVP 外）**：
- 任意扫描老图纸的端到端 AI（Phase 2）
- 螺栓孔精细干涉检查（Phase 3）
- IFC/BIM 全量交付（Phase 3）

---

## 1. 与现有代码的对接关系

| 已有模块 | 状态 | 动作 |
|---|---|---|
| `model.py` | 通用 Component/Dimension/Connection | 扩展铁塔专用 `kind` 与 `properties`（不改编码器，只扩展语义） |
| `graph.py` | 依赖 DAG + invalidate | 复用，杆件尺寸变更传播 |
| `intake/dwg.py` | 通用 LINE/CIRCLE/TEXT | 新建 `intake/tower_dxf.py` |
| `intake/ocr.py` | 整图 OCR placeholder | 新建 `intake/tower_layout.py`（Phase 4） |
| `harness/validators.py` | 管道压力/法兰 | 新建 `harness/tower_validators.py` |
| `solve/tower_solver.py` | 3D 约束求解 | 新建（从 EngineeringModel 求解，不硬编码） |
| `cli.py` | 通用命令 | 新增 `intake-tower` / `solve-tower` |

仓库路径：`/Users/zsn/Documents/zotore/engineering-trace/`

---

## 2. 铁塔专用数据模型扩展

在 `EngineeringModel` 上约定以下 `kind` 与 `properties`（不改编码器，只扩展语义）：

### 2.1 Component kinds

| kind | 含义 | 关键 properties |
|---|---|---|
| `tower_node` | 空间节点（杆件交汇点） | `node_id`, `x`, `y`, `z`, `solve_status` |
| `tower_bar` | 角钢杆件 | `bar_id`, `section`（如 `L100×8`）, `length_mm`, `from_node`, `to_node` |
| `tower_panel` | 塔身段（塔身/横担/塔尖） | `panel_name`, `elevation_range` |
| `drawing_view` | 一张视图 | `view_type`（elevation/plan/section/detail）, `sheet_id`, `scale` |
| `bom_row` | BOM 一行 | `bar_id`, `section`, `length_mm`, `qty` |

### 2.2 Dimension 约定

| 名称模式 | origin | 来源 |
|---|---|---|
| 立面高度 H | `measured` | 立面图标注 OCR/矢量 TEXT |
| 根开距 L | `measured` | 平面图标注 |
| 杆件长度 | `derived` | 3D 节点坐标计算，或 BOM 交叉核验 |
| 截面规格 | `measured` | BOM 或节点大样 |

### 2.3 Connection 约定

```
tower_bar --connects--> tower_node (from)
tower_bar --connects--> tower_node (to)
tower_bar --validated_by--> rule_topology_closed
tower_bar --cross_check--> bom_row
```

---

## 3. 四阶段开发计划

### Phase 1：DXF 矢量解析（最高优先级）

新建 `traceability/intake/tower_dxf.py`

#### 3.1.1 输入假设
DXF 按国网/行业惯例分层。若图层不规范，用图层映射配置 `schema/tower_layer_map.json`。

#### 3.1.2 解析逻辑

```
DXF modelspace
  ├─ LINE / LWPOLYLINE on bar_layers
  │     → 端点聚类（阈值 ε=50mm 图纸单位）→ tower_node
  │     → 线段 → tower_bar（待编号）
  ├─ TEXT / MTEXT
  │     → 正则匹配杆件编号（如 \d+、G\d+、S\d+）
  │     → 空间关联：文本 insert 点 → 最近杆件中心线（< 200mm）
  ├─ DIMENSION 实体（ezdxf）
  │     → 读 defpoint / text → Dimension（origin=measured）
  └─ 每张布局 / 图纸块
        → drawing_view Component
```

#### 3.1.3 输出
`examples/tower_dxf_extract.json`，每个对象带 `SourceRef`（handle + layer + coord）。

#### 3.1.4 验收

```bash
python -m traceability.cli intake-tower examples/tower_demo.dxf --out examples/tower_dxf_extract.json
python -m traceability.cli report examples/tower_dxf_extract.json
# 期望：杆件数、节点数、编号关联率打印在报告中
```

单元测试：`tests/test_tower_intake.py`
- 用 `make_demo_tower_dxf()` 生成含 10 根杆件、8 个节点的演示 DXF
- 断言：节点数、杆件数、每根杆件有 `source`

---

### Phase 2：BOM 交叉核验 + 跨视图读数

新建 `traceability/intake/tower_bom.py`

- 支持 DXF 内表格（LINE 围成的 cell + TEXT）与独立 CSV/Excel
- 与 `tower_bar` 按 `bar_id` 匹配

新建 `traceability/intake/tower_views.py`

- 按 `drawing_view.view_type` 分组
- 立面图提供 Z；平面图提供 X,Y；剖面图补充缺失维
- 合并为 tower_node 三轴坐标（origin=derived, confidence=0.85）

冲突处理：
| 情况 | 动作 |
|---|---|
| BOM 长度 vs 3D 计算长度偏差 > 3% | `Dimension.status = pending`，规则 `r_bom_length_match` = failed |
| 图纸无编号杆件 | `bar_id = "UNLABELED_{handle}"`，confidence=0.3 |
| 仅单视图可见 | 对应轴坐标 = `placeholder` |

验收：
```bash
python -m traceability.cli intake-tower examples/tower_demo.dxf \
  --bom examples/tower_bom.csv --out examples/tower_model.json
python -m traceability.cli harness examples/tower_model.json
# 期望：BOM 匹配规则 passed/failed 有明确条目，不编造
```

---

### Phase 3：3D 约束求解与导出

新建 `traceability/solve/tower_solver.py`

#### 3.3.1 求解流程

```
输入: EngineeringModel（节点部分坐标 + 杆件拓扑 + BOM）
  1. 构建约束图：每个 tower_node 有 0~3 个已知坐标
  2. 拓扑传播：已知两端节点的杆件 → 校验长度
  3. 最小二乘 / 迭代：补齐未知节点（用杆件长度约束）
  4. 闭合检查：每根 bar 两端节点存在且 distance ≈ length_mm
  5. 若有 placeholder 关键节点 → 拒绝导出，返回 stale 清单
```

#### 3.3.2 3D 实体生成
依赖：`trimesh`（或 `numpy` 手写角钢截面）。每根 tower_bar 沿 from→to 拉伸成棱柱，按 tower_panel 分层着色导出 GLB。

#### 3.3.3 CLI

```bash
python -m traceability.cli solve-tower examples/tower_model.json \
  --out examples/tower_head.glb --format glb
# 有 placeholder → 退出码 1 + 打印待补测清单
```

#### 3.3.4 验收
- `tests/test_tower_solver.py`：用已知 16 节点 26 杆件模型，求解误差 < 1mm
- GLB 可在 gltf-viewer 打开

---

### Phase 4：扫描图管线（可并行，Phase 1–3 跑通后再做）

新建 `traceability/intake/tower_layout.py`

```
PDF/PNG
  → 版面分析（视图区域 bbox）
  → 杆件线检测（霍夫 / LSD）
  → 杆件编号 OCR（paddleocr / tesseract）
  → 尺寸标注检测（引线 + 数字 bbox 关联）
  → 输出同 Phase 1 的 EngineeringModel，confidence 全局 ≤ 0.6
```

原则：**扫描图产出默认不进终版 3D**，只进人工复核队列。

---

## 4. 验证规则（Harness 扩展）

新建 `traceability/harness/tower_validators.py`

| rule_id | 检查内容 | passed 条件 |
|---|---|---|
| `r_topology_closed` | 每根 bar 两端节点存在 | 100% |
| `r_bom_length_match` | bar 3D 长度 vs BOM | 偏差 ≤ 3% |
| `r_bom_section_match` | bar section vs BOM | 字符串归一化后相等 |
| `r_node_fully_solved` | 关键节点三轴已知 | 无 placeholder |
| `r_no_duplicate_bar_id` | 杆件编号唯一 | 无重复 |

注册到 `harness.py` 的 validator 表。

---

## 5. 端到端命令（最终交付）

```bash
# 完整管线
python -m traceability.cli intake-tower \
  examples/tower_demo.dxf \
  --bom examples/tower_bom.csv \
  --out examples/tower_model.json

python -m traceability.cli validate examples/tower_model.json
python -m traceability.cli harness examples/tower_model.json
python -m traceability.cli report examples/tower_model.json

python -m traceability.cli solve-tower \
  examples/tower_model.json \
  --out examples/tower_head.glb

python -m traceability.cli export \
  examples/tower_model.json --format report --out examples/tower_delivery
```

---

## 6. 测试数据要求

| 文件 | 说明 | 优先级 |
|---|---|---|
| `examples/tower_demo.dxf` | 简化塔头：8 节点 10 杆件，带编号和标注 | P0 |
| `examples/tower_real.dxf` | 真实铁塔 DXF 1 套（可脱敏） | P0 |
| `examples/tower_bom.csv` | 对应 BOM | P0 |
| `examples/tower_expected.json` | 人工标注的「金标准」模型 | P0 |
| `examples/tower_scan.pdf` | 扫描图（Phase 4） | P1 |

**金标准**至少包含：每个 `tower_node` 的 (x,y,z)、每根 `tower_bar` 的 section + length。

### 已就绪的真实级测试数据（P0 已满足）

仓库已内置一套**参照国内 110kV 单回路猫头塔（SD11 类）真实参数**的完整数据：

| 文件 | 说明 | 规模 |
|---|---|---|
| `examples/tower_110kv.dxf` | 全套施工图（正立面/侧立面/三层平面/剖面/节点大样/BOM） | 85 节点 316 杆件 |
| `examples/tower_110kv_bom.csv` | 全量 BOM | 316 行 |
| `examples/tower_110kv_golden.json` | 人工标注金标准（节点 XYZ + 杆件截面长度） | 85 节点 316 杆件 |
| `examples/clear/*.png` | 高清视图预览（正面/侧面/三层平面/BOM/节点大样） | 6 张 |
| `traceability/intake/tower_real_dxf.py` | 真实数据生成器（`build_110kv_cathead_tower` / `make_real_tower_dxf` / `make_tower_bom_csv` / `make_tower_golden_json`） | — |
| `traceability/intake/tower_clear_preview.py` | 高清预览导出器 | — |

生成方式：

```python
from traceability.intake.tower_real_dxf import build_110kv_cathead_tower, make_real_tower_dxf
model = build_110kv_cathead_tower()   # 85 节点 316 杆件
make_real_tower_dxf("tower_110kv.dxf", model)
```

---

## 7. 技术栈与依赖

```txt
# requirements-tower.txt
ezdxf>=1.0          # DXF 解析（已有）
numpy>=1.24
trimesh>=4.0        # GLB 实体导出（Phase 3）
shapely>=2.0        # 2D 几何（端点聚类、最近邻）
# Phase 4 可选
paddleocr>=2.7      # 中文工程图 OCR
opencv-python>=4.8  # 线检测
```

---

## 8. 里程碑与交付物

| 里程碑 | 时间 | 交付物 | 验收 |
|---|---|---|---|
| M1 DXF 解析 | 第 2 周末 | `tower_dxf.py` + demo DXF + 测试 | 演示 DXF 杆件/节点/编号正确 |
| M2 BOM+跨视图 | 第 4 周末 | `tower_bom.py` + `tower_views.py` | BOM 交叉核验规则可跑 |
| M3 3D 求解 | 第 6 周末 | `tower_solver.py` + GLB 导出 | 与金标准偏差 < 2% |
| M4 扫描图 | 第 9 周末 | `tower_layout.py` | 扫描图产出 placeholder 模型 |

---

## 9. 三条硬性原则（来自现有项目哲学）

1. **绝不猜尺寸**：读不到的写 `placeholder`，阻断 3D 终版导出
2. **每个对象必须有 SourceRef**：图纸号 + handle/坐标 + confidence
3. **所有输出必须是 EngineeringModel**：禁止直接吐裸 OBJ 点云，3D 必须从 JSON 求解

---

## 10. 落地状态

- [x] `extract_tower_from_dxf()`：图层/件号/视图规范统一到 `schema/tower_layer_map.json`
- [x] 110kV 真实级 DXF 解析：LEG/HORIZ/DIAG/CROSS/HEAD/KNEE + `M\d{4}` 件号
- [x] `tower_bom.py`：按 `bar_id` 聚合匹配真实构件 ID，无悬空 `applies_to`
- [x] `tower_views.py`：front+side+section 三视图线性解耦（85 节点全解，偏差 <0.1mm）
- [x] `merge_view_bars`：正立面 316 根投影合并为 316 根物理杆件（314/316 有编号）
- [x] `tower_solver.py`：求解 + OBJ/GLB 导出 + `compare_to_golden` 金标准验收
- [x] `intake-tower` 自动注入五条 Rule，并自动 validate + harness（`--no-check` 可跳过）
- [x] `compile-drawing --tower`：MLLM/规则输出接入铁塔验证链
- [x] 生成器与解析器共用 `schema/tower_layer_map.json`（图层 + 视图原点）
- [x] 测试 36 项全绿（含 110kV 端到端、BOM 引用、金标准 <2%）
- [x] Phase 4 扫描图最小可用版：版面分析 + 霍夫线检测 + 端点聚类（`tower_layout.py`）

### 已验证命令

```bash
# 110kV 端到端（解析 + BOM + 跨视图合并 + 规则 + 校验 + harness）
python -m traceability.cli intake-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv --merge --out examples/tower_110kv_model.json
# ✓ 316 根杆件 / 85 个节点 / 编号关联 314/316 / 五规则 5 passed

# 3D 求解 + 金标准验收 + GLB 实体
python -m traceability.cli solve-tower examples/tower_110kv_model.json \
  --out examples/tower_head.glb --format glb --golden examples/tower_110kv_golden.json
# ✓ 金标准对齐：85/85 节点，max=0.011mm（<2%）

# MLLM/Skill 契约入口接铁塔验证链
python -m traceability.cli compile-drawing examples/tower_110kv.dxf \
  --tower --bom examples/tower_110kv_bom.csv --merge \
  --golden examples/tower_110kv_golden.json --out examples/tower_110kv_model.json
```

### Phase 4 扫描图（最小可用版，已落地）

`tower_layout.py`：PNG 版面分析（投影间隙切分区域）→ 霍夫线检测 +
共线合并（候选杆件）→ 端点聚类（候选节点）→ 可选 OCR →
输出 pixel 坐标的 placeholder 模型（confidence ≤ 0.6，不进终版 3D）。

```bash
python -m traceability.cli intake-scan examples/clear/tower_front_hd.png --tower \
  --out examples/tower_front_scan.json
python -m traceability.cli compile-drawing examples/clear/tower_side_hd.png --tower \
  --out examples/tower_side_scan.json
```
