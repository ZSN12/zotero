# Skill + Harness 架构说明：多模态模型驱动的工程图编译系统

> 本文回答一个本质问题：**工程制图识别到底靠什么？**
> 答案：**多模态模型（MLLM）当眼睛和脑子，Skill 管「怎么干」，Harness 管「干得对不对」。**
> 模型不是最终交付物，它只是把图纸「读」成结构化候选对象的一个环节。

---

## 1. 为什么不能「直接问模型」

让多模态模型直接看图并回答问题（例如「这张铁塔图里有什么」）在工程场景下不可用，原因有四：

| 直接问模型 | 工程后果 |
|---|---|
| 输出一段自然语言描述 | 无法进入 CAD / PLM / 数字孪生 |
| 说不清每个数值来自图里哪个位置 | 失去追溯性，施工方不敢用 |
| 模型可能「幻觉」编造尺寸 | 无验证机制，风险不可控 |
| 改一个数要模型重读全图 | 无变更管理，下游作废关系丢失 |

因此模型必须被约束在**结构化输出契约**之内，其输出必须经过**验证编排**才能成为交付物。

---

## 2. 三件套的分工（本质定义）

### 2.1 多模态模型（MLLM）= 眼睛和脑子

- **输入**：扫描图 / PDF / DXF / DWG（或转成的图像）
- **输出**：结构化候选对象（JSON），例如识别出「这里有个杆件 G01，截面 L100x8，两端连接 N01 和 N05」
- **能力边界**：模型会看错、会猜、会漏。所以它的输出**一律视为候选**，置信度不设满。

### 2.2 Skill = 工作流和输出契约

Skill 不是代码库，而是一份**行为规范**，约束模型「怎么干活」：

1. **每个对象必须带 SourceRef**：图纸号 + handle/坐标 + confidence
2. **绝不猜尺寸**：读不到的写 `placeholder`，阻断终版导出
3. **冲突不覆盖**：交叉核验发现冲突 → 新建待验证项，不悄悄改原值
4. **改动必须传播**：改了任何节点，沿依赖 DAG 标记下游 `stale`
5. **交付前必须验证**：所有 pending 规则走 Harness 验证

在本仓库中，Skill 的落地是 `SKILL.md`（行为规范）+ `skill/contract.py`（把模型输出强制转成 `EngineeringModel` 的代码契约）。

### 2.3 Harness = 验证编排

Harness 不信模型的「一面之词」，它拿模型的候选对象去做**交叉验证**：

- 与 BOM 物料表核验长度/截面
- 几何闭合检查（杆件两端节点存在、距离匹配）
- 规则执行（压力等级、材料兼容……）
- 依赖 DAG 变更传播
- `placeholder` 阻断终版交付

本仓库中，Harness 的落地是 `harness/harness.py`（编排器）+ `harness/validators.py`（内置验证器）+ `harness/tower_validators.py`（铁塔专用验证器）。

---

## 3. 完整数据流

```
                        ┌────────────────────────────────────┐
 扫描图 / PDF / DWG / DXF│        多模态模型（MLLM）            │
 ───────────────────────►│  识别图元、标注、构件关系            │
                        │  输出：结构化候选 JSON（带置信度）     │
                        └───────────────┬────────────────────┘
                                        │
                                        ▼
                        ┌────────────────────────────────────┐
                        │    SKILL 输出契约（contract.py）      │
                        │  • 强制 SourceRef（文件/版本/位置）    │
                        │  • 读不到的 -> placeholder            │
                        │  • 冲突 -> 新建待验证项，不覆盖        │
                        │  • 输出 EngineeringModel             │
                        └───────────────┬────────────────────┘
                                        │
                                        ▼
                        ┌────────────────────────────────────┐
                        │    HARNESS 验证编排                 │
                        │  • BOM 交叉核验（长度/截面）          │
                        │  • 几何闭合 / 拓扑校验                │
                        │  • 依赖 DAG + 变更传播               │
                        │  • placeholder 阻断终版交付          │
                        └───────────────┬────────────────────┘
                                        │
                                        ▼
                        ┌────────────────────────────────────┐
                        │  交付：EngineeringModel + 3D + 报告  │
                        │  可进入 CAD / PLM / 数字孪生 / AI    │
                        └────────────────────────────────────┘
```

---

## 4. MLLM 可插拔后端（本仓库已实现）

`intake/mllm_backend.py` 定义统一接口：

