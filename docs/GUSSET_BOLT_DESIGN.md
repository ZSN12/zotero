# 节点板与螺栓连接详图设计（Gap 2）

## 现状

求解链产出的是角钢中心线骨架 + 扫掠实体；多根杆件在端点处交汇于同一数学点 `(x,y,z)`。
缺少大样详图、节点板实体与螺栓群验算。

## 目标架构

```
detail DXF / 扫描大样
    │
    ▼
DetailViewTransform（局部→全局）
    │
    ├── GussetPlate（多边形 + 厚度 + 切角）
    │       └── bolt_holes[]
    │
    └── BoltGroup + verify_bolt_group()
            ├── 边距 e1/e2 ≥ 1.2·d0
            ├── 孔距 ≥ 3·d0
            └── 孔数 vs 标注 count
```

## 已实现（本 PR）

| 模块 | 路径 | 能力 |
|---|---|---|
| 大样变换 | `traceability/connection/detail_view.py` | `parse_detail_view_meta`、`local_to_global`、`attach_detail_transform` |
| 节点板 | `traceability/connection/gusset.py` | `GussetPlate`、`parse_gusset_from_detail`、厚度 Dimension |
| 螺栓验算 | `traceability/connection/bolt_verify.py` | `parse_bolt_annotation(2M16X50)`、边距/孔距/干涉规则 |

## 与现有 DXF 管线的衔接点

1. `tower_dxf.py` 中 `node_detail` / `detail` 视图 → 调用 `attach_detail_transform`
2. 从 LWPOLYLINE/CIRCLE 实体提取节点板外轮廓与孔位（待接 ezdxf 遍历）
3. Harness 注入 `r_bolt_group_*` 规则（`inject_bolt_verification_rule`）

## 待办

- DXF 大样自动抽取（多边形/孔圆）并关联全局节点 ID
- GLB 导出节点板薄壳实体（trimesh extrude）
- 国标 GB 50017 边距/孔距公式可配置化（当前用 1.2·d0 / 3·d0 工程近似）

## 验收命令

```bash
python3 -m pytest tests/test_phase_c_gaps.py::ConnectionDetailTest -q
```
