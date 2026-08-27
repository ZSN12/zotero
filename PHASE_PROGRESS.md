# 铁塔管线推进记录（防丢失）

> 只记录「已落地 + 已验证」的步骤。当前进度：P0 ✅ / P1 ✅ / P2 基本 ✅ / Phase 4 进行中。

## 当前状态一句话

`tower_110kv` 从解析到可信 3D 已端到端跑通：316 杆件 / 85 节点，
五条验证规则 5 passed，与金标准偏差 max=0.011mm（验收限 <2%）。
Phase 4 扫描图最小可用版已落地（版面分析 + 霍夫线检测 + 端点聚类），
并在 P1 完成无 DXF 扫描图的 A0→A4 多 Agent 编排（每步可审计 + Harness 闸门）。
测试 90 项全绿。

---

## 已完成步骤（按时间）

### P0 — 让端到端能跑

| # | 步骤 | 落地文件 | 验证 |
|---|---|---|---|
| 1 | 图层映射统一：LEG/HORIZ/DIAG/CROSS/HEAD/KNEE/HANG + TRUSS_MAIN | `schema/tower_layer_map.json` | `intake-tower examples/tower_110kv.dxf` → 1064 投影杆件 / 265 节点（原先 0） |
| 2 | 件号正则支持 `M\d{4}` + `[GSB]\d{1,4}` | 同上 + `tower_dxf.py` | 110kV 编号关联 407/1064（投影） |
| 3 | 杆件编号关联改为「同视图内最近线段中点」距离（点-线段距离会被交叉杆件抢走编号） | `tower_dxf.py` | 正立面 300/316 标签唯一命中；demo 38/38 |
| 4 | `cross_check_bom` 按 `properties.bar_id` 聚合匹配真实构件 ID，不再写 `bar_{id}` | `tower_bom.py` | `validate` 0 问题（原先 52 条悬空 applies_to） |
| 5 | `intake-tower` 结束自动注入五条 Rule | `tower_validators.py` `inject_tower_rules()` | `model.rules` 有 5 条，harness 不再空跑 |
| 6 | 修复两条 BOM 验证器 bug：全匹配时误报 PENDING → 改为 PASSED | `tower_validators.py` | 110kV 合并模型 5/5 passed |

### P1 — 让 3D 有意义

| # | 步骤 | 落地文件 | 验证 |
|---|---|---|---|
| 7 | `merge_view_coordinates`：front(x'=x+0.08y, z) + side(y'=y+0.08x, z) + section(干净 x, z) 三视图线性解耦，剖面 x 作 Hungarian 配对判据；plan 视图给 x/y/z_level | `tower_views.py` | 85 个 front 节点全部解出，与金标准偏差 max=0.011mm |
| 8 | `merge_view_bars`：以正立面为主骨架（每根物理杆件只画一次），316 投影合并为 316 物理杆件；`length_mm_3d` 由节点三轴坐标计算；UNLABELED 用 BOM 长度唯一匹配回编号 | `tower_views.py` | 合并后 314/316 有编号；BOM 长度/截面规则 passed |
| 9 | `solve_tower` 实际可用；新增 `compare_to_golden`（贪心/Hungarian 最近邻匹配 + 中心对齐） | `tower_solver.py` | `solve-tower --golden` → 85/85，max=0.011mm |
| 10 | OBJ 线框 + GLB 实体导出（trimesh，按杆件圆柱近似、按类别着色） | `tower_solver.py` | GLB 250KB 可导出 |

### P2 — 工程化

