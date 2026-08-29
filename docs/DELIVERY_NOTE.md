# 交付说明（Phase A4）

> 一页讲清：交付什么、**产品主路径 vs 工程旁路**、多模态用在哪、怎么验收。  
> 官网：[仝心圆](https://concentriccirclesmrtt.github.io) · 计划：[`PRODUCT_PATH_AND_AGENT_PLAN.md`](PRODUCT_PATH_AND_AGENT_PLAN.md)

## 产品主路径（应对外）

**可插拔多模态（MLLM）+ Skill + 工程 Agent Harness（A0→A4）**

| 步骤 | 职责 | 后端 |
|------|------|------|
| A0 | 版面 / 视图 | 规则 |
| **A1** | 件号 OCR | **`MLLMBackend`**（`MLLM_PROVIDER` 可换：openai / kimi-code / moonshot 等） |
| A2 | 几何 | ezdxf（hybrid）或霍夫（扫描） |
| A3 | 杆↔件号 | 确定性规则 |
| A4 | 编译 + Harness | Skill + validators |

```bash
# 扫描图
python3 -m traceability.cli run-tower examples/clear/tower_front_hd.png --out-dir out/agent-run

# DXF hybrid（Phase 1）
python3 scripts/run_agent_sheet.py path/to.dxf --out-dir out/hybrid --layer-map overlay.json
```

## 工程旁路（当前 DXF 全册默认）

| 路径 | 说明 |
|------|------|
| `deliver-project` / `run_35A1_jc1_full.py` | 纯 ezdxf + cross_file，**无** Agent steps |
| L0 `canonical.glb` | GIM 完整塔真值 |

## 三层产物

| 层 | 产物 | 来源 |
|----|------|------|
| L0 | `canonical.glb` | GIM / GT |
| L1 | `index.json` | 全册 per-sheet 解析 |
| M3 | `skeleton.glb` | spatial_merge 正交视图 |

## 验收

```bash
bash scripts/acceptance.sh
bash scripts/acceptance.sh --with-mllm   # 多模态 A1 门禁（需 API key）
```

## 环境

| 依赖 | 用途 |
|------|------|
| OpenAI 兼容 SDK | `MLLMBackend`（提供商可配置） |
| ezdxf | 矢量 / hybrid A2 |
| opencv | 扫描 A2 |
| trimesh | GLB |
