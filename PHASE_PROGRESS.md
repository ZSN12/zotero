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

---

## 多视图语义打通 + 多文件编排（P0/P1/P2 补全，已落地，2026-08）

> 本轮目标：把铁塔管线从「单文件 / 单视图」打通为「多视图语义 + 多文件编排」，
> 并以国网 35A1 真实图纸（矢量 DXF 为主）+ 扫描图（VLM 复核）验证。

### 现状诊断（改造前实测）

| 能力 | 状态 |
|---|---|
| 矢量 `tower_110kv.dxf` + `--merge` | ✅ 三视图合 3D 已验证（85 节点，max_dev=0.011mm） |
| 国网 `35A1-JC1-02/03` | ⚠️ 能 parse，但 `layer_overlay.json` 里 `view_regions` 为空，全是 `view_type=drawing`，无法 `--merge` 出 3D |
| 扫描 agent（A0→A4） | ⚠️ 单 PNG；A1 VLM 坐标未还原，关联率个位数（5.14%） |
| `examples/clear/` | 已有 front/side/plan/bom 分文件，无批量跑 + 合并 |
| `merge-scans` | 仅 front+side，无 plan、无 agent 链 |

### 关键结论：国网 02/03 是单视图图纸，不强行 merge 出 3D

用 ezdxf 分析真实图面（INSERT 展开后按 bar_layers 统计）：

- `35A1-JC1-02`（总装）：杆件 x 集中在 34350~34886（宽 536）、y -7678~-7244（高 434），
  是**单张正立面图**；图面左侧 11114~34375 的实体是图层 0 的图框/尺寸线（2546 个实体），
  非杆件。文字全是截面 L40X3 / 材质 Q345 / 螺栓 M16X40，无「立面/平面/剖面」标题
  （中文标题是 SHX 转义码 `\M+5BAF...`）。
- `35A1-JC1-03`（节点大样）：杆件 x 34383~34717、y -8332~-8006，`1:100` 比例 + 螺栓标注密集。

结论：02/03 不像 `tower_110kv` 那样一张图排布 front+side+plan 三视图，
**无法靠单文件 merge 出 3D**。产品路径 = 矢量主路径（02 件号关联 ≈56.8%、
03 ≈84.9%）+ 扫描/VLM 复核，不替代矢量主路径。

### P0 — 多视图语义打通（已落地）

| # | 步骤 | 落地文件 | 验证 |
|---|---|---|---|
| P0-1 | DXF view_regions：`35A1-JC1-02` 配 `front` 单立面、`35A1-JC1-03` 配 `detail`+空 axes；无 overlay 时 `_infer_assembly_views()` 按图面结构推断（assembly 左右两簇切 front/side，否则单 front；node_detail → detail） | `examples/external/guowang_35A1/layer_overlay.json` + `tower_dxf.py` | 02 → 1235 杆全 `front`（原 1236 全 drawing）；03 → detail 不产杆件；有图层无 view_regions 时 02 自动推断 front |
| P0-2 | 扫描图按文件名标 view_type：`tower_scan_views.infer_scan_view_meta()` 从 stem 推断 front/side/plan+z_level/bom/node；接入 A0，drawing_view/tower_bar/tower_node 不再硬编码 `view_type="drawing"` | `tower_scan_views.py` + `tower_agent_pipeline.py` | `tower_front_hd`→front、`tower_plan_z8100_hd`→plan+z_level=8100、`tower_bom_hd`→bom（不 parse）、`tower_node_k1_hd`→detail（不 parse） |
| P0-3 | A1 VLM 坐标还原：`_labels_to_full_image()` 按 `source_crop_size/crop_size`（≥1）放大 + crop 左上角 bbox 偏移还原到整图（方向取反会致件号坐标偏 2~3 倍） | `tower_agent_pipeline.py` | 真实扫描图 A1 识别 42 件号（此前关联率 5.14% 的根因之一） |

### P1 — 多文件编排（已落地）

