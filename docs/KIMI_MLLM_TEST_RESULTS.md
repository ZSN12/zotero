# 35A1-JC1 真实图纸 + Kimi MLLM 测试结论（供 Cursor 复核）

> 用真实国网 35A1-JC1 CAD 文件，对「hybrid DXF + Kimi MLLM 整塔识别」路径做端到端测试。
> 结论：**方向正确、已跑通，但发现并修复了 3 个真实 bug；仍有 3 个待 Cursor 处理的设计问题。**

---

## 一、测试结论（一句话）

Kimi MLLM 整塔识别**在 ezdxf 失效的真实国网图纸上成功救回了杆件**——01-1 图 ezdxf 解析 0 杆，
Kimi 直接识别出 **53 根杆件 + 28 节点**，全部 `geometry_origin=mllm_geom`，已贴号 53/53。

---

## 二、我修复的 3 个 bug（都在 `traceability/intake/hybrid_dxf_agent.py`）

### Bug 1：`graph.finish()` 关键字参数重复冲突（`method` / `bars` / `nodes`）

`_mllm_detect_geometry()` 返回的 `meta` 里含 `method`、`bars`、`nodes` 三个键，
而 A2 分支又显式传了 `method=a2_method`、`bars=...`、`nodes=...`，导致 Python 调用期
`TypeError: got multiple values for keyword argument 'xxx'`。

**报错现象**：A2 步骤直接 `failed`，整条链 `ok=False`。

**修复**：在展开 `**mllm_geom_meta` / `**hough_meta` 前，剔除会与显式参数冲突的键：

```python
# mllm 分支（原第 580-583 行）
graph.finish(
    bars=len(bars), nodes=node_count,
    ezdxf_bars=bar_count,
    **{k: v for k, v in mllm_geom_meta.items()
       if k not in ("method", "bars", "nodes")},
    method=a2_method,
)

# hough 分支（原第 601-605 行）同理
graph.finish(
    bars=len(bars), nodes=hough_meta.get("nodes_px", 0),
    vector_bars=bar_count,
    **{k: v for k, v in hough_meta.items()
       if k not in ("nodes_px", "method", "bars", "nodes")},
    method=a2_method,
)

# 两个 graph.fail(...) 分支同理剔除 method/bars/nodes
```

### Bug 2：`EngineeringModel.add_component()` 签名错误

`_inject_mllm_bars_into_model()` 里用了错误的调用方式：

```python
# 错误（原代码）
model.add_component(nid, "tower_node", f"节点 {nid}", properties={...})
# 报错：EngineeringModel.add_component() got an unexpected keyword argument 'properties'
```

**正确签名**是 `add_component(self, c: Component)`，接收一个 `Component` 对象：

```python
# 修复后
model.add_component(Component(
    id=nid, name=f"节点 {nid}", kind="tower_node",
    source=SourceRef(SourceType.DRAWING, "mllm_geom", confidence=0.6),
    properties={...},
))
```

同时补了 `Component / SourceRef / SourceType` 的 import（原来 `from ..model import` 只导了
`EngineeringModel, ValidationStatus`）。

### Bug 3：A2 分支顺序导致 MLLM 几何被丢弃

原逻辑 `if not ezdxf_bars:` 才注入 MLLM 杆件，导致 **02 号图（ezdxf 能解析出 61 杆）时，
Kimi 的 31 根 MLLM 杆件被丢弃**，最终模型仍用 ezdxf 结果。这是设计问题（见下），不是纯 bug。

---

## 三、测试结果对比

| 图 | ezdxf 杆件 | Kimi MLLM 杆件 | 最终模型 | 结论 |
|---|---|---|---|---|
| 35A1-JC1-02（总装图，分层 1~8） | 61 | 31 | 61（用 ezdxf） | MLLM 未生效（Bug 3） |
| 35A1-JC1-01-1（塔型一览图，全在 layer 0） | **0** | **53** | **53（全 mllm_geom）** | ✅ MLLM 救回 |

