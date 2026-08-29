# 产品愿景（最终目的）

> 本文档来自官网三张核心介绍页，是这套系统的**最终目的**——不是实现细节，
> 而是「做出来后长什么样、解决什么问题」。所有代码、Skill、Harness、Agent 链，
> 最终都要服务于下面这三件事。
>
> 官网：[仝心圆](https://concentriccirclesmrtt.github.io)

---

## 一句话定位

> **从一张图，到可供 AI 使用的工程上下文。**

工程制图领域，AI 生成的 3D 模型「看着像那么回事」，但没人敢拿去做施工、
检修、改造——因为说不清来源、说不清依据、说不清验证。这套系统要做的，
就是把「一张图纸」变成「可追溯、可验证、可变更管理的工程上下文」。

---

## 页面一：数据管线（DATA PIPELINE）—— 三段式主流程

这是系统的**骨架**。围绕真实使用场景，共同确定数据范围、质量标准、
交付格式和工程验收方式。

### 1. 多源图纸接入（DRAWING INTAKE）
- 接收**扫描图、PDF、DWG、DXF** 等存量资料。
- 保留**文件、版本与原始位置**（SourceRef 证据链的起点）。

### 2. 工程信息编译（ENGINEERING COMPILATION）
- 读取图纸**标注、尺寸与构件关系**。
- 与**物料表（BOM）等工程资料交叉核验**，冲突记为待验证，不悄悄改值。

### 3. 可信结果交付（VERIFIED DELIVERY）
- 由**工程 Agent Harness** 编排专业 **Skills、工具与验证流程**。
- 输出**可验证、能进入 CAD / PLM / 数字孪生 / AI 系统**的工程上下文。

对应到本项目的数据模型：

| 阶段 | 产物 |
|------|------|
| DRAWING INTAKE | `SourceRef`（source_type / reference / confidence） |
| ENGINEERING COMPILATION | `Component` / `Dimension` / `Connection` + BOM 交叉核验 |
| VERIFIED DELIVERY | `Rule` 验证通过 + `staleness=current` + 可导出 GLB/JSON |

---

## 页面二：长上下文重建（LONG-CONTEXT RECONSTRUCTION）—— 核心能力

> **多页、多视图、多模块，仍然重建成同一个结构。**

面对跨页图纸、重复编号、投影重合和分段装配，系统持续保留**构件身份、
跨视图关系和模块依赖**，完成更长链条的结构建模。

这是系统区别于「单张图 OCR」的**本质能力**：

| # | 能力 | 含义 |
|---|------|------|
| 01 | **跨页证据连续**（Multi-page evidence） | 图纸页码、视图区域、标注和版本都进入**同一条证据链** |
| 02 | **跨视图身份一致**（Cross-view identity） | 正视、侧视、剖面和局部大样**共同指向同一个物理构件** |
| 03 | **跨模块装配闭合**（Multi-module assembly） | M1—M6 分段连接、共享节点和依赖状态在整体模型中保持一致 |

右侧交付物：**最新重构 M1—M6 塔段**（白色线框 3D 模型）。
底部标签：**6 个模块 / 多页图纸 / 多视图关联 / 统一三维装配**。

> 这正是「为什么不能只靠 ezdxf 硬解析单张图」的答案：真实铁塔被拆成
> M1–M6 多个塔段、多张图纸、多视图，必须靠**跨页/跨视图/跨模块的
> 身份统一**，才能拼回同一个结构——而不是每张图各出一个孤立的 2D 结果。

---

## 页面三：完整塔头重构（COMPLETE TOWER HEAD）—— 交付形态

> **交互式 GLB / 完整塔头重构。**

最终交付是一个**可交互的 3D 模型（GLB）**，塔头从上到下按模块分段着色
（顶部蓝 → 中部青绿 → 底部金黄），可旋转、缩放、单/双视图切换。

底部三栏是交付的**三个验证维度**，也是 Harness 要保证的三件事：

| 维度 | 英文 | 含义 |
|------|------|------|
| **图纸读数** | DRAWING READINGS | 读取尺寸、标注、构件编号与跨视图对应关系 |
| **物料表核验** | BOM CROSS-CHECK | 交叉核对构件规格、数量、编号与工程资料 |
| **几何求解验证** | GEOMETRY VALIDATION | 检查拓扑连接、几何约束、结构闭合与工程一致性 |

> 三个维度 = 三条证据链：**图纸读数**（SourceRef 来源）、**物料表核验**
> （BOM 独立来源交叉）、**几何求解验证**（Rule 拓扑/闭合/一致性）。
> 三者都过，交付的 GLB 才是「可信」的，而不是「看着像」。

---

## 三者合一：最终目的的一句话总结

```
一张图纸（扫描图/PDF/DWG/DXF）
   └─ 三段式管线（接入 → 编译 → 交付）
        └─ 长上下文重建（多页/多视图/多模块 → 同一结构）
             └─ 可交互 3D（GLB）+ 三条证据链（图纸读数/BOM核验/几何验证）
                  = 可供 AI 使用的、可验证的工程上下文
```

## 对实现的要求（由此推导出的硬约束）

1. **不能只读单张图**：必须支持跨页/跨视图/跨模块的身份统一（`bar_id` 全局一致）。
2. **不能只靠 ezdxf 硬解析**：标注/尺寸/物料表这些「要读懂图里数据」的环节，
   走多模态视觉模型（Kimi 等，可插拔 `MLLMBackend`），而不是正则/坐标硬猜。
3. **BOM 必须是独立来源交叉核验**：BOM 长度/截面与图纸读数并行，偏差→规则 failed，
   不悄悄覆盖。
4. **3D 必须真实可验证**：塔是四棱台对称结构，正立面 → 四向镜像展开（`expand_4_face_symmetry`），
   而非 front+side 伪解耦或 `synthetic_side_from_front` 的 45° 假斜片。
5. **交付前过 Gate**：`tower_geometry_gate` 检查拓扑闭合、bbox、四面网架杆件数，
   不达标就不导出 strict GLB。

---

## 与其它文档的关系

- **实现路径 & Agent 缺口**：见 [`PRODUCT_PATH_AND_AGENT_PLAN.md`](PRODUCT_PATH_AND_AGENT_PLAN.md)
- **Skill + Harness 本质**：见 [`../SKILL_HARNESS_ARCHITECTURE.md`](../SKILL_HARNESS_ARCHITECTURE.md)
- **交付什么**：见 [`DELIVERY_NOTE.md`](DELIVERY_NOTE.md)
- **CAD 里有什么、哪些能直接读**：见 [`DXF_DATA_READING.md`](DXF_DATA_READING.md)
- **README 三阶段管线**：见 [`../README.md`](../README.md)
