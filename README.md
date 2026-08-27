# Engineering Traceability

**从一张图，到可供 AI 使用的工程上下文。**

工程制图中，AI 生成的 3D 模型「看着像那么回事」，但没人敢拿去做施工、
检修或改造——因为说不清：

- 这根构件来自哪张图？
- 这个尺寸是实测还是猜的？
- 哪些连接规则验证过？
- 改了某个判断后，哪些结果要作废？

本项目用**结构化数据模型 + 依赖 DAG + 变更传播引擎**回答这四个问题，
并提供一个给 AI 使用的 **Skill**（`SKILL.md`）。

## 三阶段管线

```
多源图纸接入 DRAWING INTAKE
    扫描图、PDF、DWG、DXF → SourceRef（文件、版本、原始位置）
        ↓
工程信息编译 ENGINEERING COMPILATION
    构件 Component / 尺寸 Dimension / 连接 Connection
    + 与物料表交叉核验，冲突记为待验证
        ↓
可信结果交付 VERIFIED DELIVERY
    工程 Agent Harness 编排 Skills/工具/验证流程
    输出可进入 CAD、PLM、数字孪生、AI 系统的工程上下文
```

## 快速开始

```bash
# 1. 查看示例工程模型（P&ID 泵送管线）
python -m traceability.cli report examples/pipe_network.json

# 2. 校验引用完整性
python -m traceability.cli validate examples/pipe_network.json

# 3. 模拟「改了一个判断」：把 d_pipe_od 从 assumed 改掉
python -m traceability.cli invalidate examples/pipe_network.json --node d_pipe_od
#    → 自动作废 d_flow_rate（它依赖 d_pipe_od）

# 4. 验证规则，恢复 current
python -m traceability.cli verify examples/pipe_network.json --rule r_pressure_rating
```

## 核心数据模型

| 对象 | 关键字段 | 回答的问题 |
|---|---|---|
| Component | id, kind, source | 这是什么？来自哪张图？ |
| Dimension | value, unit, origin | 数值多少？实测还是猜的？ |
| Connection | from, to, rule_ids | 谁连谁？验证过吗？ |
| Rule | status, message | 这条规则过了没有？ |
| dependencies | node -> upstreams | 改了它会作废谁？ |
| staleness | current / stale | 现在还有效吗？ |

### 尺寸来源分级（origin）

- `measured` 实测
- `assumed` 假设/估算
- `derived` 派生计算
- `placeholder` 占位，待补测

### 变更传播示例

依赖图：

```
d_pipe_od ──┐
            ├──> d_flow_rate
c_pump_p101 ┘

c_pipe_seg1 ──> conn_pump_to_pipe ──> r_pressure_rating
                                    ──> r_flange_match
```

执行 `invalidate --node d_pipe_od` 后，`d_flow_rate` 自动变为 `stale`——
因为它是由 `d_pipe_od` 派生出来的，上游变了，下游必须重算。

## 目录结构

```
engineering-trace/
├── SKILL.md                      # 给 AI 的 Skill 定义（工作流 + 硬性要求）
├── README.md
├── schema/
│   └── engineering_model.json    # JSON Schema
├── traceability/
│   ├── __init__.py
│   ├── model.py                  # 数据模型 + 序列化
│   ├── graph.py                  # 依赖 DAG + 变更传播
│   ├── io.py                     # 读写 + 校验 + 报告
│   └── cli.py                    # 命令行入口
├── examples/
│   └── pipe_network.json         # 示例：P&ID 泵送管线
└── tests/
    └── test_traceability.py
```

## 铁塔结构图识别与 3D 重构

完整实施方案见 [`TOWER_IMPLEMENTATION_PLAN.md`](TOWER_IMPLEMENTATION_PLAN.md)。
图层 / 件号 / 视图区域规范见 [`schema/tower_layer_map.json`](schema/tower_layer_map.json)，
解析器与 DXF 生成器共用这一份规范。

### 主路径：110kV 猫头塔端到端

```bash
# 1) 解析真实级图纸 + BOM 交叉核验 + 跨视图坐标合并 + 自动注入五条验证规则
python -m traceability.cli intake-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv --merge --out examples/tower_110kv_model.json

# 2) 引用完整性校验（intake-tower 已自动执行；也可手动）
python -m traceability.cli validate examples/tower_110kv_model.json

# 3) 五条铁塔规则自动验证（intake-tower 已自动执行；也可手动）
python -m traceability.cli harness examples/tower_110kv_model.json

# 4) 3D 求解 + OBJ 线框 / GLB 实体 + 与金标准对齐（偏差 <2% / <50mm）
python -m traceability.cli solve-tower examples/tower_110kv_model.json \
  --out examples/tower_head.obj --golden examples/tower_110kv_golden.json
python -m traceability.cli solve-tower examples/tower_110kv_model.json \
  --out examples/tower_head.glb --format glb --golden examples/tower_110kv_golden.json

# 5) 交付报告 / 图数据库 / 3D 格式
python -m traceability.cli report examples/tower_110kv_model.json
python -m traceability.cli export examples/tower_110kv_model.json --format report
python -m traceability.cli export examples/tower_110kv_model.json --format glb --out examples/tower_head
python -m traceability.cli validate examples/tower_110kv_model.json --schema
```

