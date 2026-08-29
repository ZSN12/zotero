# 产品路径 vs 实现路径 + Agent 缺口计划

> 对齐官网：[仝心圆](https://concentriccirclesmrtt.github.io)  
> 多模态后端通过 `MLLMBackend` 可插拔（`MLLM_PROVIDER` / `MLLM_MODEL`），**不绑定单一厂商**。

## 产品主路径

```
多源图纸（DXF / DWG / PDF / 扫描图）
  → A0 版面 → A1 多模态件号 → A2 几何 → A3 规则关联 → A4 Skill + Harness
  → EngineeringModel + steps.json + 证据链
```

| 步骤 | 后端 |
|------|------|
| A1 件号 | **MLLMBackend**（OpenAI 兼容；提供商可换） |
| A2 几何 | ezdxf（DXF hybrid）或霍夫（扫描） |
| A3 关联 | 确定性规则 |
| A4 | Skill 契约 + tower_validators |

## 工程旁路（当前 JC1 全册默认）

`deliver_project` → `cross_file_batch` → 纯 ezdxf，无 Agent steps。

## Phase 1 — Hybrid 单张 ✅ 已落地

| 文件 | 说明 |
|------|------|
| `traceability/intake/hybrid_dxf_agent.py` | DXF 矢量 A2 + MLLM A1 + A3 + A4 |
| `scripts/run_agent_sheet.py` | CLI 入口 |

```bash
export MLLM_PROVIDER=openai    # 或 kimi-code / moonshot
export MLLM_MODEL=gpt-4o       # 视觉模型
export OPENAI_API_KEY=sk-...

python3 scripts/run_agent_sheet.py \
  out/xianyu-acceptance/batch-jc1/dxf/35A1-JC1-02.dxf \
  --out-dir out/jc1-02-hybrid \
  --layer-map examples/external/guowang_35A1/layer_overlay.json
```

## 后续

- **Phase 2**：`deliver_project(agent_mode="hybrid")` 图册批处理  
- **Phase 3**：M1–M6 TraceLink + Evidence UI  
- **Phase 4**：`acceptance.sh --with-agent`

## 责任边界

- **L0 GIM** = 完整塔 3D 真值  
- **Hybrid Agent** = 施工图编译 + 件号关联 + Harness（可换 MLLM 提供商）  
- **ezdxf 旁路** = 开发回归，非对外产品终态