| # | 步骤 | 落地文件 | 验证 |
|---|---|---|---|
| 11 | 依赖声明：ezdxf/numpy/scipy/matplotlib/openai/trimesh/jsonschema | `requirements.txt` + `requirements-tower.txt` | 环境按此安装 |
| 12 | `intake-tower` 自动 validate + harness（`--no-check` 可跳过） | `cli.py` | 110kV 自动 5 passed；demo 如实报 failed |
| 13 | `compile-drawing --tower --bom --merge --golden`：MLLM/规则输出接入铁塔验证链 | `cli.py` + `tower_pipeline.py` | 一条命令 5/5 passed + 金标准通过 |
| 14 | 生成器与解析器共用规范：新增 `tower_spec.py` 统一读取 `schema/tower_layer_map.json` | `tower_spec.py` + 两个生成器 | 重新生成 DXF 后解析结果不变（1064/265） |
| 15 | `export --format obj/glb` 3D 格式 | `cli.py` | 可导出 OBJ/GLB |
| 16 | JSON Schema enforced：`load_model(enforce_schema=True)`、`validate_against_schema()`、`validate --schema` | `io.py` + `cli.py` | `validate --schema` 通过 |
| 17 | 文档更新：README 铁塔主路径、TOWER_IMPLEMENTATION_PLAN 落地状态 | `README.md` `TOWER_IMPLEMENTATION_PLAN.md` | — |
| 18 | 集成测试补齐 | `tests/test_tower_110kv.py` | 36 tests passed |

---

## 关键命令（已验证，可直接照抄）

```bash
# 主路径
python -m traceability.cli intake-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv --merge --out examples/tower_110kv_model.json

python -m traceability.cli validate examples/tower_110kv_model.json --schema
python -m traceability.cli harness examples/tower_110kv_model.json
python -m traceability.cli solve-tower examples/tower_110kv_model.json \
  --out examples/tower_head.obj --golden examples/tower_110kv_golden.json
python -m traceability.cli solve-tower examples/tower_110kv_model.json \
  --out examples/tower_head.glb --format glb --golden examples/tower_110kv_golden.json

# MLLM/Skill 契约入口（同一条链）
python -m traceability.cli compile-drawing examples/tower_110kv.dxf \
  --tower --bom examples/tower_110kv_bom.csv --merge \
  --golden examples/tower_110kv_golden.json --out examples/tower_110kv_model.json

# 测试
python -m pytest tests -q          # 36 passed
```

---

## Phase 4 扫描图（已完成最小可用版）

`tower_layout.py` 已实现：

1. 版面分析：按空白行/列空隙切分区域 → `scan_region` 组件（bbox + ink_ratio）
2. 线检测：灰度阈值过滤浅色网格 → 霍夫 HoughLinesP → 共线合并 → 长度过滤
3. 端点聚类 → 候选 `tower_node`（pixel 坐标）
4. OCR（可选 pytesseract；未安装则跳过，绝不猜编号）
5. 输出 EngineeringModel：所有候选 confidence ≤ 0.6、`solve_status=pending_review`、
   比例尺 dimension 为 placeholder，不进终版 3D

验证（`examples/clear/tower_front_hd.png`，3541×5968）：
- 498 候选杆件 / 788 候选节点 / 18 版面区域
- 所有候选对象有 SourceRef，confidence ≤ 0.6
- `validate` 引用完整性通过；roundtrip 通过

CLI 入口：
```bash
python -m traceability.cli intake-scan examples/clear/tower_front_hd.png --tower \
  --out examples/tower_front_scan.json

python -m traceability.cli compile-drawing examples/clear/tower_side_hd.png --tower \
  --out examples/tower_side_scan.json
```

已知局限（符合设计，非 bug）：
- 候选数量大于真实杆件（dim 线/图例等仍会被霍夫检测到），这是「人工复核队列」语义，
  不进入终版 3D。
- 不标定 px→mm 比例尺、不识别真实件号，均由人工复核完成。

---

## 仍保留的已知缺口（有意为之 / 待办）

- 正立面 8 组 PLAN_D 斜材两两标签完全重叠（图纸信息本身缺失）→ 2 根 `UNLABELED`，符合「绝不猜」。
- 扫描图候选含 dim 线/图例等噪声，未做精细过滤（Phase 4 定位是人工复核队列，可接受）。
- 三套 DXF 生成器已读规范，但 `tower_clear_preview.py` 未接入（预览器不改结构数据，暂缓）。
- 无 IFC/BIM 全量交付（MVP 外）。
- `tower_110kv_model.json` / `tower_model.json` 等 examples 已用最新代码重新生成。