`--merge` 会执行 Phase 2：正立面 + 侧立面 + 剖面三视图线性解耦出节点三轴
坐标（正立面带 `0.08*y` 展开量，侧立面带 `0.08*x`，用剖面干净 x 作配对判据），
再把正立面的 316 根投影杆件合并为 316 根物理杆件。

也可以走 MLLM/Skill 契约入口（DXF 会先走 rule-based 后端）：

```bash
python -m traceability.cli compile-drawing examples/tower_110kv.dxf \
  --tower --bom examples/tower_110kv_bom.csv --merge \
  --golden examples/tower_110kv_golden.json --out examples/tower_110kv_model.json
```

### 无 DXF 扫描图：多 Agent 编排（A0→A4，P1）

PNG/PDF 扫描图不再用「单轮 MLLM 识别整塔」，而是拆成五步可审计的 Agent 链：

| 步 | 职责 | 后端优先级 | 输出 |
|---|---|---|---|
| A0 版面分析 | 切视图/BOM/图签 | 规则（tower_layout） | `drawing_view + bbox` |
| A1 件号 OCR | 只读件号，不画杆 | VLM/MLLM；无 API 跳过 | `{labels: [{text, bar_id, x_px, y_px, view}]}` |
| A2 几何检测 | 只检线/节点，不挂件号 | 霍夫线（tower_layout）为主 | `{bars: [{bar_uid, x1,y1,x2,y2}], nodes}` |
| A3 关联匹配 | bar → 最近合法件号 | **确定性规则**（与 DXF 同逻辑，不扔给模型） | `{bar_uid, bar_id, confidence}` |
| A4 编译验证 | `to_engineering_model()` + Harness | contract + tower_validators | EngineeringModel |

每步都有 Harness 闸门（passed / pending / failed），写入 `steps.json`；
扫描图默认 `solve_status=pending_review`，无坐标不 export strict GLB。

```bash
# 一条命令跑完 A0→A4（无 MLLM API 时 A1 自动跳过，A3 全 UNLABELED）
python -m traceability.cli run-tower examples/clear/tower_front_hd.png \
  --out-dir out/tower-agent-run
# 输出：steps.json（a0_layout/a1_labels/a2_geom/a3_link/a4_harness）
#      + model.json + harness_summary.json
```

验收基线（`examples/clear/tower_front_hd.png`，无 API 环境）：
- A2 霍夫基线 `bars=291`（≥100）
- A1 无 API → `skipped`，不猜值；A3 无 label → `pending`，不 failed 凑数
- 全链 `ok=True`（pending 不是失败），模型全部 `solve_status=pending_review`，不导出 strict GLB

矢量主路径保持不变：**DXF/DWG 永远走 `RuleBasedBackend`**，不调用 MLLM；
`choose_backend()` 继续保证 dxf/dwg 不入 MLLM 主路径。

### Phase 4：扫描图候选（pixel 坐标，人工复核队列）

```bash
# 版面分析 + 霍夫线检测 + 端点聚类 → 候选杆件/节点（confidence ≤ 0.6）
python -m traceability.cli intake-scan examples/clear/tower_front_hd.png --tower \
  --out examples/tower_front_scan.json

# 也可走 compile-drawing 入口（raster + --tower 时自动用 rule-based-scan 后端）
python -m traceability.cli compile-drawing examples/clear/tower_side_hd.png --tower \
  --out examples/tower_side_scan.json
```

扫描图产出是 pixel 坐标候选（`solve_status=pending_review`），不换算毫米、
不猜编号，因此不会进入终版 3D——比例尺标定与编号确认留给人工复核。

### demo 路径（不合并，保留投影杆件）

```bash
python -m traceability.cli intake-tower examples/tower_demo.dxf --demo \
  --bom examples/tower_bom.csv --out examples/tower_demo_model.json
python -m traceability.cli solve-tower examples/tower_demo_model.json \
  --out examples/tower_demo.obj
# 预期：投影节点缺 Z，严格模式拒绝终版导出（这是正确行为，等待 --merge）
```

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## 多步编排与一键交付（P0）

