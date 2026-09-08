# 工程图纸到可信三维几何与工程上下文编译系统 (Engineering Traceability & 3D Reconstruction)

> **终极目标**：把多视图工程施工图（扫描图/PDF/DWG/DXF）编译为**可追溯、可验证、参数化、可交互的 3D 几何实体（Interactive GLB/CAD）与工程上下文**，支撑施工、检修、改造与数字孪生。

---

## 1. 核心问题与痛点定义

传统的 AI 生成 3D 模型（基于扩散模型或 NeRF/Gaussian Splatting）产出的是非结构化的表面网格（Mesh），在真实工程场景中存在四个致命缺陷：
1. **来源不可溯**：说不清这根构件/梁柱来自哪张图纸的哪个视图。
2. **尺寸不可信**：无法区分尺寸是实测值、设计值、规范查表值还是算法臆测。
3. **连接未验算**：空间杆件是否闭合、螺栓孔位是否匹配、拓扑连接是否成立缺乏校验。
4. **无变更管理**：修改了某个视图或人工校正了一个参数后，下游无法自动识别哪些衍生计算作废。

**本系统的核心准则**：
> 工程制图不是「画得像」，而是「可追溯、可验证、可参数化求解、可变更管理」。

---

## 2. 核心架构与三阶段编译管线

系统严格遵循工程图编译的三阶段流式管线：

```
┌────────────────────────────────────────────────────────────────────────┐
│                        1. 多源图纸接入 (DRAWING INTAKE)                 │
│  接收扫描图、PDF、DWG、DXF 存量图纸，保留文件版本、图层、图元 Handle 与原始空间坐标 │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      2. 工程信息编译 (ENGINEERING COMPILATION)          │
│  • 图纸读数 (Drawing Readings)：读取图纸标注、构件编号与跨视图对应关系       │
│  • 物料表核验 (BOM Cross-Check)：交叉核对构件规格、数量、材质与工程资料      │
│  • 依赖图构建 (Dependency DAG)：构建构件、尺寸与派生计算之间的有向无环图       │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      3. 可信结果交付 (VERIFIED DELIVERY)               │
│  • 几何求解验证 (Geometry Validation)：拓扑连接、几何闭合与工程一致性求解    │
│  • 变更传播引擎 (Change Invalidation)：上游修改自动标记下游为 STALE 待重算  │
│  • 多目标可信交付：导出交互式 3D 几何 (GLB/OBJ)、图数据库 (Neo4j)、CAD/PLM 上下文 │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 已实现的代码结构与功能清单

项目根目录：`/Users/zsn/Documents/zotore/engineering-trace/`

```
engineering-trace/
├── SKILL.md                      # 给大模型/Agent 的工作流技能定义与硬性规则约束
├── README.md                     # 项目上手与快速运行指南
├── PROJECT_DOCUMENTATION.md      # 本文档（系统架构与设计全貌）
├── schema/
│   └── engineering_model.json    # JSON Schema 严格数据规范
├── traceability/
│   ├── __init__.py               # 包入口与版本定义
│   ├── model.py                  # 核心数据模型 (Component/Dimension/Connection/Rule)
│   ├── graph.py                  # 依赖 DAG 分析、下游级联失效传播算法
│   ├── io.py                     # 模型持久化、引用完整性校验、追溯报告生成
│   ├── cli.py                    # 完整命令行交互工具
│   ├── intake/
│   │   ├── __init__.py
│   │   ├── dwg.py                # DXF/DWG 矢量图元解析与实体提取 (基于 ezdxf)
│   │   └── ocr.py                # 扫描图尺寸与标注可插拔 OCR 提取接口
│   ├── harness/
│   │   ├── __init__.py
│   │   ├── harness.py            # 规则验证编排器 (Agent Harness)
│   │   └── validators.py         # 几何/压力/材质/法兰内置工程验证器
│   └── export/
│       ├── __init__.py
│       ├── exporters.py          # Neo4j Cypher / GEXF / 交付验收报告导出
├── examples/
│   └── pipe_network.json         # 工业管道 P&ID 验证示例
└── tests/
    ├── test_traceability.py      # 模型与图分析核心单元测试 (全部通过)
    └── test_intake_harness_export.py # 图纸接入、验证编排与导出测试 (全部通过)
