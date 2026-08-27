# 交付说明（Phase A4）

> 一页讲清：交付什么、哪条路径是生产、哪条是样例、Kimi 用在哪、怎么验收。

## 三条路径，一句话定位

| 路径 | 输入 | 产物 | 定位 |
|---|---|---|---|
| **矢量主路径（生产）** | 国网 DXF/DWG（`35A1-JC1-02/03` 等） | 杆件/节点/件号关联 + 2D 坐标 | **生产路径**：件号关联率、图层报告、跨文件去重 |
| **扫描样例路径** | `examples/clear/` 位图（front/side/plan/bom） | 霍夫几何候选 + 多视图融合候选 | **样例/演示**：验证 A0→A4 编排与 merge 结构，默认 `pending_review`，不进终版 |
| **Kimi 复核** | 清晰扫描图 | 件号 OCR 候选（A1） | **仅清晰扫描**：辅助件号识别；不替代矢量主路径 |

## 关键结论（不要混淆）

1. **矢量 ≠ 扫描**。国网真实图纸走 DXF 解析（ezdxf），件号关联率约 56.8%（02）/ 84.9%（03）；
   扫描图走霍夫 + VLM，是人工复核候选，`solve_status=pending_review`，无坐标不 export strict GLB。
2. **单立面 ≠ 3D**。`35A1-JC1-02` 是单张正立面图（`view_mode=single_facade`），无法单文件 merge 出
   真 3D。要 3D 需立面/平面分文件（多 DWG 各自带 `view_regions`）走 `merge_view_coordinates`；
   否则按「2D + 件号率」交付。
3. **`--merge` 两种语义**：
   - 单文件多视图（`tower_110kv.dxf`）→ 三视图线性解耦，真 3D。
   - 多文件批量（DWG 目录）→ 只是文件级 ID 前缀拼接，**不是 3D 装配**；额外产出
     `cross_file_bar_id_report`（按 bar_id 跨文件去重）供人工核对，不假装合 3D。

## 交付物清单

| 文件 | 说明 |
|---|---|
| `model.json` | EngineeringModel（组件 + 规则 + 依赖 + staleness） |
| `steps.json` | ProcessingGraph（每步 status/duration/detail） |
| `harness_summary.json` | 五条铁塔规则验证结果 |
| `tower.glb` / `*.obj` | 3D 线框/实体（仅矢量 merge 通过后） |
| `batch_report.json` | 批量接入 per-file 汇总 + 图层 + 跨文件去重 |
| `report.md` | 人类可读报告 |

## 验收命令（一条命令证「没退化」）

```bash
cd engineering-trace
bash scripts/acceptance.sh                 # 全绿：110kV 5/5 + 国网 ≥50% + clear 三 view_type + pytest
bash scripts/acceptance.sh --with-mllm     # 追加 Kimi 门禁（需 KIMI_API_KEY）
```

验收口径：

- `tower_110kv` `--merge` → 五条规则 5/5 passed，金标准偏差 < 2%
- 国网 `35A1-JC1-02` → 件号关联率 ≥ 50%
- `examples/clear/` → front/side/plan 三 view_type 正确 + merged model 存在
- 全量 pytest 全绿
- （可选）`tower_front_hd` + k3 → A1 件号 > 0，A3 关联率 > 3%（修复前基线）

## 环境依赖

| 依赖 | 用途 | 必需 |
|---|---|---|
| ezdxf / numpy / scipy / jsonschema | 矢量解析 + 三视图解耦 | ✅ 核心 |
| opencv-python-headless | 扫描图霍夫/版面分析 | 扫描路径 |
| trimesh | GLB 实体导出 | 3D 导出 |
| openai | MLLM 调用 | 仅 Kimi 复核 |

## 已知边界

- 扫描候选默认 `pending_review`，需人工确认（`confirm_tower_scan` / `--allow-scan`）才进终版。
- A3 关联率受 A2 霍夫噪声影响（图框/标注线误判为杆件会拉低 labeled/bars 比率）。
- 国网单立面图纸的 3D 重构需跨文件组合，属 Phase D 范围。
