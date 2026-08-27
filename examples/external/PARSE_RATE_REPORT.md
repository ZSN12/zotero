# 外部铁塔图试点解析率报告

> 原则：只报告实测数字，不编造通过。外部图纸图层不规范时，未知图层原样列出，
> 换 `--layer-map` overlay 即可修复，不需要改解析代码。
> 本报告由 `python3 -m traceability.cli parse-report ...` 同款口径生成。

## 输入

- `examples/external/tower_external_demo.dxf`（脱敏外部铁塔图样例）
- `examples/external/guowang_35A1/`（国网 35kV 已转换 DXF 样例，原 DWG 不进 git）

## 解析结果（国网 35A1-JC1，实测 2024）

使用 overlay：`examples/external/guowang_35A1/layer_overlay.json`

| 文件 | 实体总数 | 杆件候选 | 节点 | 件号关联 | 件号关联率 | 图纸类型 |
|---|---|---|---|---|---|---|
| 35A1-JC1-00-1.dxf | 235 | 0 | 0 | 0 | — | title_block（图签页，不进入杆件解析） |
| 35A1-JC1-02.dxf | 4335 | 1236 | 31 | 341 | 27.59% | assembly（总装） |
| 35A1-JC1-03.dxf | 1288 | 305 | 16 | 94 | 30.82% | node_detail（节点） |

注：
- 总装图 `35A1-JC1-02.dxf` 图层为数字 0/1/2/3/4/5/7/8，其中 0 是尺寸界线/刻度短线，
  1-8 为杆件线；text/dim 在 0/2/3。
- `$TD_AUDIT_GENERATED_*` 为 ODA 审计生成的图层，列为未识别，不参与杆件解析。

## 未识别图层清单（35A1-JC1-02）

| 图层 | 实体数 | 处置建议 |
|---|---|---|
| `$TD_AUDIT_GENERATED_(886)` | 35 | ODA 审计生成层，可忽略 |

## 修复方法（只改配置，不改代码）

国网图直接使用 overlay：

```bash
python3 -m traceability.cli intake-tower \
  examples/external/guowang_35A1/35A1-JC1-02.dxf \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out /tmp/35A1-JC1-02_model.json
```

生成解析率 JSON（F3，替代手改 markdown）：

```bash
python3 -m traceability.cli parse-report \
  examples/external/guowang_35A1/35A1-JC1-02.dxf \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out /tmp/parse_report.json
```

批量（A3）：

```bash
python3 -m traceability.cli intake-tower-batch \
  examples/external/guowang_35A1 \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out-dir /tmp/guowang_batch
```

## 说明

- 件号关联率不是 100% 也如实写：真实国网总装图存在大量无编号的板件轮廓/螺栓/尺寸线。
- 图签页 `00-*` 标记 `drawing_kind=title_block`，不计入「杆件解析失败」。
- 低解析率写进报告，不通过改验证器来凑 passed。
