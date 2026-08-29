# Kimi 多模态「整塔识别」验证记录 —— 供 Cursor 复核

> 结论先行：**文档里「DXF 走 ezdxf、MLLM 只读件号」的 hybrid 路径，对真实国网图纸是失效的；
> 正确方向是让 Kimi 直接识别整塔几何（bars/nodes），这条路我已用真实图验证跑通。**

---

## 一、背景与触发问题

真实国网数据 `35A1-JC1`（49 个 DWG，位于
`~/Downloads/输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)/35A/35A1/35A1-JC1/`）。

先跑了一条命令：

```bash
python3 -m traceability.cli intake-tower-batch \
  ~/Downloads/.../35A/35A1/35A1-JC1/ \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out-dir out/jc1-batch --merge
```

**结果**：49 张图里只有 1 张（`35A1-JC1-02`）解析出杆件（61 根），其余 48 张全是 0 杆件；
合并后仅 15 根有效杆件、10 个节点；6 条验证规则全部 `pending`。

## 二、根因（代码定位）

### 根因 1：DXF 后端选择被硬编码，Kimi 根本不会被调用

`traceability/intake/mllm_backend.py` 的 `choose_backend()`：

```python
# 第 412-413 行
if drawing.kind in ("dxf", "dwg"):
    return RuleBasedBackend()   # 无条件走 ezdxf，忽略 MLLM 配置
```

CLI 里 `--backend mllm` 只在 `tower + png/jpg/pdf` 分支生效（`cli.py:290-300`），
对 `dxf/dwg` 分支（`cli.py:321-330`）完全无效——`backend.analyze()` 永远走 `RuleBasedBackend`。

**后果**：不管怎么配 Kimi，DXF 都走 ezdxf 规则解析。

### 根因 2：ezdxf 图层映射对国网图纸失效

国网图纸把杆件几何和文字几乎全画在 `layer "0"` 上（默认层），
而 `layer_overlay.json` 配置的杆件层是 `1/2/3/4/5/7/8`。

实测（`batch_report.json`）：
- `35A1-JC1-01-1`：1263 实体全在 layer `0`（636 LINE + 40 LWPOLYLINE + 105 DIMENSION）→ **0 杆件**
- `35A1-JC1-01-2`：1977 实体全在 layer `0`（1624 TEXT + 258 LINE，疑似 BOM 表）→ **0 杆件**
- `35A1-JC1-03~46`：节点详图，几何画在 `1~8` 层但被判定 `parse_bars=False` 或识别不到 → **0 杆件**
- 只有 `35A1-JC1-02` 恰好把几何分层到了 `1~8` → 唯一解析出杆件的图

**后果**：ezdxf 几何提取在这套图上大面积失效，48/49 张图无几何产出。

### 根因 3：A2 几何检测对扫描图也硬编码走霍夫线

`traceability/intake/tower_agent_pipeline.py` 的 A2 几何检测（第 720-749 行）：

```python
# 第 732 行
bars, nodes, geom_meta = _detect_geometry(...)   # 霍夫线检测，method="hough"
```

A1 件号走 MLLM，但 A2 几何检测走霍夫线（`_detect_geometry`，第 141 行起），
`GEOM_AGENT_PROMPT`（Kimi 几何检测）已经写好但**没有在任何路径被真正调用**。

同样，`hybrid_dxf_agent.py` 的 A2 也是 ezdxf 优先，只有 ezdxf 0 杆时才回退到 `hough_fallback`
（第 481-514 行），MLLM 几何检测从未被使用。

## 三、验证：Kimi 整塔识别是否可行

### 环境

- Key 在 `~/.zshrc` 里：`KIMI_API_KEY=sk-kimi-...OCVP`（MLLM_PROVIDER=kimi-code）
- 视觉模型：`k3-256k`（256K 上下文）；`k3`（1M 上下文）也可用
- Kimi `/models` 接口确认 `kimi-for-coding` / `k3` / `k3-256k` 全部 `supports_image_in: true`

### 测试 1：整图识别（`TOWER_MLLM_PROMPT`）

把 `35A1-JC1-02.dxf` 用 `render_dxf_preview_with_mapping()` 栅格化成 PNG
（1196×735），整图喂给 Kimi 的 `TOWER_MLLM_PROMPT`。

**结果**：✅ Kimi 正确识别出图纸里多个视图区域（塔身主立面 + 并列的其它塔段/详图），
描述准确（如"方形塔身横截面大样，四根主材+斜材"）。

