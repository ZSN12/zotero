# engineering-trace 项目交接文档（给 GLM 续做）

> 用途：把当前进度、已验证结论、未提交改动、下一步行动一次性交代清楚，便于另一名 AI 直接接手。
> 仓库根：`/Users/zsn/Documents/zotore/engineering-trace`
> 最后更新：2026-08-31 09:00（张少楠交接给 GLM）

---

## 1. 项目目标与背景

**做什么**：把电力铁塔（35kV/110kV 国网典型设计）的 **DXF 工程图纸**自动转换成**可追溯的工程数据**（杆件 bars、节点 nodes、几何 geometry、塔身半宽 caliber、BOM 物料）。系统代号 engineering-trace，强调“每一步产出都能追溯到源图纸图元”（evidence chain）。

**对标要求**：最终需满足项目官网 `concentriccirclesmrtt.github.io` 定义的评测口径，核心指标是 **A2 召回率（recall）——在基准塔 `35A1-JC1` 上，模型识别出的杆件与人工真值 GT 的匹配度**。

**基准集 35A1-JC1**：
- 50 张 DXF 图（02/04/05/06/07 五张是立面主图，贡献绝大多数杆件；其余 45 张是详图/模块页，按管线设计只进 M1 index、不进 M3，非缺陷）。
- GT 共 **1071 根杆**（这是“全塔 4 个面投影到正面 (x,z)”后的数量；GT 含 `depth_diag` 252 根等只有完整 3D 才有、正面投影才可见的杆）。
- 容差固定 `DEFAULT_TOLS` 的 500mm（TP@500 是主指标，不靠调容差刷分）。

---

## 2. 进度总览（哪些已提交 / 哪些未提交）

### 已提交（git log 可见，pytest 全量通过）
- `99a290e` 任务4+5：baseline_report 双口径现算 + A2-effective 评测
- `cc4cae7` 任务1关闭（07段主腿已切成~1m段，切点距 GT 标高 42–200mm）+ 任务3补 z 12–14km 盲区实证
- `0ba4db8` / `93fd16e` P0 口径诚实化：纯 DXF 主口径 + GT 标高辅助增量单列 + front 上限 80.1%
- `c6e9771` P1 共线拼接实验：现行口径 TP+9 / P+4.1点；对称口径 recognition 5.2%→15.9%
- `efd0a32` **Phase 2.3 并行工作**（与召回优化并行推进的另一条线）：受控局部端点吸附 `snap_dangling_endpoints_local` + Phase 0 可复现基线 + A1 件号污染过滤 + 悬空节点分类器

### 未提交（工作树里 2 个文件被改，属于本次“锥度重建”）
- `M traceability/solve/tower_geometry.py`
- `M traceability/intake/tower_symmetry.py`

> ⚠️ 注意：`tower_symmetry.py` 同时承载 Phase 2.3 的未提交改动，提交锥度改动时要一并处理，避免只 commit 一半导致文件状态撕裂。

---

## 3. 召回率优化完整记录（6 轮，已定量验证）

起点基线（commit `93fd16e`，ezdxf 模式，无 MLLM）：
| 口径 | 模型杆数 | TP@500 | Precision | Recall |
|---|---|---|---|---|
| **A2-pure（纯 DXF 直接识别，对外主口径）** | 280 | 56 | 20.0% | **5.2%** |
| A2-full（physical，含 GT 标高辅助横隔） | 632 | 188 | 29.8% | 17.5% |

**Round 0 基线**：口径上限 80.1%（858/1071）；FN 883 构成 diagonal 253 / depth_diag 248 / leg 231 / y_member 87 / horiz_x 64。

**Round 1 拼接首试失败**：并查集全连通把整条主腿 51 个节间并成 17256mm 超长杆（GT 最长仅 7077mm），TP 反降（188→155）。→ 改贪心成对合并。

**Round 2 根因修正（重要）**：诊断已匹配的 188 对，发现**已匹配杆端点极准**（长度比中位 1.06、Z 偏差中位 0mm、X 偏差 25mm）。未匹配 GT 中 56% 的最近模型杆误差 >1000mm——即**根本没有对应杆**，不是碎片。推论：任何“移动端点”的操作（吸附/拼接）都会破坏已有精确定位。

**Round 3 口径不对称（4 倍）**：模型 front 只投 `face=f` 单面，GT 投影是全塔 4 面，两侧不对称 4 倍。4 面全投影（对称口径）：physical TP 188→281（R 17.6%→26.2%）。该修正尚未改生产口径（会重算全部历史指标），仅实验脚本呈现。