```bash
# 一步命令跑完全链：intake → compile → cross_check → verify → retry → export
python -m traceability.cli run-tower examples/tower_110kv.dxf   --bom examples/tower_110kv_bom.csv --merge   --golden examples/tower_110kv_golden.json --out-dir out/tower-run
# 输出：model.json + tower.glb + report.md + steps.json + harness_summary.json

# 一键交付包
python -m traceability.cli deliver-tower examples/tower_110kv.dxf   --bom examples/tower_110kv_bom.csv --merge --out-dir out/tower-delivery
```

Demo 页：`python web/server.py` 后打开 http://127.0.0.1:8000
（上传 DXF/PNG/PDF → 看 GLB + Harness 摘要 + 构件追溯）。

NeuBE SR 对标叙事见 [`docs/NEUBE_ALIGNMENT.md`](docs/NEUBE_ALIGNMENT.md)。

## 真实国网 35kV 数据验收（G）

一条命令文档：

```bash
python3 -m traceability.cli run-tower <国网dwg或dxf> \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --bom <BOM.csv或BOM.dxf> --merge --out-dir out/guowang-run
```

批量（目录内全部 DWG 自动转 DXF）：

```bash
python3 -m traceability.cli intake-tower-batch <国网目录> \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out-dir out/guowang-batch [--merge]
```

解析率报告（替代手改 markdown）：

```bash
python3 -m traceability.cli parse-report <dxf> \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out out/parse_report.json
```

真实验收标准（国网 35A1-JC1）：
- 总装/立面类文件：杆件 >0，件号关联率 >30%（第一版）
- Harness：拓扑 closed；BOM 有则 length/section 规则有明确 passed/failed，不 silent pending
- 有 --merge 且多视图齐全时：节点三轴 solved 比例可统计
- 图签类 00-*：标记 drawing_kind=title_block，不计入杆件解析失败

原则：读不到 → placeholder，strict 导出阻断；每个对象必须有 SourceRef；
外图低解析率写进报告，不改验证器凑 passed；国网原图不进公开 git。

## Roadmap

- [x] 接入 DXF/DWG 解析器（ezdxf + ODA/dwg2dxf 转换层）自动抽取 Component
- [x] 接入 OCR 读取图纸标注 → 自动生成 Dimension（带 origin=placeholder）
- [x] Agent Harness：多步编排 + 规则验证写回状态（run-tower / harness）
- [x] 导出到 Neo4j / 数字孪生 / PLM 的适配器
- [x] 铁塔专用 MLLM Prompt + JSON Schema（P1-1）
- [x] 扫描图 MLLM 主路径（配 API 走 MLLM，无 API 降级规则线检测）
- [x] PDF 转图入口（pymupdf/pdf2image）
- [x] 图层映射 per-project overlay（--layer-map，换图只改配置）
- [x] 长度约束传播 / 最小二乘 3D 求解
- [x] L 型角钢截面 GLB（按截面规格区分）
- [x] MLLM 铁塔 Schema 硬约束（只允许 tower_bar/tower_node/drawing_view；策略 A 丢弃+parse_warnings）
- [x] MLLM 送图前缩放（最长边 ≤2048 PNG）+ 超时/日志 meta
- [x] benchmark/mllm_vs_scan.py 三列评测（rule-based-scan / kimi-for-coding / k3-256k）
- [x] 扫描图多 Agent 编排（A0 版面 → A1 件号 → A2 几何 → A3 关联 → A4 编译验证，steps.json 五步可审计）
- [x] P0-1 merge_view_coordinates/merge_view_bars/finalize_tower_model 下传 --layer-map（国网 overlay 的 view_regions 可读回来）
- [x] P0-2 国网 02 单立面标记 view_mode=single_facade（2D+件号率；3D 靠立面/平面分文件）
- [x] P0-4 intake_scan_batch 按 (view_type, z_level) 存模型，多 plan 不再互相覆盖
- [x] P0-5 DWG 批量 --merge 输出 cross_file_bar_id_report（跨文件去重，不假装合 3D）
- [x] P1-6 A2 霍夫杆按 A0 视图 bbox 打 view_type，A3 同视图过滤生效
- [x] P1-7 parse_bars=False 短路 A1/A2 整条 agent 链
- [x] P1-8 LABEL_AGENT_PROMPT 明确 x_px/y_px 为裁剪图左上角 (0,0) 的像素坐标
- [x] P1-9 intake_scan_batch 返回完整 ProcessingGraph（每文件一步 + merge_scan + a4_harness）
- [x] P2-10 web 2D 证据层（model.json + 源图 + tower_bar 双向高亮）
- [x] P2-11 scripts/acceptance.sh 验收脚本固定化

## License

