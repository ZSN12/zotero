# P3 收口报告：35A2-JC1 全塔接入，dual-recon R 33.8% → 78.5%（TP 134→311）

日期：2026-09-10 ｜ 承接 `690d439`（P4 件号扩绑）

## 结果（tol=500mm 主口径，endpoint_sum_cost）

| 口径 | 前（仅 02 册） | 后（02-07 册） |
|---|---|---|
| A2-dual-view-reconstructed | TP 134 / R 33.8% | **TP 311 / R 78.5%（P 17.5%）** |
| A2-dual-view-pure | TP 206 / R 52.0% | TP 206 / R 52.0%（不变） |
| A2-front-pure | TP 105 / R 26.5% | TP 105 / R 26.5%（不变） |

红线回归（本 session 只改 overlay/config，代码改动见下）：

| 红线 | 要求 | 实测 | 判定 |
|---|---|---|---|
| JC1 A2-dual-view-pure | ≥304 TP | TP 304 | ✓ 零回退 |
| JC1 A2-dual-view-reconstructed | R ≥99.5% | R 99.6%（TP 1067） | ✓ 零回退 |
| ZC1 A2-dual-view-reconstructed | R ≥95.1%（不可回退） | R 95.1%（TP 271） | ✓ 持平 |

pytest 871 passed / 0 failed；self_test 3/3 PASS（单测/冒烟/证据层 IR）。

## 接入过程（五册，R 逐级）

| 册 | z 域（mm） | 视图 | R 增量 | 关键定标 |
|---|---|---|---|---|
| 02 | [34000, 40280] | front+side | 33.8% 基线 | 塔顶双视图（前 session） |
| 04 | [27000, 34000] | front+side | →50.5% | merge_stems_extra 破门；DIM 链 2100/1700/1300/1000 |
| 05 | [20000, 27000] | front | →74.0% | DIM 链 2400/1300/1200/2100 做 z 域常数 |
| 06 | [15000, 20000] | front | →76.3% | +16500 层位（DIM 链只有 2500+2500，GT 主材跨型端点补层） |
| 07 | [7500, 15000] | front | →78.5% | 含腿段；z_anchor 落 7500（region 含 GT 外腿段） |

分段召回（GT zmax 归属）：02 段 89.8%（137 GT）、04 段 40.8%（120）、
05 段 80.0%（95）、06 段 73.7%（38）、07 段 100%（6）。

## miss 总账（85 根，官方口径 Hungarian 重放复核 TP=311 一致）

| 族 | 根数 | 归因 |
|---|---|---|
| 横担（\|x/y\|>1200） | 67 | 33k 横担层 34（03 册详图未接入）+ 27k 层 30（挂 05 册图外横担吊杆/平面斜杆，一对一挤兑）+ 02 册顶 3 |
| 04 段斜杆 | 12 | PM_0201-0208（33000 层交叉斜杆，cost≈1000）+ PM_0227-0234（中心斜杆 741） |
| 同层平面 X | 5 | PM_0127/0179/0180/0335/0336（两视图与边横杆共线） |
| 02 塔顶 | 1 | PM_0020（653） |

其中 27k 接缝层的 24 根（PM_0259-0282）已定性：GT 在 27000 下挂三族杆
（主材 24900 + 吊杆 24800 + 平面斜杆 24500，各×12），模型只有一根
27000→24900 杆（cost 6/105/406 三族共享），Hungarian 一对一只能吃一族；
24500/24800 层 05 册图面未独立画线（DIM 链只标到 24900）。

## 本轮代码改动（2 文件，均为 fail-closed 修复）

1. **tower_dxf.py：多 region 版面文字贴挂收紧**。图纸声明 view regions
   时，落在全部 region 之外的文字（材料表/详图标注/图签）不再参与贴挂
   （此前 view=None 回退全图配对，35A2-02 材料表 169 个数字全在
   TEXT_SNAP=400 危险带，件号 146 贴到 315 单位外塔身杆，制造
   r_project_bom_master 假冲突）。无 regions 的单视图图纸保留全图兜底。
2. **centerline_extract.py：leg_synth_min_span_mm 可配置**（默认 800
   不变）。02 册塔顶细段跨型 <800mm 需显式放宽；跨册边界杆（如
   PM_0354 16500→14500 跨 06/07 边界，z14500 在 06 region 外 287u）
   被链覆盖 15u 容差正确拒绝——这是 fail-closed 保护，不放宽到图外。

overlay 定标（`examples/external/guowang_35A2_jc1/layer_overlay.json`）：
02-07 册 view_regions + dimension_beat_anchor（region_span_linear 模式）
+ centerline_extract（beam_marker_levels_mm = DIM 链累计值 = GT 主材跨型
端点差，z-only 注入合规；x/y 无注入）+ bar_layers_by_stem +
cross_file_views.merge_stems_extra（04-07 自持视图显式入 merge）。

## 已知缺口清单（按性价比排序，均未排期）

1. **03 册横担详图**（34 根）：横担层 33000-34000 有独立详图册，定标
   体系（详图 scale/region）与塔身册不同，接入成本高。
2. **平面 X 斜杆通道**（5+24 根）：同层交叉杆在两视图与边横杆共线，
   需新证据通道（如平面图视图），现行 front/side 投影结构性不可分。
3. **27k 接缝层双族吊杆**（24 根内含）：05 册图面只画主材族，
   24500/24800 层无独立画线。
4. **跨册边界杆**（PM_0354 等 6 根）：跨 06/07 边界主材两册各画一半，
   leg_synth 链覆盖 15u 容差拒绝图外端点（fail-closed，不硬造）。
5. **04 段中心斜杆**（PM_0227-0234，8 根）：cost 741 近失，图面画线
   截断（1703 vs 1900）。
6. BOM master conflicts 11（105-113 件号 5>4）：05 册段内斜杆挂同件号
   未 split，审计未排期。

## L1 硬编码暴露点清单（本轮核查，三处均非阻断）

1. `traceability/solve/multiview_hypothesis.py:158`：函数默认参数
   `sheet="35A1-JC1-06"`——调用方 tower_symmetry.py:1545 恒显式传
   sheet，且三塔 overlay `multiview_hypothesis_sheets` 均为空列表，
   该默认值当前无生效路径。建议下轮清理为必填参数。
2. `traceability/solve/canonical_tower.py:43-53`：模块级
   DEFAULT_MOD/DEFAULT_NODE 兜底路径硬编码 35A1-JC1 官方包结构——
   交付管线 delivery.py 显式传 overlay canonical_tower 路径，不走
   默认值；仅交互式调试入口可触达。已登记 TECH_DEBT_REGISTER L1 项。
3. `traceability/intake/tower_spec.py:648`：docstring 示例含
   "35A1-JC1-06"——纯文档，无行为。

## 复现命令

```bash
WS=examples/external/guowang_35A2_jc1
python3 -m traceability.cli deliver-project $WS/dxf \
  --layer-map $WS/layer_overlay.json --bom $WS/master_bom.csv \
  --out-dir out/35A2-JC1-p3-probe
python3 scripts/eval_a2_profiles.py examples/gt/35A2-JC1_ground_truth.json \
  out/35A2-JC1-p3-probe/model.json --overlay $WS/layer_overlay.json \
  --json-out out/35A2-JC1-p3-probe/a2_eval.json
```

红线复验：`out/35A1-JC1-p3-redline/`、`out/35A2-ZC1-p3-redline/`
（run_manifest 记录输入 sha 与上轮 35A1-JC1-full-deliver 一致）。
