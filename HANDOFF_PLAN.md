# 35A1-JC1 塔架重建交接计划（Phase 2 收尾 → Phase 3 → Phase 4）

> 交接日期：2026-08-31。前一 AI 已完成 Phase 1（已提交 2129bfa），Phase 2 完成 90%，
> 剩一个已诊断清楚的 bug 修复。本文档自包含，按步骤执行即可。

---

## 0. 当前状态（先看这里）

- **仓库**：`/Users/zsn/Documents/zotore/engineering-trace`，分支 `main`，远端 `https://github.com/ZSN12/zotero.git`
- **已提交**：`2129bfa`（S7 锥体重建：Theil-Sen 直线锥体 + 沿杆插值采样 + 生产横担层检测）
- **测试基线**：334 passed（`python3 -m pytest tests/`，29 秒）
- **当前基线指标**（无拼接，`/tmp/model_baseline.json` 已存基线模型）：

| 口径 | TP@50 | TP@100 | TP@200 | TP@500 | P@500 |
|---|---|---|---|---|---|
| A2-pure（纯 DXF，278 杆） | 0 | 0 | 8 | 60 | 21.6% |
| A2-full（physical，628 杆） | 78 | 102 | 138 | **208** | 33.1% |

- **悬空断裂节点**：17 个（目标 ≤4；另有 46 个横担悬臂端头属正常）
- **未提交改动**（`git status`）：
  - `traceability/solve/tower_geometry.py`（+251 行）：`stitch_collinear_bars()` 函数已写完、已测；
    `inspect_model_topology()` 增加了 T 形接头判定（度=1 节点落在其它杆身上不计悬空）
  - `tests/test_taper_half_width.py`（+141 行）：拼接与 T 形接头的 8 个单测，全过
  - `examples/external/guowang_35A1/layer_overlay.json`：新增 `collinear_stitch: false` 等键

---

## 1. Phase 2 收尾（唯一剩余 bug，已诊断清楚）

### 1.1 Bug 是什么

`stitch_collinear_bars()` 已在 `traceability/solve/tower_geometry.py` 写完并单测通过，
但**还没接进管线**。之前接过一次（挂在 `tower_symmetry.py` 第 386 行、
`inspect_model_topology` 之前），结果 TP@500 从 208 掉到 188，于是撤掉了（当前状态）。

**根因（已用实验证实）**：拼接挂在 `classify_members()` **之前**，此时 `face_bars`
里没有 `role` 字段，`stitch_collinear_bars` 内的 `role == "CROSS"` 跳过逻辑完全没生效
→ 40 根横担杆被错误拼接、几何被毁。离线实验（同样代码在最终模型上跑，role 齐全）
结果是 **TP@500 208→209、TP@200 138→142、Precision@500 33.1%→37.4%**，全部正向。

### 1.2 修复步骤（预计 20 分钟）

文件：`traceability/intake/tower_symmetry.py`

在 `expand_4_face_symmetry_model()` 里找到（约第 386-387 行）：

```python
    topology = inspect_model_topology(face_nodes, face_bars, half_width_fn=half_width_fn)
    roles = classify_members(face_nodes, face_bars)
```

在其**之后**插入（注意：必须在 classify_members 之后，让 role 已标注）：