| 后端 | 说明 | 产品路径 | 当前代码默认 |
|---|---|---|---|
| `MLLMBackend` | Kimi / OpenAI 兼容 VLM | **A1 件号 OCR**；扫描图主路径 | PNG/PDF + `--backend mllm` |
| `TowerScanBackend` | 霍夫线 + 规则 A2 | 无 API 时扫描降级 | 无 API 的 `run-tower` |
| `RuleBasedBackend` | ezdxf 矢量解析 | **hybrid 计划中的 A2** | `dxf/dwg` 硬编码默认 ⚠️ |
| `NullBackend` | placeholder | 兜底 | 非 tower 扫描无 API |

> **实现缺口：** `choose_backend()` 与 `run_tower()` 对 DXF/DWG 跳过 Agent 链；  
> `deliver_project()` 未调用 Kimi。修复计划见 [`docs/PRODUCT_PATH_AND_AGENT_PLAN.md`](docs/PRODUCT_PATH_AND_AGENT_PLAN.md)。

统一接口：

```python
class DrawingBackend(Protocol):
    def analyze(self, drawing: DrawingInput) -> ModelCandidate:
        ...
```

其中 `ModelCandidate` 是**模型候选输出**（尚未通过 Skill 契约），
`skill/contract.py` 负责把它强制转成 `EngineeringModel`（补 SourceRef、placeholder 化、冲突不覆盖）。

---

## 5. 关键设计原则（不可妥协）

1. **模型输出 ≠ 交付物**。模型输出只是候选，必须过 Skill 契约 + Harness 验证。
2. **confidence 永远 < 1.0**。模型识别结果默认 0.6~0.85，人工确认后才可升为 1.0。
3. **placeholder 是安全阀**。读不到的关键尺寸必须阻断终版导出，宁可少交付，不可错交付。
4. **SourceRef 是命根子**。没有来源的对象不进 EngineeringModel。
5. **Harness 只依据数据说话**。数据不足返回 pending，绝不编造 passed。

---

## 6. 与现有代码的映射

| 架构层 | 本仓库文件 |
|---|---|
| MLLM 眼睛/脑子 | `intake/mllm_backend.py`（新增，可插拔） |
| Skill 输出契约 | `SKILL.md` + `skill/contract.py`（新增） |
| Harness 验证编排 | `harness/harness.py` + `harness/validators.py` |
| 铁塔专用验证 | `harness/tower_validators.py`（待补） |
| 数据模型 | `model.py` |
| 依赖 DAG / 变更传播 | `graph.py` |
| 交付导出 | `export/` + `solve/` |

---

## 7. 最小可用闭环（伪代码）

```python
# 1. 选择后端
backend = choose_backend(input)   # RuleBased / MLLM / Null

# 2. 模型「看」图，输出候选
candidate = backend.analyze(input)

# 3. Skill 契约：候选 -> EngineeringModel
model = contract.to_engineering_model(candidate)

# 4. Harness 验证 + 交叉核验
results = run_harness(model)
report = render_report(model)

# 5. placeholder 阻断终版交付
if missing_axes(model):
    raise SolveError("存在 placeholder，拒绝终版导出")
```

---

## 8. 结论

> **这个系统的本质 = 多模态模型（读图）+ Skill（约束模型行为）+ Harness（验证模型输出）。**
>
> 模型负责「看懂」，Skill 负责「说人话且不撒谎」，Harness 负责「对账」。
> 三者缺一不可：没有模型，看不懂扫描图；没有 Skill，模型输出不可信；没有 Harness，错误无法被发现和传播。

---

## 9. 与官网对齐 & 实现缺口（2026-08）

官网（[concentriccirclesmrtt.github.io](https://concentriccirclesmrtt.github.io)）要求 **DXF 与扫描图同属多源输入**，经 Agent Harness 编译。  
本仓库 **Agent 链（A0→A4）已实现**，但 **图册交付 `deliver_project` 与 JC1 跑批脚本仍走 ezdxf 旁路**。

| 能力 | 状态 |
|------|------|
| A0→A4 + `steps.json` | ✅ 扫描图 `run-tower` |
| Kimi A1 件号 | ✅ `MLLMBackend` + `acceptance.sh --with-mllm` |
| DXF → Agent（栅格化 / hybrid） | ⏳ 计划 Phase 1–2 |
| `deliver-project` 接 Agent | ⏳ 计划 Phase 2 |
| 跨页 M1–M6 + 证据链 | ⏳ 计划 Phase 3 |

详见 [`docs/PRODUCT_PATH_AND_AGENT_PLAN.md`](docs/PRODUCT_PATH_AND_AGENT_PLAN.md)。