**Round 4 拼接网格搜索（核心成果）**：贪心成对 + `max_merged_len`/`max_segments` 双约束，最优参数 **gap=800mm / ang=20° / maxLen=10000mm / seg≤10**（注意：有效 gap 是 800mm 而非原计划的 80mm，DXF 分段断裂间隙远大于预期）。
| 口径 | 不拼接 | 贪心拼接 | 变化 |
|---|---|---|---|
| 单面 physical | TP 188, P 29.7% | TP 197, P 33.8% | **TP+9，P+4.1点** |
| 单面 recognition | TP 56, P 20.0% | TP 66, P 26.1% | TP+10，P+6.1点 |
| 4 面 physical | TP 281, R 26.2% | TP 305, R 28.5% | TP+24 |
| 4 面 recognition | TP 56, R 5.2% | TP 170, R 15.9% | **TP+114（3倍）** |

**Round 5 转 3D 评测：结论为否**：3D 基线 TP@500=242（R 22.6%），低于 front 2D 四面口径 281/305。原因：模型 4 面由 `expand_4_face_symmetry` 镜像生成，3D 配准精度不足（各面 302 根仅匹配 32 根，精度 10.6%）。结论：先补 3D 配准精度再谈切换。

**Round 6 覆盖率与定位精度诊断**：50 张 DXF 仅 5 张贡献杆件（合计 1208 根，与 GT 1071 相当，**数量不是问题**）。匹配两极分化：已匹配极准、未匹配 56% 完全错位；各 z 段召回普遍 8%~35% 无异常 → **系统性几何/拓扑重建问题，非单图配准失误，也非参数调优可解**。推论：拼接/吸附参数收益已饱和（Round 4 已收敛），下一步回几何重建阶段。

---

## 4. 关键技术发现（已验证，非推测）

1. **碎片化只对一半**：已匹配杆端点极准，问题在“模型根本没生成对应杆”，不是端点不准。→ 吸附/拼接类小修小补收益已封顶。
2. **front 2D 硬上限 80.1%**：`y_member` 87 根正面投影退化为点（物理不可匹配）；`depth_diag` 252 根与 leg 投影 0mm 重合（1:1 最多召一半，−126）。突破靠 3D 评测或 4 面对称口径。
3. **口径不对称 4 倍**：模型 1 面 vs GT 4 面。
4. **横隔过量生成**：模型造 330 根 vs GT 208 根（FP 主要来源，Precision 的下一个抓手）。
5. **锥度（taper）错误是根本瓶颈**👇（当前主攻方向）。

---

## 5. 锥度重建（当前进行中，未提交）—— 这是最大的杠杆

### 5.1 发现
GT 塔身半宽 `hw(z)` 是**严格直线锥体**：拟合 `hw(z) = 2762 − 70.24·z`（残差 max 31mm）。
模型当前在 `z ∈ 7–12m` 段用**常数半宽 ~1827mm**（平台段），导致该段所有斜材/横材的横向位置整体错位，进而无法与 GT 匹配。

### 5.2 探针验证（离线，不改生产代码）
对 GT 施加真实锥度缩放 + 4 面对称口径：
- physical TP **188 → 335（+78%）**，超过线性预期。
- 这是“根本性大幅提升”方向，符合用户“不要拆西墙补东墙、要大动作”的要求。

### 5.3 已落地的代码（未提交，工作树）
`traceability/solve/tower_geometry.py`：
- `_theil_sen_fit(zs, hs)`：Theil-Sen 稳健回归（斜率取中位数，抗横担/内部竖杆污染）。
- `_fit_taper_profile(z_pts, hw_pts, *, inlier_tol_mm=150)`：分箱取 95 分位 → 剔除“无主腿箱”（样本值低于中位 50%）→ Theil-Sen 回归 → 物理约束 k≤0 → **内点比例判据**（inlier_ratio ≥ 0.7 才接受，否则回退旧 monotone 路径，返回 None）。
- `fit_tower_half_width_from_face(..., method="monotone"|"taper", taper_max_residual_mm=150.0)`：method="taper" 时先试锥体回归，失败落回旧法，不影响既有行为。

`traceability/intake/tower_symmetry.py`：在 `half_width_fn is None` 分支接线——
```python
taper = bool(spec.get("half_width_taper", False))
fitted = fit_tower_half_width_from_face(..., method="taper" if taper else "monotone",
                                        taper_max_residual_mm=float(spec.get("half_width_taper_max_residual_mm", 150.0)))
```
即：仅当 overlay 显式 `half_width_taper: true` 时才启用锥体重建（与 snap 同纪律，默认关闭）。