```python
    # S4 贪心共线拼接（Phase 2）：把断裂碎片杆拼回整杆。
    # 关键教训（2026-08-31 实测）：
    #   1. 必须在 classify_members 之后挂——否则 face_bars 无 role，
    #      role=="CROSS" 跳过不生效，40 根横担被错拼，TP@500 208→188。
    #   2. 拼接端点用精确投影极值新建节点（stitch 返回的 new_nodes），
    #      严禁吸附到现存节点（吸附引入 ≤gap 偏移，实测 TP@500 209→188）。
    #   3. 参数 gap=300/ang=10°/maxLen=4500 是 12 组扫参最优；
    #      800/20°/10000 会过合并（TP@500 -3），不要用。
    if bool(spec.get("collinear_stitch", False)):
        from ..solve.tower_geometry import stitch_collinear_bars
        for _b in face_bars:
            if not _b.get("role"):
                _b["role"] = roles.get(str(_b.get("id")))
        face_bars, _stitch_nodes, _stitch_rep = stitch_collinear_bars(
            face_nodes, face_bars,
            gap_mm=float(spec.get("collinear_stitch_gap_mm", 300.0)),
            ang_deg=float(spec.get("collinear_stitch_ang_deg", 10.0)),
            min_merged_len_mm=float(spec.get("collinear_stitch_min_len_mm", 600.0)),
            max_merged_len_mm=float(spec.get("collinear_stitch_max_len_mm", 4500.0)),
        )
        if _stitch_nodes:
            face_nodes = dict(face_nodes)
            face_nodes.update(_stitch_nodes)
        # 拼接后杆件集合变了，重新分类 role（新 stitch_* 杆也需要 role）
        roles = classify_members(face_nodes, face_bars)
        _df = model.components.get("drawing_file")
        if _df is not None:
            _df.properties["collinear_stitch_report"] = dict(_stitch_rep)
```

然后启用 overlay（文件 `examples/external/guowang_35A1/layer_overlay.json`）：
把 `"collinear_stitch": false` 改成 `true`（其余 4 个参数键已存在，保持不动）。

### 1.3 验收标准

```bash
python3 -m pytest tests/          # 必须 334 passed
python3 scripts/run_35A1_jc1_full.py
```

期望输出（A2-full 行）：
- TP@500 = **209**（不小于 208 即可接受）
- TP@200 = **142**
- Precision@500 ≈ **37.4%**
- A2-pure TP@200 = 17（从 8 提升）
- 悬空断裂节点 ≤ 17（不应恶化；T 形接头判定已上线）
- `model.json` 里 `collinear_stitch_report` 显示 merged_groups ≈ 280-450

**对照参考**：如果 TP@500 掉到 188 → role 标注没生效；掉到 204 → 端点吸附了旧节点。

### 1.4 提交

```
git add traceability/solve/tower_geometry.py traceability/intake/tower_symmetry.py \
        tests/test_taper_half_width.py examples/external/guowang_35A1/layer_overlay.json
git commit -m "S4 贪心共线拼接生产化（Phase 2）：横担跳过 + 精确端点 + T 形接头判定"
```

---

## 2. Phase 3：悬空断裂节点清零（17 → ≤4）

### 2.1 诊断方法

```bash
python3 scripts/classify_dangling_nodes.py   # 已有分类器脚本
```

当前 17 个 genuine 悬空节点。类别参考（Phase 2.3 已建）：
`module_boundary_gap`（模块边界处杆件缺失导致断头）、`horizontal_endpoint_gap`
（水平材端头没接到腿）。T 形接头和横担悬臂端已排除。

### 2.2 修复策略（按性价比排序）

1. **已有工具**：`snap_dangling_endpoints_local()`（tower_geometry.py:1649，已在 overlay
   开启 `snap_dangling_endpoints: true, snap_dangling_max_gap_mm: 300`）。它已跑过，
   17 个是它救不了的（缺口 >300mm 或角色不匹配）。可以把 `snap_dangling_max_gap_mm`
   试探提到 500，看会不会误伤（TP 不能掉）。
2. **模块边界缺口**：查这 17 个节点的 z 坐标是否聚集在图纸模块边界（如 z≈12000、
   z≈24000，02/05/06/07 图分界）。若是，说明跨图杆件没接上——在 merge 阶段
   （`traceability/project/` 里查 spatial merge 逻辑）做端点距离 <500mm 的同向连接。
3. **兜底**：真接不上的（源图就缺线），从模型中删除孤立短杆（长度 <600mm 且度=1），
   或生成 `review_queue.json` 标记为人工复核项，不计入 FP。

### 2.3 验收

- `topology_genuine_dangling` ≤ 4
- 生成 `out/35A1-JC1-full-deliver/review_queue.json`（含每个残留悬空节点的
  坐标、所属面、断口距离、建议动作）
- A2 指标不回退（TP@500 ≥ 209）

---

## 3. Phase 4：双口径交付（最后一步）

### 3.1 交付报告

写 `out/35A1-JC1-full-deliver/final_report.md`，含：