- 02 号图最终 `ok=True`，但 A4 `pending`（`r_node_fully_solved` / `r_no_duplicate_bar_id` failed）
- 01-1 号图最终 `ok=True`，A4 `pending`（同样两条规则 failed）

---

## 四、仍待 Cursor 处理的 3 个设计问题（非本次修复范围）

### 问题 1：`choose_backend()` 对 dxf/dwg 仍硬编码走 ezdxf

`traceability/intake/mllm_backend.py:412-413`：

```python
if drawing.kind in ("dxf", "dwg"):
    return RuleBasedBackend()   # 无条件 ezdxf，忽略 MLLM 配置
```

所以 `compile-drawing` / `run-tower` 对 DXF **仍不会触发 Kimi**。本次测试只能绕过它，
直接用 `scripts/run_agent_sheet.py`（内部调 `run_hybrid_dxf_agent_pipeline`）。
**要让 DXF 走 Kimi，必须改这里或加 `--backend mllm` 对 dxf 的显式分支。**

### 问题 2：BOM 数据是空的 → 两条规则永远 pending

`examples/external/guowang_35A1/guowang_merged_bom.csv` 的 `section`、`length_mm` 全是空/0，
导致 `r_bom_length_match`、`r_bom_section_match` 永远 `pending`。
需要从图纸材料表（01-2 图有 1624 个 TEXT，疑似就是 BOM）提取真实截面/长度。

### 问题 3：`r_node_fully_solved` / `r_no_duplicate_bar_id` 两条规则 failed

- `r_node_fully_solved`：MLLM 几何只有 2D 坐标（x/y，来自 pixel 转 mm），**无 z 轴**，
  所有节点 `z=null` → failed。这是预期的——MLLM 单视图只能给 2D，3D 需多视图合并。
- `r_no_duplicate_bar_id`：Kimi 在多个视图/详图里识别出重复件号（如 '30'、'6'、'13' 在
  不同 view 重复），这是「塔型一览图」多塔对比导致的正常现象，但被规则判 failed。

---

## 五、对 Cursor 的建议

1. **改 `choose_backend()`**：让 DXF 在 `--backend mllm` 或配置了 MLLM key 时，走
   「栅格化 + hybrid MLLM」路径（而不是永远 ezdxf）。
2. **修 BOM**：从图纸提取真实截面/长度，或至少让空 BOM 的规则降级为 `pending` 而非影响闸门。
3. **明确 MLLM 2D 几何的定位**：`r_node_fully_solved` 对「纯 MLLM 单视图 2D」应标
   `pending`（等待多视图/人工补 z），而不是 `failed`——否则纯 MLLM 路径永远被判失败。
4. **处理重复件号**：多塔一览图（01-1）的重复 bar_id 是跨塔正常现象，应加「按 view 分组」
   或「塔型区分」再判重，而不是全局判重。

---

## 六、可复现命令

```bash
# 环境
export KIMI_API_KEY='sk-kimi-...OCVP'
export MLLM_PROVIDER=kimi-code
export MLLM_MODEL=k3-256k

# 02 总装图（ezdxf 能解析，MLLM 作对照）
python3 scripts/run_agent_sheet.py out/jc1-batch/dxf/35A1-JC1-02.dxf \
  --out-dir out/jc1-02-hybrid \
  --layer-map examples/external/guowang_35A1/layer_overlay.json --dpi 200

# 01-1 塔型一览图（ezdxf 失效，MLLM 救回 53 杆）
python3 scripts/run_agent_sheet.py out/jc1-batch/dxf/35A1-JC1-01-1.dxf \
  --out-dir out/jc1-01-1-hybrid \
  --layer-map examples/external/guowang_35A1/layer_overlay.json --dpi 200
```

注意：MLLM 几何检测单次 60~70 秒，需后台跑或调大 `MLLM_TIMEOUT`。