```

---

## 4. 核心数据模型详解 (`traceability/model.py`)

### 4.1 四级尺寸置信度 (`DimensionOrigin`)
* `MEASURED`（实测）：直接从矢量 CAD 几何、点云或高精度激光测量获得。
* `DERIVED`（派生）：通过几何约束求解、力学平衡或跨视图投影计算得出。
* `ASSUMED`（假设）：由工程师假设或经验估算，需标明置信度。
* `PLACEHOLDER`（占位）：图纸上识别出有该尺寸标注，但数值未定，强制阻断直接交付。

### 4.2 来源追踪结构 (`SourceRef`)
每一个构件和尺寸均强制绑定来源信息：
```json
{
  "source_type": "drawing",
  "reference": "Tower-Sheet-03-Elevation",
  "detail": "Handle=2A4F, Layer=TRUSS_MAIN, Coord=(1200, 3400)",
  "confidence": 0.95,
  "extracted_by": "ezdxf-intake"
}
```

### 4.3 变更级联失效算法 (`traceability/graph.py`)
利用有向无环图（DAG），当某一个上游尺寸（例如塔脚跨距 $L$）被修改时，系统沿依赖链自动搜索所有下游依赖节点，将其状态置为 `STALE`（失效待重算），防止旧参数污染 3D 求解结果。

---

## 6. 命令行常用指令清单

```bash
# 1. 验证工程模型引用的完整性
python -m traceability.cli validate examples/pipe_network.json

# 2. 生成工程追溯与置信度报告
python -m traceability.cli report examples/pipe_network.json

# 3. 模拟改动上游构件/尺寸（触发依赖作废）
python -m traceability.cli invalidate examples/pipe_network.json --node d_pipe_od

# 4. 执行工程 Agent Harness 自动规则验算
python -m traceability.cli harness examples/pipe_network.json

# 5. 导出数字孪生 Neo4j Cypher 脚本
python -m traceability.cli export examples/pipe_network.json --format cypher

# 6. 从 DXF 图纸中自动提取图元并生成工程模型
python -m traceability.cli intake-dxf examples/demo.dxf --demo
```

---

## 8. 给下游 AI / 协作团队的对接指引

当其他大模型或算法模块接入本项目时，请遵守以下交互协议：
1. **输入解析协议**：从任何多视图施工图读取数据时，必须实例化为 `EngineeringModel`，严禁直接输出裸 3D 点云。
2. **求解阻断原则**：如果模型中存在 `status == pending` 的几何闭合规则，或 `origin == placeholder` 的关键定位尺寸，3D 导出器应报警并拒绝生成终版施工级模型。
3. **输出交付物**：
   * 结构化工程数据：`model.json`
   * 3D 几何实体：`model.glb` / `model.obj`
   * 工程验收报告：`report.md`（包含所有杆件的图纸出处与置信度）


---

## 附录：项目交付验收清单 (Checklist)

### 功能验收
- [x] 工程模型数据规范（JSON Schema）定义完整
- [x] 构件 / 尺寸 / 连接 / 规则四类实体可序列化与反序列化
- [x] 依赖 DAG 构建与祖先/后代查询
- [x] 变更传播：修改上游节点自动标记下游 STALE
- [x] 引用完整性校验（跨对象引用检查）
- [x] 追溯报告生成（尺寸来源分级、置信度、失效清单）
- [x] DXF 图纸图元抽取（直线/圆/文本 + 来源追溯）
- [x] 扫描图上下文建立（可插拔 OCR，缺 OCR 时降级为 placeholder）
- [x] Agent Harness 规则验证编排（内置压力/法兰/材料验证器）
- [x] Neo4j Cypher / GEXF / 交付报告导出
- [x] 全部 12 个单元测试通过

### 质量原则验收
- [x] 尺寸必须声明 origin（实测/派生/假设/占位），拒绝裸数值
- [x] 数据不足时验证器返回 pending，绝不编造结果
- [x] 低置信度对象（confidence < 0.7）在报告中醒目标注
- [x] 改动必须沿依赖图传播，旧结果不得继续使用

### 已知边界（诚实声明）

- **3D 重构方向已暂停**：此前基于臆造坐标生成的铁塔塔头示意图质量差、
  与真实工程不符，已全部删除（`glb_truss.py`、`tower_head.obj`、
  `tower_viewer.html` 等）。下一步将基于用户提供的**真实 CAD 图纸**
  （DWG/DXF 或扫描版）重新设计几何求解与 3D 导出，不做凭空捏造的演示模型。

- OCR 真实标注识别依赖环境安装 `tesseract` + `pytesseract`；当前环境未装，
  扫描图尺寸以 placeholder 形式保留，等待人工或真 OCR 补测。
- 3D 导出能力待基于真实 CAD 图纸重新设计；当前不提供任何捏造的演示模型。