> 更正：此前写「front / side / plan 详图」不准确。后续 ezdxf 几何聚类 + OCR 交叉验证
> 表明 02 图是**多个塔段/视图横排**（OCR 描述为「左塔 + 右塔对称布置」），**没有独立的
> side 立面**。3D 应走「单立面 + 四向镜像」（`expand_4_face_symmetry`），详见
> [`DXF_DATA_READING.md`](DXF_DATA_READING.md)。

但**整图级别 Kimi 只返回 `drawing_view`，没有输出杆件级 `tower_bar/tower_node`**——
因为整页含图框、标题栏、材料表、多个详图，密度太高，Kimi 倾向于"概括"而非逐根提取。

### 测试 2：放大裁剪后几何检测（`GEOM_AGENT_PROMPT`）

把 02 号图左侧主立面裁出来（整图左 42% 宽度）并 2 倍放大（1004×1470），
喂给 Kimi 的 `GEOM_AGENT_PROMPT`（A2 几何检测）。

**结果**：✅ **成功输出 32 根杆件（bars）+ 28 个节点（nodes），坐标成对数字**
（`x1/y1/x2/y2`、`x_px/y_px`），完全符合 A2 输出契约。示例：

```json
{"bar_uid": "bar_0001", "x1": 452, "y1": 524, "x2": 392, "y2": 1136},
{"bar_uid": "bar_0003", "x1": 452, "y1": 524, "x2": 524, "y2": 524},
...
{"node_id": "N001", "x_px": 452, "y_px": 524}
```

## 四、结论

1. **「识别整塔几何」这条路走通了**：Kimi 能从栅格化 DXF 图里直接读出杆件级几何
   （bars/nodes），不依赖失效的 ezdxf 图层解析。

2. **文档有几处需要修正**：
   - 视觉模型应明确用 `k3-256k`（`MLLM_MODEL=k3-256k`），**不要用 `kimi-for-coding`**。
     后者是编码模型，虽标 `supports_image_in: true` 但读图慢（实测整图 292s）且易
     只返回粗略概括；`k3-256k` 读几何仅 68s、输出杆件级坐标。`~/.zshrc` 里当前
     `MLLM_MODEL=kimi-for-coding` 需改成 `k3-256k`。
   - "hybrid A2 走 ezdxf"是当前实现，但对图层失效的真实图纸不是正确方向；
     A2 几何也应交给 MLLM。

3. **需要改的代码位置**：
   - `mllm_backend.py:412-413`：`choose_backend()` 对 dxf/dwg 硬编码 `RuleBasedBackend`，
     应支持显式选择「栅格化 + MLLM」后端。
   - `tower_agent_pipeline.py:720-749`：A2 几何检测应支持 MLLM（`GEOM_AGENT_PROMPT`），
     而非只走霍夫。
   - `hybrid_dxf_agent.py:481-514`：A2 应支持 MLLM 几何，而非 ezdxf→hough 兜底。
   - CLI `--backend mllm` 对 dxf/dwg 分支目前不生效（`cli.py:321-330`）。

## 五、建议的下一步（待定方向）

- **方案 A（改动小）**：独立脚本，对关键立面图做「栅格化 → 切视图 → Kimi 几何+件号
  → A3 关联 → EngineeringModel」，验证完整闭环（能否过 Harness、生成 3D）。
- **方案 B（改动大）**：改 `choose_backend()` + hybrid 管线，让 DXF 显式支持
  「栅格化 + MLLM 整塔识别」后端，写进 CLI。

## 附：验证用到的关键调用（可复现）

```python
# 栅格化 DXF
from traceability.intake.hybrid_dxf_agent import render_dxf_preview_with_mapping
m = render_dxf_preview_with_mapping('35A1-JC1-02.dxf', '02_preview.png', dpi=150)

# Kimi 几何检测
from traceability.intake.mllm_backend import MLLMBackend, _encode_image
from traceability.intake.mllm_tower_prompt import GEOM_AGENT_PROMPT
mllm = MLLMBackend()   # 读环境变量 KIMI_API_KEY / MLLM_MODEL=k3-256k
image_b64, _ = _encode_image('02_front_crop.png')
client = mllm._make_client()
resp = client.chat.completions.create(
    model=mllm.model,
    messages=[{'role':'user','content':[
        {'type':'text','text':GEOM_AGENT_PROMPT},
        {'type':'image_url','image_url':{'url':f'data:image/png;base64,{image_b64}'}}
    ]}],
    response_format={'type':'json_object'},
    max_tokens=8000,
)
```

注意：几何检测调用需 `max_tokens` 较大（杆件坐标多），且单次耗时 >60s，
需后台跑或调大 `MLLM_TIMEOUT`。