---

## P0/P1/P2 补全（按任务表逐项落地，2026-08）

### P0 编排与交付
- P0-1 `traceability/harness/tower_harness.py` + CLI `run-tower`：intake→compile→cross_check→verify→retry→export，steps.json 逐步状态，`--retry`/`--human-review`
- P0-2 `traceability/harness/processing_graph.py`：steps.json（status/duration/error）
- P0-3 `web/`（index.html + app.js + styles.css + server.py）：上传图纸 → GLB + Harness 摘要 + 构件追溯
- P0-4 CLI `deliver-tower`：一次产出 model.json + tower.glb + report.md + steps.json + harness_summary.json
- P0-5 `.cursor/skills/engineering-traceability/`：SKILL.md + contract.md + examples.md
- P0-6 `docs/NEUBE_ALIGNMENT.md` + Demo 侧栏

### P1 核心技术缺口
- P1-1 `traceability/intake/mllm_tower_prompt.py`：铁塔专用 Prompt + JSON Schema + validate/parse
- P1-2 `choose_backend()`：tower+scan → MLLM 优先，无 API 降级 `TowerScanBackend`
- P1-3 `traceability/intake/pdf_raster.py`：pymupdf/pdf2image PDF 转 PNG
- P1-4 `examples/external/`：脱敏 DXF + BOM + `PARSE_RATE_REPORT.md`（如实报告未识别图层）
- P1-5 `tower_spec.py` per-project overlay + CLI `--layer-map`
- P1-6 `tower_solver.solve_tower`：杆长约束传播 + scipy least_squares 补缺轴
- P1-7 `export_tower_glb`：L 型角钢截面按规格拉伸（无 shapely 依赖）
- P1-8 `intake/dwg.py.ensure_dxf`：ODAFileConverter/dwg2dxf 转换层

### P2 扫描图续做
- P2-1 `tower_layout.filter_noise_segments`：孤立 dim/图例线过滤（front_hd 498→291，-41.6%）
- P2-2 `tower_layout.calibrate_scale`：`--scale`/`--mm-per-px`/OCR 比例尺
- P2-3 `tower_layout.associate_ocr_labels`：OCR 件号空间关联（tesseract 可选）
- P2-4 `traceability/intake/tower_scan_merge.py` + CLI `merge-scans`
- P2-5 `r_scan_reviewed` 闸门 + CLI `confirm-scan` + `solve-tower --allow-scan`
- P2-6 `examples/tower_scan.pdf` + `intake-scan --tower` 可跑

### 验证
- 测试：60 passed（原 42 + 新增 18 项 P0-P2 验收）
- 命令验证：run-tower / deliver-tower / merge-scans / confirm-scan / pdf / overlay 全部可跑

---

## MLLM 硬约束 / P1 性能 / P2 评测（2026-08）

- `mllm_tower_prompt.py`：只允许 tower_bar/tower_node/drawing_view；非法 kind 按条丢弃
  + parse_warnings（策略 A），`drawing_view` 缺 view_type 记 warning；
  坐标只认 x_px/y_px 或 x/y/z，detail 字符串坐标不采信。
- `mllm_backend.py`：
  - 送图前缩放最长边 ≤2048（`MLLM_MAX_IMAGE_EDGE` 可调 1536），统一 PNG；
  - 单次调用 timeout 默认 90s（`MLLM_TIMEOUT`/`MLLM_CONNECT_TIMEOUT` 可调）；
  - response_format 回退只认「格式不支持」类错误，网络/超时不再重复调用；
  - meta 记录 model/elapsed_s/raw_length/parse_warnings/failure_reason；
  - 新增 `MLLMAnalysisError` 携带 meta，供 run-tower 写入 steps.json。
- `tower_harness.py`：intake 步骤把 MLLM model/raw_length/失败原因/parse_warnings
  写进 steps.json（成功时记录 mllm meta，失败时记录 failure_reason）。