1. **A2-pure**（对外汇报口径）：纯 DXF 识别能力，278→~204 杆（拼接后）
2. **A2-full**（内部归因口径）：含 GT 标高辅助（横隔 330 + 节间化 20）
3. 理论天花板说明：front 2D 投影上限 80.1%（858/1071）——y_member 87 根退化为点、
   depth_diag 126 根与腿投影重合，这是 2D 评测的固有上限，不是缺陷
4. S7 锥线拟合验收：DXF 拟合线 vs GT 腿节点线（2762 - 0.0700z）残差中位 24mm ≤ 30mm
5. 各 Phase 提升表：TP@50 40→78（×1.95）、TP@200 102→142、TP@500 188→209、
   Precision@500 33.1%→37.4%

### 3.2 GLB 分类分色

`skeleton.glb` 已由管线产出（1544 KB）。确认/增强着色：
- recognized（直接识别）→ 绿色
- reconstructed（横隔/节间化）→ 蓝色
- collinear_stitch（拼接杆）→ 黄色
- derived（corner/展示）→ 灰色
- review_queue 悬空节点 → 红色球标记

GLB 导出代码在 `scripts/run_35A1_jc1_full.py` 或 delivery 模块里（搜 "glb"）。

### 3.3 最终验收 + 推送

```bash
python3 -m pytest tests/          # 334+ passed
python3 scripts/run_35A1_jc1_full.py
git push origin main               # 远端 https://github.com/ZSN12/zotero.git
```

---

## 4. 关键背景（防止踩已踩过的坑）

### 4.1 评测口径

- `eval_a2_dual_caliber()` 在 `traceability/eval/metrics.py`：GT 投影杆 1071（front 2D，Hungarian 1:1）
- **GT 验收基准线是「GT 腿节点线」hw = 2762 - 0.0700z**（GT 四角节点 192 个 Theil-Sen
  拟合，残差中位 0mm）。注意 **不是** `gt_profile.py` 的 2649-0.0687z——那是侧面中心线
  参考，与节点位置差 ~104mm，用错基准会误判拟合偏差 +130mm。

### 4.2 已否决的方案（不要重试）

| 方案 | 结果 | 原因 |
|---|---|---|
| union-find 全连通拼接 | A2-pure 56→26 | 17m 主腿并成一杆，GT 最长 7077 |
| 拼接参数 800/20°/10000 | TP@500 -3 | 过合并 |
| 拼接端点吸附现存节点 | TP@500 209→188 | 端点偏移 ≤gap |
| 拼接挂在 classify 前 | TP@500 208→188 | 横担没跳过 |
| 端点采样拟合半宽 | z 9250 箱 p85=1054 vs 腿线 2014 | 节间化腿端点只在节间边界 |
| 偏好 ≥2500 通长腿采样 | 整段塔身无样本 | 选中 6 根幽灵长腿（21~27m） |

### 4.3 运行入口与产物

- 全管线：`python3 scripts/run_35A1_jc1_full.py`（约 2 分钟）
- 产物目录：`out/35A1-JC1-full-deliver/`（model.json / skeleton.glb / full_run_report.json / compare.html）
- 预览：`http://127.0.0.1:8000/demo/35A1-JC1/compare.html`（需 `python3 -m http.server 8000`）
- 离线拼接对照实验：`python3 scripts/experiment_collinear_stitch.py out/35A1-JC1-full-deliver/model.json examples/gt/35A1-JC1_ground_truth.json --gap 300 --ang 10`
- 基线模型快照：`/tmp/model_baseline.json`（无拼接版，用于 A/B 对照）

### 4.4 代码纪律

- overlay 显式 opt-in：任何新功能加 `"键": true` 到 `layer_overlay.json`，代码里
  `spec.get("键", False)` 默认关闭。**严禁**默认开启改变行为。
- GT 隔离：生产路径严禁读 GT 数值（`gt_profile.py` 仅 debug/eval 可用）。
  GT 标高注入仅限 `use_gt_half_width` 显式开启的对照实验。
- 每次改完跑 `python3 -m pytest tests/`（334 个，29 秒）+ 全管线。