`tests/test_tower_geometry.py::test_fit_half_width_monotonic_taper` 已存在且通过（污染下完美恢复真值 2275/1718/1092；k>0 正确回退；明显变坡正确回退）。

### 5.4 验证状态
- ✅ 单元测试通过（锥体拟合逻辑正确）。
- ❌ **尚未跑全量 pytest 回归**（上轮被网络 499/429 中断）。
- ❌ **尚未跑 JC1 全管线（half_width_taper=true）确认真实增量**，对照探针上限：单面 physical 约 207、4 面 physical 约 335。

---

## 6. 给 GLM 的下一步行动清单（按价值排序）

**P0 — 先验证锥度改动不破坏现有测试**
```bash
cd /Users/zsn/Documents/zotore/engineering-trace
python -m pytest tests/test_tower_geometry.py tests/test_tower_symmetry.py -q
```
全绿后再走 P1。（之前网络中断导致这步没跑完，优先补上。）

**P1 — 跑 JC1 全管线验证锥度真实增益**
- 在 guowang_35A1 生产 overlay 里加 `"half_width_taper": true`（必要时 `"half_width_taper_max_residual_mm": 150.0`），重跑 50 张 DXF。
- 对照探针上限：单面 physical ≈207、4 面 physical ≈335。若接近则锥度方向成立。
- 同时看单面口径（生产默认）下增量是否 ≥ Round 4 拼接的 +9 TP（否则锥度需配合 4 面口径才划算）。

**P2 — 若 P1 验证成立，协商提交**
- `tower_symmetry.py` 同时有 Phase 2.3 未提交改动，**不要只 commit 锥度那半**，应与 Phase 2.3 改动一起审阅后统一提交。
- 提交前确保全量 `pytest` 通过（历史基线 310 passed）。

**P3 — 决策 4 面对称口径**
- 对称性论证成立（GT 含 depth_diag 252 根，必为完整 3D 模型投影），但切换会重算全部历史指标，影响对外汇报数字连续性，**需人拍板**，GLM 不要擅自改。

**P4 — 横隔校准（Precision 抓手）**
- 模型 330 根 vs GT 208 根，横隔过量生成是 FP 主因。可与锥度配合做 Precision 提升。

**纪律（务必遵守）**
- 每轮必须跑真实 JC1 全量（50 张 / GT 1071），禁用子集或合成数据自证。
- 所有增益必须来自几何/拓扑改进，**容差恒定 DEFAULT_TOLS，不靠调容差刷绿**。
- 用户要求“大幅调整、不要拆西墙补东墙”——优先几何重建（锥度、段间 z 配准、端点归属），而非参数微调。
- 不动他人正在改的文件；锥度与 Phase 2.3 都碰 `tower_symmetry.py`，改动要协同。

---

## 7. 关键文件速查

| 文件 | 作用 |
|---|---|
| `traceability/solve/tower_geometry.py` | 几何求解；锥体回归在此（未提交） |
| `traceability/intake/tower_symmetry.py` | 4 面对称展开 + 半宽拟合接线（未提交） |
| `traceability/eval/metrics.py` | 双口径评测、front 上限、角色判据 |
| `scripts/evaluate_ground_truth.py` | 主评测入口（A2-pure 主报，A2-full 归因） |
| `scripts/experiment_collinear_stitch.py` | Round 4 拼接实验 |
| `scripts/experiment_caliber_matrix.py` | Round 3 口径矩阵 |
| `scripts/experiment_stitch_sweep.py` | Round 4 参数网格 |
| `OPTIMIZATION_TARGETS.md` | 6 轮迭代完整记录 + 目标设定 |
| `UNIMPLEMENTED_PLAN.md` | 原任务清单（任务1/4/5 已关闭） |
| `out/35A1-JC1-full-deliver/model_nosnap.json` | 当前模型备份（无吸附） |

---

## 8. 一句话给 GLM

“A2 纯 DXF 召回只有 5.2%，根因不是碎片而是**塔身锥度被建错成平台段** + **模型只投单面而 GT 是 4 面**。锥度重建（Theil-Sen 稳健回归，未提交）探针显示 +78%（188→335），请先跑通全量 pytest 再跑 JC1 验证它，验证成立后协同 Phase 2.3 一起提交，不要擅自切 4 面口径。”