- `benchmark/mllm_vs_scan.py`：同图三列对比（rule-based-scan 候选数 /
  kimi-for-coding 杆件+件号 / k3-256k 杆件+件号），输出
  `examples/external/mllm_benchmark.json`；MLLM 未配置 API 时如实记录 unavailable。
- 测试新增 `tests/test_mllm_perf_bench.py`（16 项）；全量 76 passed。

---

## 闲鱼国网 35A1-JC1-02 件号关联修复（已落地，2026-08）

### 根因
- 旧逻辑「text → 最近 bar，同一 handle 只留最近一个文字」方向反了：
  layer 1（873 杆，70%）没有 TEXT，件号在 layer 0/3；774 个文字到杆中点
  距离全部 <52（TEXT_SNAP=400 够用），但只覆盖约 318 根杆。

### 已实现
- P0 `tower_dxf.py`：改为「bar → 同视图内最近合法 text」，再按距离升序做
  bar-text 一对一贪心（文字不重复用、每杆最多一个文字）。同 handle 多线段
  取最近文字共用件号；无视图规范 / fallback 视图时文本落在杆段 bbox 外
  也全图兜底配对（单视图图纸）。
- P1 正则 `_extract_bar_label` + `_BAR_ID_EXCLUDE_RES`：排除材质
  Q235/Q345/Q420、截面 L40X3/L100X7、螺栓 M16X40 / 1M16X40 / 2M16X50。
- P1 重复件号：保留 `r_no_duplicate_bar_id`（不删规则、不凑 passed）；
  同视图「一號多杆」报告写入 drawing_file：
  `duplicate_bar_id_groups` / `duplicate_bar_id_detail`，杆件带
  `bar_id_dup=true` + `bar_id_primary`（距离最近的杆标 primary）。
  `parse-report` 同步输出 `duplicate_bar_id_groups` / `duplicate_bar_id_detail`。

### DSH 确认结论：B\d 螺栓排除不必为这张图加（已同步）
- 数据：35A1-JC1-02.dxf，INSERT 展开，layer 0/2/3，与 layer_overlay.json text_layers 一致。
- layer 0/2/3 上「纯 ASCII B+数字」明文（B8/B4 等）= 0；`(?<![A-Za-z0-9])B\d` 命中 = 0；
  当前 `\b(...)\b` 的 bar_id_re 捕获 B 开头件号 = 0。
- 之前看到的 B8×13、B4×12 是「无 \b 的 [GSB]\d{1,4}」在 MTEXT 编码串
  `\M+5BAB8\M+5BDD3` 等上的误匹配，不是真实螺栓号；`\b` 边界已挡住（本图 B 开头捕获 0）。
- P1 正则按实测优先级：Q235/Q345/Q420 排除（必须，layer0 35 条）、
  螺栓 `\d*M\d+X\d+` 排除（建议，layer3 63 + layer2 13）、
  截面 `L\d+X\d` 排除（建议，layer0 L40X3 等）——当前实现已全部覆盖。
- 国网 overlay 的 `[GSB]\d{1,4}` 暂不改：本图靠 `\b` 未产生 B 件号；
  若未来去掉 `\b` 会炸，届时可删 GSB 或仅保留 G/S（不在本次动）。

### 验收结果（examples/external/guowang_35A1/35A1-JC1-02.dxf）
- `parse-report --layer-map examples/external/guowang_35A1/layer_overlay.json`：
  bars=1236，labeled=702，association_rate=**0.568**（≥0.30 验收线，也过 0.40 建议线），
  duplicate_bar_id_groups=129。
- `run-tower`：r_topology_closed **passed**；r_no_duplicate_bar_id failed（132 组，
  保留规则、有 primary 消歧报告）；单张总装缺 Z 不强行 export GLB。
- 测试新增 `tests/test_bar_label_association.py`（8 项）；全量 85 passed。

---

## P1 多 Agent 编排（扫描图 / 无 DXF 主路径，已落地，2026-08）

### 目标
不再用「单轮 MLLM 从整图输出 tower_bar + tower_node + from_node/to_node + 坐标」，
改为 Skill 约束下的分步 Agent 链：每步小输出 + Harness 闸门，最后
`to_engineering_model()` 编译。

