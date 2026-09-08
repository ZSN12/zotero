# 任务书：3D 外观升级 Phase 6.4–6.6（截面规格可视化 + 节点板样例 + 材质升级）

> 你将接手一个独立子任务（前序任务：TASK_VIEWER_3D.md，Phase 6.1–6.3 已由你完成：
> diff.glb + 分层查看 + 统计面板）。本任务与主线程（评测口径/几何管线）**零文件冲突**，
> 可完全并行。禁止改动文末「冲突红线」列出的文件。

---

## 0. 项目背景（自包含，无需其他上下文）

- **仓库**：`/Users/zsn/Documents/zotore/engineering-trace`（Python 3.10 + 原生 JS/three.js，无 node 构建）
- **项目**：输电铁塔 DXF 图纸 → 3D 模型重建（35A1-JC1 塔，杆件约 1500 根 / 高 36.6m）
- **你的前序成果**（可继续复用/扩展）：
  - `web/demo/35A1-JC1/compare.html`（535 行：diff 视图 + 分层过滤 + 统计面板）
  - `scripts/generate_diff_glb.py`、`scripts/sync_demo_assets.py`
  - `tests/test_diff_glb.py`
- **本地预览**：`python3 -m http.server 8000` → `http://127.0.0.1:8000/demo/35A1-JC1/compare.html`
- **测试**：`python3 -m pytest tests/`（当前 363 passed，不得回退）

## 1. 三个子目标（对应总计划的 Phase 6.4–6.6）

### 6.4 截面规格可视化（点击杆件看角钢截面）

**现状**：`skeleton.glb` 里每根杆已是真实 L 型角钢实体（`_angle_steel_mesh`：
按 section 解析肢宽/肢厚拉伸），但 viewer 里用户看不到「这根是什么规格」。

**数据**：`out/35A1-JC1-full-deliver/skeleton.bar_map.json`（每杆
`bar_id/component_id/role/geometry_origin`）+ 模型 `model.json` 每杆
`properties.section`（如 `"L40X3"` = 40mm 肢 × 3mm 厚）。

**要做的**：
1. 同步脚本扩展：把 section 并进 bar_map（sync_demo_assets.py 读 model.json，
   给每条 bar_map 记录加 `section` 字段；解析失败记 null）。
2. viewer 点击交互：射线拾取杆件 → 侧边信息卡显示：
   - 件号 bar_id / 角色 role / 来源 geometry_origin（溯源链已有，补 section）
   - **截面卡片**：规格名 + 肢宽×肢厚（如 L40X3 → 40×3mm）+ 小示意 SVG（L 形两肢）
   - 3D 高亮：选中杆件 emissive 高亮 + 其他杆件半透明
3. 截面分布统计：统计面板加一行「截面规格分布」（L40X3 ×157 / L56X4 ×44 / …），
   未关联 section 的杆计为「未关联」单列。

**验收**：点击任意有 section 的杆，信息卡出现且截面数据正确；
统计面板出现规格分布；无 console 报错。

### 6.5 节点板 + 螺栓样例（Detail 模式）

**现状**：全塔模型 0 个节点板组件（节点板只存在于**详图页**：
`web/demo/35A1-JC1/latest_deliver/sheets/35A1-JC1-03.json` 里有
`gusset_D1`：`polygon_local` 10 点轮廓 + `bolt_holes` 32 孔 + `transform`
{origin_local, scale_to_real}，但 `polygon_global` 未解算——即详图页的
节点板**没有 3D 世界坐标**，只有图内局部坐标）。

**要做的**（不要做全局 3D 定位——那是主线程 Phase 7 的几何问题）：
1. **样例展示**：新增 viewer 的「节点详图」模式（一个按钮切换）：
   - 新脚本 `scripts/build_detail_sample.py`（新文件）：读 03 页 JSON，
     用 polygon_local + bolt_holes 生成**样例节点板 GLB**：
     - 节点板：多边形拉伸成 8mm 厚实体（厚度假定 8mm，数据里 thickness 为 null）
     - 螺栓孔：32 个圆孔（CSG 或贴图方式皆可；性能允许时真挖孔，否则半透明圆片标记）
     - 旁边摆上该 detail 关联的杆件截面（从 03 页 bolt_group/bar 数据取，摆成连接示意）
   - 输出 `web/demo/35A1-JC1/detail_sample.glb` + `detail_sample.bar_map.json`
2. viewer 集成：Detail 模式加载该 GLB，相机拉近，可旋转查看；
   信息卡显示 detail_id / 孔数 / 孔径 / 板厚（来自 bolt_group 的 diameter_mm）。
3. 至少 1 个节点板样例可看（D1 就够）；如果 03 页还有其他 detail 也顺手支持。

**验收**：viewer 里能切到「节点详图」模式，看到带孔节点板实体 + 连接杆件示意，
信息卡数据与 JSON 一致。

### 6.6 外观整体升级（材质 / 灯光 / 抗锯齿）

**现状**：three.js 0.160 基础 MeshStandardMaterial，单方向光，无抗锯齿，
背景纯色。