| # | 步骤 | 落地文件 | 验证 |
|---|---|---|---|
| P1-1 | 扫描目录批量：`scan_dir_files()` 按文件名分组 + `intake_scan_batch()`（front+side → merge_scan_views，plan 写 z_level，跳过 bom/node） | `tower_scan_views.py` | `examples/clear/` → front(291杆)+side(272杆) merge 339 节点候选；3 plan 写 z_level；bom/node 正确跳过 |
| P1-2 | `run-tower` 目录识别：全是 PNG/PDF → 扫描批量；含 DWG/DXF → 现有 `intake_tower_batch`；`_dir_has_cad_files()` 分流 | `tower_harness.py` | CLI `run-tower <dir>` 端到端跑通（禁用 MLLM 时 A1 跳过，front+side merge 正常） |
| P1-3 | `_model_stem()` 优先从 `drawing_file.drawing_view`/path stem 取，回退 model.name：修复 batch-merged 模型对不上 overlay 的 view_regions | `tower_views.py` | `tower-batch-merged` 模型正确取到 `35A1-JC1-02` stem |

### P2 — 关联与配置（已落地）

| # | 步骤 | 落地文件 | 验证 |
|---|---|---|---|
| P2-1 | A3 同 view_type 配对 + 中点距离；A2 产出 bar/node 注入 view_type（否则 `_view_of(bar)` 恒 None，同视图配对失效）；删 `_dist_point_segment` 死代码 | `tower_agent_pipeline.py` | side 视图杆只匹配 side 件号（不跨视图抢号） |
| P2-2 | MLLM 配置：`MLLM_TIMEOUT` 默认 300s、`MLLM_CONNECT_TIMEOUT` 30s、`MLLM_MAX_IMAGE_EDGE` 默认 2048；`resolve_mllm_config()` 显式空 key 可禁用（隔离环境变量，修复 `MLLMBackend(api_key="")` 被环境覆盖） | `mllm_backend.py` + `mllm_providers.py` | `MLLMBackend(api_key="")` → available=False；默认构造仍读 `KIMI_API_KEY` |

### 测试与回归

- 修复 `tests/test_mllm_contract.py`：`test_choose_null_when_no_api_for_scan` 显式传
  `mllm=MLLMBackend(api_key="")`，隔离宿主环境变量（否则环境有 KIMI_API_KEY 时
  拿到 MLLMBackend 而非 NullBackend）。
- 非 API 测试 58 项全绿：test_bar_label_association(8) + test_tower_intake(7) +
  test_traceability(7) + test_tower_110kv(11) + test_mllm_contract(6) +
  test_p0_p2_features(19)。
- `tower_agent_pipeline` 测试因含真实 API 调用（`test_scan_model_pending_review`）在
  有 KIMI_API_KEY 环境会阻塞/超时，需在无 API 环境或显式禁用 MLLM 下跑。

### 遗留与后续建议

1. **02/03 的 3D 重构**：单视图图纸无法单文件 merge 出 3D，需跨文件组合（02 立面 +
   03 节点大样 + 其它平面图）或人工标定尺度。当前按「矢量主路径做件号关联 + 扫描复核」交付。
2. **A3 关联率仍受 A2 噪声影响**：霍夫把图框/标注线误判为杆件（真实扫描图 545 候选杆，
   真实件号仅数十），`MIN_ASSOCIATION_RATE=0.20` 在杆件基数虚高时永远 pending。
   建议 A2 加「超长连续直线（图框）/ 贴图边界线段」过滤，或 A3 改用
   「OCR 件号中成功关联比例」而非「labeled/全部候选杆」。
3. **`MLLM_MAX_IMAGE_EDGE` 默认值不一致**：`mllm_backend.py` docstring 写 1536、
   实际代码默认 2048，建议统一。
4. **git 管理**：已推送远程 `https://github.com/ZSN12/zotero`（`main` 分支，见下「发布与验收」）。

---