### 五步链（A0→A4）

| 步 | steps.json id | 职责 | 后端优先级 | 闸门 |
|---|---|---|---|---|
| A0 版面分析 | `a0_layout` | 切视图/BOM/图签 | 规则（tower_layout） | ≥1 drawing_view 或 whole_sheet；无视图 → pending |
| A1 件号 OCR | `a1_labels` | 只 OCR 件号，不画杆 | VLM/MLLM；无 API 跳过 | labels>0 或明确「无文字」；0 字非图签 → pending |
| A2 几何检测 | `a2_geom` | 只检线/节点，不挂件号 | 霍夫线为主（VLM 可选） | bars>0；0 杆（非图签页）→ failed |
| A3 关联匹配 | `a3_link` | bar → 最近合法件号 | **确定性规则** | labeled/bars 率 + 重复件号组数；率<阈值 → pending |
| A4 编译验证 | `a4_harness` | `to_engineering_model()` + Harness | contract + tower_validators | 现有 5 条 + r_scan_reviewed；扫描默认 pending_review |

### 关键实现
- `traceability/intake/tower_agent_pipeline.py`（新增）：`run_tower_agent_pipeline()` 跑 A0→A4，
  返回与 `run_tower` 兼容的结果 dict。
- `traceability/intake/mllm_tower_prompt.py`：拆出 `LABEL_AGENT_PROMPT` / `LABEL_AGENT_SCHEMA`、
  `GEOM_AGENT_PROMPT` / `GEOM_AGENT_SCHEMA` + 按条校验（策略 A：非法条丢弃 + warning）。
- `traceability/intake/mllm_backend.py`：`MLLMBackend.call_agent_json()` 单 Agent 调用，
  记录 `duration_ms`，失败 `meta.failure_reason` 回传；编排层标 pending，不级联猜值。
- `traceability/harness/processing_graph.py`：`ProcessingGraph.pending()` 支持三态闸门
  （passed / pending / failed / skipped）。
- `traceability/harness/tower_harness.py`：`run_tower` 对单文件 PNG/JPG/PDF 扫描图
  分流到 A0→A4；DXF/DWG 仍走 `RuleBasedBackend`，永不调 MLLM。
- A3 关联与 DXF P0 同源：bar → 最近合法件号 + 一对一贪心（距离升序），
  材质/截面/螺栓排除沿用 `_extract_bar_label`。

### MLLM 调用规范（所有 Agent 共用）
- 每 view 裁图长边 ≤2048px；记录 `scale_mm_per_px`（未标定 → placeholder）。
- Prompt 只干一件事；坐标必须在 JSON 数字字段，禁止只写 detail。
- 单 Agent 日志 `duration_ms`；失败 → 该步 pending，不级联猜值。
- 降级：A2 无 API → `TowerScanBackend`（霍夫）；A1 无 API → 跳过，
  A3 只依赖 A2（全 UNLABELED）。

### 验收（examples/clear/tower_front_hd.png，无 MLLM API 环境）
- steps.json 五步齐全：`a0_layout / a1_labels / a2_geom / a3_link / a4_harness`。
- A0 passed（18 drawing_view）；A1 skipped（无 API 不猜值）；A2 passed（bars=291，≥100）。
- A3 pending（无 label，率 0.0 < 0.20，不 failed 凑数）；A4 pending（扫描 pending_review）。
- 模型 291 bars / 374 nodes，全部 `solve_status=pending_review`；
  无坐标不 export strict GLB（无 tower.glb）。
- 全链 `ok=True`（pending 不是 failed）。

### 回归
- `tower_110kv run-tower --bom --merge --golden`：verify 5/5 passed，
  golden 85/85 matched、max_dev=0.011mm。
- 全量测试：90 passed（含新增 `tests/test_tower_agent_pipeline.py` 5 项）。

### 文档
- README.md 新增「无 DXF 扫描图：多 Agent 编排（A0→A4）」一节 + Roadmap 勾选。