MIT


## 核心架构：Skill + Harness（本质）

系统本质 = **多模态模型（读图）+ Skill（约束行为）+ Harness（验证输出）**。

完整说明见 [`SKILL_HARNESS_ARCHITECTURE.md`](SKILL_HARNESS_ARCHITECTURE.md)。

```bash
# 一条命令走完：后端选择 -> 模型分析 -> Skill 契约 -> EngineeringModel
python -m traceability.cli compile-drawing examples/tower_demo.dxf --kind dxf

# 扫描图（未配置 OPENAI_API_KEY 时走 Null 兜底，绝不猜值）
python -m traceability.cli compile-drawing examples/tower_demo.png --kind scan
```

配置多模态模型（Kimi Coding 套餐 — K2.7 Code）：

```bash
export MLLM_PROVIDER=kimi-code
export KIMI_API_KEY=sk-...        # Kimi Code 控制台创建的密钥
export MLLM_MODEL=k3-256k           # K3 视觉（256K）；更高档可用 k3

# 检查配置是否读到（不打印完整 key）
python3 -c "from traceability.intake.mllm_providers import mllm_config_status; print(mllm_config_status())"

# 铁塔扫描图走 MLLM
python3 -m traceability.cli compile-drawing examples/clear/tower_front_hd.png \
  --tower --backend mllm --out examples/tower_front_mllm.json
```

OpenAI / Moonshot 开放平台：

```bash
export OPENAI_API_KEY=sk-...
export MLLM_MODEL=gpt-4o

# 或 Moonshot 按量 + kimi-k2.7-code
export MLLM_PROVIDER=moonshot
export MOONSHOT_API_KEY=sk-...
export MLLM_MODEL=kimi-k2.7-code
```


## MLLM 铁塔输出硬约束（Prompt / Schema / 评测）

`mllm_tower_prompt.py` 已按验收口径收紧：

- 只允许 `kind`：`tower_bar`、`tower_node`、`drawing_view`；
  禁止 `tower`、`bolt`、`gusset_plate` 等。
- `tower_bar` 必须输出 `bar_id`、`from_node` / `to_node`；
  `tower_node` 必须输出 `node_id`。
- 坐标只认 `x_px/y_px` 或 `x/y/z`（mm）；没有则 null + placeholder，
  禁止只写在 detail 字符串里。
- Schema 策略 A：非法 kind 丢弃该条 + `parse_warnings`，不整批拒，
  避免一个 `tower` 对象导致 0 产出。

P1 多 Agent 编排使用独立的小 Prompt/Schema（每步只干一件事）：

- `LABEL_AGENT_PROMPT` / `LABEL_AGENT_SCHEMA`：A1 只读件号文字，
  输出 `{labels: [{text, bar_id, x_px, y_px, view}]}`；禁止识别整塔。
- `GEOM_AGENT_PROMPT` / `GEOM_AGENT_SCHEMA`：A2 只检线/节点，
  输出 `{bars: [{bar_uid, x1,y1,x2,y2}], nodes: [...]}`；禁止挂件号。
- 每条坐标必须是 JSON 数字字段；非法条按策略 A 丢弃 + warning，
  不整批 0 产出。
- `MLLMBackend.call_agent_json()`：单 Agent 调用记录 `duration_ms`，
  失败返回 `meta.failure_reason`，由编排层把该步标 pending，不级联猜值。

调用入口：
```python
from traceability.intake.mllm_tower_prompt import parse_tower_mllm_output_with_warnings
objects, problems, warnings = parse_tower_mllm_output_with_warnings(parsed)
# problems=[] 为整批结构可用；warnings 为按条丢弃/降级记录
```

P1 性能与输入：

- 送图前缩放：最长边 ≤2048（保持 PNG），`meta` 记录 original/resized/bytes_sent。
- MLLM 调用带 timeout（`MLLM_TIMEOUT`，默认 180s），`meta` 记录
  `model`、`elapsed_s`、`raw_length`、`parse_warnings`、`failure_reason`，
  失败原因可写入 steps.json。

P2 评测（与矢量主路径分开）：

```bash
python3 benchmark/mllm_vs_scan.py examples/clear/tower_front_hd.png   --out examples/external/mllm_benchmark.json
```

同图三列对比：
- `rule-based-scan`：候选杆件数 / 件号数
- `kimi-for-coding`：杆件数 / 件号数（`MLLM_PROVIDER=kimi-code` + `MLLM_MODEL=kimi-for-coding`）
- `k3-256k`：杆件数 / 件号数（`MLLM_PROVIDER=kimi-code` + `MLLM_MODEL=k3-256k`）

无 API Key 的模型记录为 `skipped`，不编造数字。