## 缺陷清单修复收尾 + 发布（2026-08，commit 44db5c3）

> 本轮按用户 P0/P1/P2 缺陷清单逐条核对并补齐缺口，最终推送到 GitHub。

### P0 收尾

| # | 项 | 落地 |
|---|---|---|
| P0-1 | `merge_view_coordinates` 读不到 overlay | `merge_view_coordinates/merge_view_bars/finalize_tower_model` 增加 `overlay`/`layer_map_path` 并全链路下传（`tower_harness.run_tower`、`cli.py` 的 `finalize_tower_model` 调用点）；`_region_meta(stem, overlay=...)` 透传给 `view_regions` |
| P0-2 | 国网 02 只有 front 无法真 3D | `extract_tower_from_dxf` 末尾写 `view_mode`（single_facade/multi_view/no_view）+ `view_kinds` 到 `drawing_file`，明确「02 只做 2D+件号率，3D 靠立面/平面分文件」；实测 02 parse-rate 0.5684 ≥ 0.50 |
| P0-3 | 扫描关联率验收 | 默认 `k3-256k`（kimi-code preset）已确认；`--backend mllm` 单 PNG 路径验证可跑（无 API 时 A1 skip）；「≥15%」需真实 API key，命令固化进验收脚本 |
| P0-4 | 多 plan 互相覆盖 | `intake_scan_batch` 改按 `(view_type, z_level)` 存模型；新增 `_attach_plan_nodes` 把各 plan 节点 `view_x/view_y/z_level` 并入合并模型 |
| P0-5 | DWG 批量 --merge 不做视图合并 | 新增 `cross_file_bar_id_report`，多文件 merge 输出「按 bar_id 跨文件去重报告」而非假装合 3D；steps.json `batch` 步骤暴露 `cross_file_duplicate_count` |

### P1 收尾

| # | 项 | 落地 |
|---|---|---|
| P1-6 | A2 霍夫杆未写 view_type | 新增 `_assign_view_by_bbox`：A2 后按 A0 视图 bbox 给杆/节点归属 view_type，A3 同视图过滤对多 region 单图生效 |
| P1-7 | parse_bars=False 未短路 | bom/node/大样短路 A1/A2，A3 空跑记 passed，A4 只记 metadata |
| P1-8 | LABEL_AGENT_PROMPT 坐标说明 | 明确 `x_px/y_px` 是裁剪缩放图左上角 (0,0) 的像素坐标 |
| P1-9 | 扫描批量与 Harness 对齐 | `intake_scan_batch` 返回完整 `ProcessingGraph`（每文件一步 + merge_scan + a4_harness），`run_tower` 复用 |

### P2 收尾

| # | 项 | 落地 |
|---|---|---|
| P2-10 | web 2D 证据层 | `web/index.html`+`app.js`+`styles.css` 新增 2D 面板：点 `tower_bar` 用 `x1_px/y1_px/x2_px/y2_px` 画线高亮 + 列表双向高亮，可选源图铺底 |
| P2-11 | 验收脚本固定化 | `scripts/acceptance.sh`：110kv 5/5 + 金标准、guowang02 ≥50%、clear 扫描批量 3 view_type + merged model、pytest 全量 |

### 发布

- 修复陈旧测试 `test_timeout_env_default_is_90`（90s → 300s）。
- 新增 `tests/test_p0_p1_fixes.py`（7 项回归）。
- 全量测试 **100 passed**；`scripts/acceptance.sh` 4 项全绿。
- 已推送远程 `https://github.com/ZSN12/zotero`（`main` 分支，HEAD=44db5c3）。

### 遗留（待外部依赖）

- **P0-3 关联率 ≥15%**：需配置 `MLLM_PROVIDER=kimi-code KIMI_API_KEY=sk-...` 后跑
  `run-tower examples/clear/tower_front_hd.png --backend mllm`；当前环境无 key，A1 会
  skip 致关联率 0.0。代码与验收命令均已就绪。
