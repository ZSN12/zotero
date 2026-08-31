# 任务书：3D 可视化升级（Phase 6.1–6.3：diff 视图 + 分层查看 + 统计面板）

> 你将接手一个独立子任务。本任务与主线程（评测口径/几何管线）**零文件冲突**，
> 可完全并行。禁止改动文末「冲突红线」列出的文件。

---

## 0. 项目背景（自包含，无需其他上下文）

- **仓库**：`/Users/zsn/Documents/zotore/engineering-trace`（Python 3.10 + 原生 JS/three.js，无 node 构建）
- **项目**：输电铁塔 DXF 图纸 → 3D 模型重建系统（35A1-JC1 塔，杆件约 1499 根 / 节点 905 / 高 36.6m）
- **当前 viewer**：`web/demo/35A1-JC1/compare.html`（100 行，three.js 0.160 CDN，
  仅两个按钮切换「GT 官方塔 GLB」vs「DXF 重建骨架 GLB」，无分层、无统计、无 diff）
- **本地预览**：`python3 -m http.server 8000`（仓库根目录）→
  `http://127.0.0.1:8000/demo/35A1-JC1/compare.html`
- **测试**：`python3 -m pytest tests/`（当前 354 passed，不得回退）

## 1. 可用数据资产（全部已存在，直接读）

| 文件 | 内容 |
|---|---|
| `out/35A1-JC1-full-deliver/model.json` | 当前版本模型（组件含 properties：geometry_origin/face/role/bar_id/source_file/geometry_class） |
| `out/35A1-JC1-baseline/model.json` | **冻结基线**（与上者 diff 的 A 侧；git b7c8630 时产出） |
| `out/35A1-JC1-full-deliver/skeleton.glb` | 当前骨架 GLB（1261KB，L 型角钢实体，provenance 五色：绿=识别/蓝=辅助重建/黄=拼接/灰=派生） |
| `out/35A1-JC1-full-deliver/skeleton.bar_map.json` | GLB 杆件映射表：`[{bar_id, component_id, role, geometry_origin}, ...]`（1504 条） |
| `out/35A1-JC1-full-deliver/metrics_multi_caliber.json` | A2 五层口径 sweep（pure/reconstructed/level_assisted/parametric/full） |
| `out/35A1-JC1-full-deliver/metrics_by_role.json` | 分角色统计（leg/diagonal/depth_diag/horiz_x/y_member） |
| `out/35A1-JC1-full-deliver/metrics_by_origin.json` | 分来源 TP/FP |
| `out/35A1-JC1-full-deliver/evidence_report.json` | 匹配对追溯（每杆 distance_mm/length_ratio/z_mid_mm） |
| `out/35A1-JC1-full-deliver/review_queue.json` | 悬空复核节点（2 物理） |

组件 properties 关键字段（model.json）：
```json
{
  "geometry_class": "recognized | reconstructed | derived",
  "geometry_origin": "dxf_geom | collinear_stitch | diaphragm_reconstructed | panel_subdivision | derived_4face",
  "face": "f | b | l | r | diaphragm | corner",
  "role": "LEG | DIAG | HORIZ | CROSS",
  "source_file": "35A1-JC1-02 | -04 | -05 | -06 | -07",
  "bar_id": "616 或 UNLABELED_*",
  "from_node"/"to_node": "节点id"
}
```
节点坐标在 tower_node 组件 properties 的 x/y/z。

## 2. 交付任务（按顺序，各自独立提交）

### 任务 A：diff.glb 生成器（`scripts/generate_diff_glb.py`）

对比 `out/35A1-JC1-baseline/model.json`（旧）vs `out/35A1-JC1-full-deliver/model.json`（新）：

1. **物理杆匹配**：按几何对齐（from/to 节点 3D 坐标距离 < 50mm 视为同杆；
   建议按杆件两端点距离的最小和配对，或直接用 component_id 前缀 stem 匹配 +
   坐标校验）。只用 front 面（face='f'）+ diaphragm 物理杆（排除 derived）。
2. **diff 三类**：
   - 新增（新模型有、旧模型无）→ **绿色** `[40, 200, 40, 255]`
   - 删除（旧模型有、新模型无）→ **红色** `[230, 60, 60, 255]`
   - 未变化 / 位置微调（<50mm）→ **灰色半透明** `[150, 150, 150, 120]`
3. **输出** `out/35A1-JC1-full-deliver/diff.glb` + `diff_report.json`：
   ```json
   {"added": [...component_id], "removed": [...], "unchanged_count": N,
    "summary": {"added": 12, "removed": 3, "unchanged": 590}}
   ```
4. 杆件实体化参考 `traceability/solve/tower_solver.py` 的 `export_tower_glb`
   （trimesh cylinder 即可，diff 场景不需要 L 截面细节；**只读参考，不改它**）。
   trimesh 已安装。
5. CLI：`python3 scripts/generate_diff_glb.py [--old PATH] [--new PATH] [--tol 50]`

### 任务 B：viewer 升级（`web/demo/35A1-JC1/compare.html` 重写）

在现有 three.js 骨架上扩展（保持 importmap/OrbitControls/GLTFLoader 用法）：

1. **数据加载**：页面 fetch 同目录 JSON（metrics_by_role.json 等）——
   先写一个 `scripts/sync_demo_assets.py`：把 out 下的
   `skeleton.glb / diff.glb / bar_map / metrics_* / evidence_report / review_queue`
   拷进 `web/demo/35A1-JC1/latest_deliver/`（该目录已存在），viewer 从那里读。
   （主线程管线每次 run 会覆盖 `tower_from_dxf.glb`，你的 sync 脚本独立于它。）
