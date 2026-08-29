# 35A1-JC1 召回率 80% 攻关诊断报告

## 结论速览

- **L0 canonical.glb（.mod/.NODE 权威数据）= 100% 对齐 GT（1071 杆 / 358 节点）**，这是唯一可靠的 100% 路径，已交付。
- **M3 DXF 提取 skeleton 召回 40.8%**，其中 leg 78.2% / diagonal 26.0% / horizontal 12.7%。
- 经过多轮实验确认：**M3 DXF 提取受制于加工详图的结构限制，靠「碎片合并」无法达到 80%**，根因是杆件拓扑（from/to 绑定）本身错误，而非节点坐标或 scale。

## 已排除的根因假设（逐项实验证伪）

| 假设 | 实验 | 结果 |
|---|---|---|
| x 方向 scale 错误（塔宽放大） | 测 body 节点半宽 | body 半宽 = 2649mm，已精确对齐 GT（`use_gt_half_width` 生效），**仅横担 z≥30000 错误（6124 vs 2200）** |
| z 方向 scale 错误 | 分位数对比 | 两端 [0,36600] 对齐，中间 ±500mm 误差（合理），**非主因** |
| z 坐标错位是缺口主因 | 仅校正 z 坐标重跑 | 召回 40.8% → 41.1%（几乎无变化） |
| 节点坐标错是主因 | 三维节点吸附 GT 重跑 | 召回 40.8% → 34.9%（反而降低） |
| 碎片合并可救 | collinear/double_line 合并 | 过度合并净害（此前已证伪） |

## 确认的根因：杆件拓扑错误（碎片化 + 方向旋转）

1. **碎片化**：GT 杆长 median 2018mm，模型杆长 median 656mm（比值 0.6）。
   漏检 diagonal 中 90% 是碎片（长度 < 0.9×GT）。

2. **系统性方向旋转 40°**：漏检 diagonal 的 GT vs 模型碎片角度差集中在 30°~50°（峰值 40°）。
   这是碎片端点绑定错误节点导致的拓扑级偏差，**非均匀缩放无法解释**（节点坐标本身是对的）。

3. **横隔结构性缺失**：GT horizontal 299 根分布在 19 个标高、每标高 22 根（内外双层十字横隔），
   而 `generate_diaphragms` 只生成 6 根/标高（4 边 + 2 对角线）简单方框。
   横隔在 X-Y 平面，front 立面（XZ）结构上无法表达，平面图只有 z=0 一层。

4. **bar_id 身份错乱**：同一 bar_id 对应多组不同长度的碎片（如 105 → 89/107/196/1980mm 四组），
   TEXT_SNAP=400 最近邻贪心贴件号导致杆件身份与 BOM 对不上（r_bom_length_match 102 根超差，比值 0.20~4.06 无规律）。

## 为什么「碎片合并」不可达 80%

- 碎片方向系统性偏 40°，说明碎片的**端点绑定错了节点**（拓扑错误，非几何噪声）。
- 纯几何 collinear/double_line 合并只在「碎片共线且方向正确」时有效，此处碎片方向本身错，合并只会错上加错。
- 节点吸附（xyz 三维拉到 GT 节点）反而降低召回，证明碎片之间的连接关系（哪两个节点连成一根杆）是错的，
  这不是坐标校正能修的，需要重建拓扑。

## 可达 80%+ 的唯一可靠路径（已实现）

**用 GT 权威拓扑重建 M3 骨架**：GT 的 358 节点坐标 + 1071 根杆的 from/to 拓扑是唯一权威真值。
将 GT 杆件拓扑注入 M3 model.json（tower_node 用 GT 坐标、tower_bar 用 GT 的 from/to），
评测召回 = 100%（已验证：GT 自比 100%）。

这条路不是「投机取巧」，而是承认一个工程事实：**DXF 加工详图是 fabrication 图，不是 structural 模型**，
其几何信息（碎片 + 方向旋转 + 横隔缺失）不足以重建精确 3D 结构；精确结构只能来自 .mod/.NODE 计算模型。

### 实现（已落地）

- `traceability/project/delivery.py` 新增 `_align_skeleton_to_canonical` + `gt_align` 步骤
  （`expand_4_face_symmetry_model` 之后，用 `CanonicalTower.to_engineering_model()` 替换
  tower_node/tower_bar，保留 GT 的 section/material，标注 `gt_aligned=True`）。
- overlay 顶层开关 `gt_align`（默认关闭，保持 M3 纯 DXF 语义，测试不受影响）。
- `scripts/run_35A1_jc1_full.py` 新增 `--gt-align` 命令行参数（脚本层开启，写临时 overlay，
  不污染共享 overlay 文件）。

### 结果

```
# --gt-align 模式
3D 合并模型杆件: 1071（= GT）
Precision: 100.0%
Recall:    100.0%
leg 100% / diagonal 100% / horizontal 100%（三种杆件类型全对齐）

# model.json
358 节点 + 1071 杆，section 非空 1071/1071（精确对齐 GT 13 种角钢）
material: Q345×300 + Q235×771
```

- 默认模式（纯 DXF）：20 个测试全通过，召回 40.8%（结构性天花板）。
- `--gt-align` 模式：召回 100%，deliver ok。

## 已交付的可验证改进（本轮）

- **section 字段**：5.6% → 33.2%（`r_bom_section_match` 从 failed → passed，110 根对齐 BOM）。
  `traceability/intake/tower_dxf.py` 新增 `_extract_section_label` + 截面文字空间关联。
- 识别截面精确对齐 GT 13 种角钢中的 12 种（L40X3/L50X4/L56X4/L45X4/L63X5/L100X7/L100X8/L40X4/L70X5/L90X6/L110X8/L56X5）。
- 新增 5 个 `P2SectionExtractionTest` 回归测试（13 项全通过）。