**要做的**（全在 web/demo/** 里，只动 viewer 侧）：
1. **材质**：杆件用金属度/粗糙度区分来源（recognized=亮银、reconstructed=蓝、
   派生=灰暗），节点板/详图模式用镀锌钢质感（metalness 0.85 / roughness 0.4）
2. **灯光**：半球光 + 主方向光（带阴影，shadow map 2048）+ 微弱补光
3. **抗锯齿**：renderer antialias: true + setPixelRatio(min(devicePixelRatio, 2))
4. **背景**：淡渐变天空（CSS 或 scene.background 渐变纹理），地面参考网格（可开关）
5. **性能红线**：全塔 ~1500 mesh 帧率 ≥30fps（M1 MacBook）；如掉帧，
   用 InstancedMesh 或合并同材质 mesh，并在任务报告里写明用的方案

**验收**：肉眼可见的质感提升（截图对比前后），帧率达标，无 console 报错。

## 2. 数据资产清单（全部已存在，直接读）

| 文件 | 用途 |
|---|---|
| `out/35A1-JC1-full-deliver/model.json` | 杆件 section / role / provenance |
| `out/35A1-JC1-full-deliver/skeleton.bar_map.json` | 杆件 ↔ GLB mesh 映射 |
| `out/35A1-JC1-full-deliver/skeleton.glb` | L 角钢实体骨架（1261KB） |
| `web/demo/35A1-JC1/latest_deliver/sheets/35A1-JC1-03.json` | 节点板 D1（polygon_local/bolt_holes/transform）+ 16 个 bolt_group |
| `out/35A1-JC1-full-deliver/metrics_by_origin.json` 等 | 统计面板数据（你已接好） |

**截面规格解析**（写在 viewer/脚本侧，参考 `traceability/solve/tower_solver.py`
的 `_parse_section`——只许读）：`L40X3` → 肢 40mm 厚 3mm；`Q345L100X7` →
材质 Q345 + 肢 100mm 厚 7mm（去掉 Q345 前缀再解析）。

## 3. 冲突红线（禁止改动——主线程所有物）

```text
traceability/（全部：eval/ intake/ solve/ harness/ project/ io/ connection/ debug/）
scripts/evaluate_ground_truth.py
scripts/run_35A1_jc1_full.py
examples/external/guowang_35A1/layer_overlay.json
examples/gt/（只读）
tests/（除你自己新建的 test_detail_sample.py / test_bar_map_section.py）
out/35A1-JC1-baseline/（冻结基线，只读）
```

你可以新建/修改：`web/demo/35A1-JC1/**`、`scripts/build_detail_sample.py`、
`scripts/sync_demo_assets.py`（扩展）、`scripts/generate_diff_glb.py`（你自己的）、
`tests/test_detail_sample.py`、`tests/test_bar_map_section.py`（新）。

> 注意：`tests/test_diff_glb.py`（你已建）可继续改；主线程会保证
> `python3 -m pytest tests/` 全绿合入前不碰你的文件。

## 4. 测试要求（合入门槛）

- 新建 `tests/test_bar_map_section.py`：sync 后的 bar_map 每条记录有
  `section` 字段（可为 null）；有 section 的记录格式合法（`L\d+X\d+` 或 `Q\d+L\d+X\d+`）。
- 新建 `tests/test_detail_sample.py`：`build_detail_sample.py` 输出的
  detail_sample.glb 存在且 ≥1 个 mesh；bar_map 与 mesh 数一致。
- 全量 `python3 -m pytest tests/` 通过（363 passed 基线，主线程可能已推进到更多，
  以合入时为准）。

## 5. 常用命令

```bash
cd /Users/zsn/Documents/zotore/engineering-trace
python3 -m pytest tests/ -q                    # 全量测试
python3 scripts/build_detail_sample.py         # 生成节点板样例 GLB
python3 scripts/sync_demo_assets.py            # 同步 demo 资产（含 section）
python3 -m http.server 8000                    # http://127.0.0.1:8000/demo/35A1-JC1/compare.html
```

## 6. 已知坑

- `polygon_global` 为空——**别等主线程解算**，你的样例用 `polygon_local` +
  `transform.origin_local`/`scale_to_real` 在样例场景里自建坐标系即可
  （样例是「展示节点板长什么样」，不是「它在塔上的 3D 位置」）。
- bolt_holes 坐标也是 local（同 03 页图内坐标），与 polygon_local 同系。
- trimesh GLB 导出的 metadata 在 three.js 侧读不到——权威映射走
  `*.bar_map.json`（你前序任务已趟过）。
- 03 页的 detail 图坐标单位是图纸单位，`transform.scale_to_real=1.0`
  表示无需缩放；`origin_local` 是图内平移基准——样例场景原点放板中心即可。
- 大 JSON fetch 必须 http 服务（file:// 会被 CORS 拦）。

## 7. 完成标准（任务报告里逐条对照）

1. [ ] 6.4：点击杆件出截面信息卡（规格/肢宽/肢厚/SVG 示意），统计面板有规格分布
2. [ ] 6.5：Detail 模式可见节点板 D1 实体（含孔）+ 连接示意，信息卡数据与 JSON 一致
3. [ ] 6.6：材质/灯光/抗锯齿升级，前后截图对比，帧率 ≥30fps
4. [ ] 两个新测试文件通过，全量测试不回退
5. [ ] 只动了允许清单内的文件（git status 核对）