2. **模式切换**（顶部按钮组）：
   - `当前模型` / `新旧 diff` / `GT 官方`（沿用 gt_reference.glb）
3. **分层查看**（侧边面板，复选框/下拉）：
   - 按来源：`只看识别(绿)` `只看辅助重建(蓝)` `只看拼接(黄)` `只看派生(灰)`
   - 按分册：02 / 04 / 05 / 06 / 07
   - 按角色：主腿 / 斜材 / 水平材 / 横担 / 横隔
   - 实现方式：GLB 里每根杆的 mesh 有 extras（component_id/role/geometry_origin，
     见 bar_map.json 的 1504 条映射 + `trimesh` 场景的 mesh 节点名）。
     按 bar_map 的 component_id → mesh 过滤 visible。
     若 extras 不可达（GLTFLoader 会把 trimesh 的 mesh.metadata 放进
     `child.userData` 或 extras），退而求其次：重建时给不同来源分组到不同
     THREE.Group（生成 diff.glb / skeleton.glb 时无法分组，则在 viewer 里
     逐 mesh 按 name 匹配 bar_map）。
4. **统计面板**（左下角，读 metrics JSON 现成数据）：
   ```
   杆件 1499 | 节点 905 | Z [6500, 36600]
   识别 972 | 辅助 402 | 拼接 43 | 派生 82 | 复核 2
   A2-pure TP@500=56 | A2-full TP@500=211 | A1 P=91% R=31%
   缺失区间: z < 6500（底段无图源）
   ```
   （数字从 metrics_multi_caliber.json / metrics_by_origin.json 现算，
   不许硬编码——JSON 里有什么算什么）
5. **复核节点红球**：review_queue.json 的残留节点画红色 sphere（r=80mm）。
6. **图例**（右下角）：绿/蓝/黄/灰/红球 + diff 三色，可点击开关对应层。

### 任务 C：回归测试（`tests/test_diff_glb.py`）

- 小模型 fixture（3 根杆：1 不变 + 1 新增 + 1 删除）→ 断言
  diff_report.json 三类计数正确、GLB 生成成功
- sync_demo_assets.py 的单测（拷贝清单完整性）
- `python3 -m pytest tests/` 全绿（354 + 你的新增）

## 3. 验收标准（M4 视觉升级）

```text
1. 网页能看出版本差异（diff 模式：绿增/红删/灰未变，一眼可辨）
2. 分层过滤工作正常（勾选「只看拼接」后只剩黄色杆）
3. 统计面板数字与 metrics JSON 一致（非硬编码）
4. 放大后能看清 L 型角钢截面（skeleton.glb 已有，确认渲染无回退）
5. 复核红球可见
6. 全量测试通过
```

## 4. 提交纪律

```
Commit 1: feat(viewer): diff.glb 生成器 + 新旧差异三色导出（Phase 6.1）
Commit 2: feat(viewer): 分层查看 + 统计面板 + 来源图例（Phase 6.2/6.3）
Commit 3: test(viewer): diff 与 sync 资产回归测试
```

每个 commit message 附前后截图说明（文字描述变化即可）与验收点逐条勾选。
**不要 push**，主线程统一 push。

## 5. 冲突红线（禁止修改这些文件——主线程正在改）

```text
traceability/eval/metrics.py
traceability/eval/*.py（eval 目录全部）
scripts/evaluate_ground_truth.py
scripts/run_35A1_jc1_full.py
traceability/intake/tower_symmetry.py
traceability/solve/tower_geometry.py
traceability/harness/tower_validators.py
traceability/project/delivery.py
examples/external/guowang_35A1/layer_overlay.json
tests/test_multi_caliber_eval.py
tests/test_delivery_status_unification.py
out/35A1-JC1-baseline/（冻结基线，只读）
```

可以新建/修改：`scripts/generate_diff_glb.py`、`scripts/sync_demo_assets.py`、
`web/demo/35A1-JC1/**`、`tests/test_diff_glb.py`、`web/demo/35A1-JC1/latest_deliver/**`。
`traceability/solve/tower_solver.py` 只许读（参考 export_tower_glb 的实体化代码）。

## 6. 常用命令

```bash
cd /Users/zsn/Documents/zotore/engineering-trace
python3 -m pytest tests/ -q                    # 全量测试（354 passed 基线）
python3 scripts/generate_diff_glb.py           # 生成 diff.glb
python3 scripts/sync_demo_assets.py             # 同步 demo 资产
python3 -m http.server 8000                    # 预览 http://127.0.0.1:8000/demo/35A1-JC1/compare.html
```

## 7. 已知坑

- GLTFLoader 对 trimesh 导出的 extras/metadata 支持有限：trimesh 的
  `mesh.metadata` 会写进 GLB 的 node extras（名字可能是 `_mesh_row_index` 之类），
  `bar_map.json` 是权威映射（component_id ↔ mesh 顺序）。若 mesh name 匹配不上，
  用 trimesh 导出时给 mesh 起名 = component_id（参考 export_tower_glb 里
  mesh 的命名/分组方式，你的 diff 生成器自己控制命名即可）。
- 大 JSON fetch 需 http 服务（file:// 会 CORS 拦截），务必用 http.server。
- camera.up 是 Z 轴（塔竖直向上），保持现有相机设置。
- 塔很大（36.6m），球/杆尺寸用 mm 级（复核球 r=80mm）。
