# 全塔 DXF 源数据发现（Round 47）

## 关键发现

在 GT 的源目录中发现了一个**全塔 DXF 文件**，它比当前管线使用的碎片化分册 DXF 好得多：

**文件**: `/Users/zsn/Downloads/输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)/计算文件/35A/35A1/35A1-JC1/35A1-JC1.dxf`（1.8MB，AC1015）

## 语义分层（GBK 编码，需 latin1→gbk 解码）

| 图层 | 类型 | 数量 | 含义 |
|---|---|---|---|
| `受力材杆件` | LINE | 561 | 主材（承力）杆件 |
| `受力材原始杆件` | LINE | 529 | 主材原始杆件 |
| `辅助材原始杆件` | LINE | 601 | 辅材原始杆件 |
| `受力材节点号` | TEXT | 689 | 主材节点编号（311 唯一）|
| `辅助材节点号` | TEXT | 581 | 辅材节点编号 |
| `受力材规格` | TEXT | 350 | 主材截面规格（如 L70x5）|
| `辅助材规格` | TEXT | 314 | 辅材截面规格 |
| `杆件结果信息层` | TEXT | 350 | 杆件内力结果 |

## 与 GT 的匹配度

- **节点号 87% 匹配 GT**（311/358 节点在 DXF 中直接对应，节点号如 100、101、110、200、220、300 与 `.NODE` 文件完全一致）
- 缺失的 47 个节点（203、223、243、1003、1303…）疑似「背面第四角」在 2D 立面/侧视图中不显示

## 当前管线的问题根源

当前 `batch-jc1/dxf/` 的 6 张分册图是**碎片化图纸**（杆件被拆成 1-2 单位碎段、无节点号、layer 0 混杂），而全塔 DXF 有：
1. **节点号锚点**（可直接重建杆件连通性）
2. **主材/辅材语义分层**（无需猜测哪些是主材）
3. **截面规格**（section 信息齐全，可做件号匹配）

## 下一步（待评估）

写一个全塔 DXF 专用解析器：
1. 读取 `受力材节点号` + `辅助材节点号` TEXT，建立 node_id → 坐标映射
2. 读取 `受力材杆件`/`受力材原始杆件` LINE，按端点与节点号匹配重建杆件
3. 用 `受力材规格` TEXT 关联 section
4. 坐标变换：y 轴翻转 + 尺度换算（DXF y=366 → GT z=36600mm，即 1 单位 = 100mm，y 翻转）

这比继续微调分册 DXF 的 cluster_eps/layer 0 更有价值——预期能把节点对齐从 40% 提到 85%+。

---

# 决定性发现：GT 的正确来源是 GIM `.mod`，不是 DXF（Round 48）

## 核心结论

**GT（358 节点 / 1071 杆）不是从 DXF 图纸提取的，而是从 GIM `.mod` + 计算 `.NODE` 结构化文件生成的。**

`scripts/build_ground_truth.py` 的生成流程：
1. `parse_mod()` 解析 GIM `.mod`（`P,<node>,x,y,z` 节点 + `R,<from>,<to>,<section>,<material>` 杆段）
2. `parse_node_file()` 用 `.NODE` 的 358 个节点 ID 提纯「标准 30m 呼高单塔」（剔除 .mod 中 8 种呼高重叠的冲突杆件）
3. 提纯后 = **358 节点 + 1071 杆段（恰好 = GT）**

## 关键文件（均已在代码库中存在）

| 文件 | 作用 |
|---|---|
| `traceability/solve/canonical_tower.py` | `load_from_mod()` / `load_gt()` / `export_glb()` |
| `scripts/build_ground_truth.py` | 从 `.mod`+`.NODE` 生成 GT JSON |
| `.mod` 文件 (367KB) | GIM 结构化模型（1707 节点 / 3473 杆段，8 塔重叠）|
| `.NODE` 文件 (16KB) | 标准 30m 单塔的 358 节点 ID |

## 验证结果

```python
load_from_mod(mod, node_file=node, merge=False)
# → 358 节点 (100% 坐标对齐 GT，0 偏差)
# → 1071 杆段 (= GT 1071)
# → Z [0, 36600], X/Y ±2762  ← 恰好是 objective 的数值目标
```

## 为什么之前 7 轮 DXF 优化只有 ~40% 召回

`canonical_tower.py` 的模块 docstring 早已写明设计意图：
> 「一种几何只从一个源来：完整铁塔 3D 只来自国网 GIM `.mod` / 计算 `.NODE`」

而 `run_35A1_jc1_full.py --agent-mode ezdxf` 用的是 `batch-jc1/dxf/` 的 **6 张碎片化分册图**（杆件拆成 1-2 单位碎段、无节点号、layer 0 混杂），这是根本性错误的数据源。

## 正确的交付路径

- **L0 canonical.glb**（已存在）：`load_gt(gt_json)` → 100% GT 对齐（358/1071）
- **M3 skeleton.glb**（DXF 提取）：~40% 召回，受碎片化源图限制
- 目标「对齐 GT 358/1071」应走 `.mod` 路径（`load_from_mod`），而非 DXF 提取

## 交付产物状态（out/35A1-JC1-full-deliver/）

| 产物 | 内容 | GT 对齐 |
|---|---|---|
| `canonical.glb` (690KB) | L0 权威完整塔（.mod/.NODE）| **100%** |
| `skeleton.glb` (1215KB) | M3 DXF 提取骨架 | ~40% |
