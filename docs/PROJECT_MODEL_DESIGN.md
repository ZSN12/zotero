# ProjectModel 与多模块装配设计（Gap 1 / Phase F）

## 背景

当前 `EngineeringModel` 以单张图纸/单文件为边界。真实高压塔工程通常包含 5~20+ 张分册：
总装、塔腿段、塔身段 M1–M6、横担/地线支架、节点大样等。Gap 1 引入 **ProjectModel**
作为图册级 IR，在不破坏现有单文件主路径的前提下扩展跨文件能力。

## 架构

```
ProjectModel
├── sheets[]          每张分册（path, kind, module_id, model_path, evidence_count）
├── modules{}         模块段 M1..Mn（拼接面、依赖关系）
├── assembly_joints[] 模块对齐报告
└── metadata

EngineeringModel (per sheet)  ──►  merge_cross_file_views  ──►  finalize(merge=True)
                                      │
ProjectModel.assemble_modules() ◄─────┘  （多段已解 3D 模块的 Z 向堆叠）
```

## 已实现（本 PR）

| 模块 | 路径 | 能力 |
|---|---|---|
| ProjectModel | `traceability/project/model.py` | 目录批量索引、证据链计数、`build_project_from_directory` |
| Assembly Solver | `traceability/project/assembly.py` | 模块 top/bottom 边界节点 XY 对齐、`assemble_modules` |
| BOM 树汇总 | `traceability/project/bom_tree.py` | 跨 sheet `bom_row` 按 bar_id 聚合、与 master BOM 数量核对 |
| 跨文件视图合并 | `traceability/intake/tower_batch.py` | `merge_cross_file_views` + `cross_file_batch` |

## 待办（后续迭代）

1. **法兰/插板语义**：从节点大样 DXF 解析拼接面多边形，替代纯 Z 极值边界
2. **模块依赖 DAG**：M3 依赖 M2 解算完成才允许 `assemble_modules`
3. **图册级 Harness**：跨 sheet 的 `r_no_duplicate_bar_id` 与 BOM 树 conflict 联动
4. **Web 工作台**：Project 视图展示多 sheet 步骤与模块装配报告

## CLI 入口（规划）

```bash
# 构建 ProjectModel 索引
python -m traceability.cli build-project examples/external/guowang_35A1/ \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out-dir out/project

# 跨文件真 3D 合并（Phase D）
python -m traceability.cli cross-file-batch examples/external/guowang_35A1/ \
  --layer-map examples/external/guowang_35A1/layer_overlay.json \
  --out-dir out/cross-file
```

## 数据准备（VLM 微调）

见 `scripts/prepare_vlm_dataset.py`：从 ProjectModel 导出 `(image, label_json, source_ref)` 三元组，
供领域 VLM 微调（Phase F 长期项，不阻塞主链路）。
