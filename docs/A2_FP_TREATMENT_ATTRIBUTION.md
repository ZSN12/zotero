# A2 full 池 FP 治理归因（负结论 + 两个零损失正结果）

日期：2026-09-05。对象：35A1-JC1，A2 front full 池 @tol=500mm。
治理前基线：TP=920 / FP=2707 / P=25.4% / R=85.9%；dual 并集 TP=1069
（R=99.8% 红线）。治理后：TP=919 / FP=2666 / P=25.6% / R=85.8%，
dual 红线 1069 保持，pytest 736/5 无回归。

结论先行：

1. **最大 FP 簇（panel_template，1257/2707）不可诚实剪除。** 七条候选
   规则全部无法做到零 TP 损失；根因是 GT 本身包含「图纸未画但真实存在」
   的深 K 面板，其证据状态与纯 FP 面板完全相同。
2. **两个零损失正结果已落地**：exact_overlap_dedup（严格 3D 重复杆，
   删 14）+ headx 证据覆盖门（被既有证据杆完整覆盖的塔头模板杆不生成，
   删 41）。合计 FP -55（P +0.2pp），TP 与 dual 红线不变。
3. **对外口径不受影响。** 对外主口径 A2-dual-view-pure：P=63.5%
   （TP=304 / FP=175），生成器 FP 全部落在 parametric/reconstructed
   caliber，pure 池零贡献。full 池 P 是内部 recall-first 过生成的设计
   结果，不是对外承诺。

## 1. FP 结构（caliber × geometry_origin）

复跑：`python3 scripts/diag_fp_pairs.py`

| caliber | geometry_origin | FP | TP | 说明 |
|---|---|---|---|---|
| parametric | panel_template_completion | 1257 | 379 | kfan/xpanel 模板补全 |
| level_assisted | terminal_pair_gen | 379 | 157 | 层位终末端对生成 |
| level_assisted | diaphragm_reconstructed | 236 | 130 | 横隔重建 |
| reconstructed | dxf_geom | 210 | 5 | 展开前 DXF 直读杆（见 §4） |
| reconstructed | diag_synth | 210 | 83 | 斜材拓扑合成 |
| recognized | diag_synth | 90 | 28 | 同上（recognized 池） |
| recognized | dxf_geom | 65 | 3 | 同上 |
| 其余 12 簇 | — | 260 | — | 各 ≤55 |

## 2. 为什么 panel_template 剪不掉：证据对称性

对 176 个 kfan 层对逐对统计 TP：116 个 0-TP 层对承载 1037 FP。理想
剪枝面是「把 0-TP 层对整层删除」，但每一条可用于区分「该层对是否真实」
的诚实证据都被证伪：

| 候选规则 | 结果 |
|---|---|
| A. 其它来源杆件跨同一层对（±300） | 丢 35 TP（错剪 14000→9000 等 5 对真面板） |
| B. junction 兄弟层对有证据 | 仅剪 76 FP，且 21000/20700 深面板漏网 |
| C. 目标层层位证据（L1 环 ∪ L2 端对 ∪ L3 端点簇） | 丢 47 TP |
| D. 展开前图纸已画跨度佐证（±300-500） | 丢 209 TP |
| 跨度匹配门（非模板跨度 ±tol） | tol=0 丢 162 TP；tol=250 仍丢 99 |
| 单册 z 覆盖（层对落在单册内 vs 跨册） | 内册 TP=141 / 跨册 TP=217，无分离 |
| 链式截断（遇下一 junction 停） | 丢 247 TP（GT 自身违反：19000→15000 跨 16000） |

根因（GT junction 跨度普查，`scripts/diag_fp_pairs.py` 输出）：junction
6500 的图纸以下完全空白（07 册底 5759mm），但 GT 真实存在 d2500–5500
深面板；junction 19000 图纸只画到 d2000，但 GT 真实到 d4000。即
**「图纸没画」不等于「结构不存在」**——0-TP 深层对（24000→d4000/5000、
22800→d4000-6000、21000→d4000、20700→d3000 等）与这些真实深面板在
图纸侧证据状态上完全相同。模板按 recall-first 设计（默认全深度 2000–
5500 补全），是当前唯一不依赖 GT 注入的选择。

## 3. 口径重述（对外 ≠ 内部）

- **对外主口径** A2-dual-view-pure：P=63.5% / R=28.4%（TP=304/FP=175），
  「front∪side 杆粒度并集、仅 recognized 入池」。panel_template/
  terminal_pair/diaphragm 等生成器不进此池。
- **内部归因口径** A2-dual-view-reconstructed：P=30.5% / R=99.7%（TP=1068）；
  A2-front-full：P=25.6% / R=85.8%（TP=919/FP=2666）。低 P 是过生成换 R 的
  直接结果（dual full R=99.8% 红线 TP=1069 由此保住）。
- 提升对外 P 的正确路径是提高 recognized 池的杆件识别率（缩小
  「需要 parametric 池兜底」的范围），而不是删 full 池的生成器。

## 4. 已落地的两个零损失杠杆（2026-09-05）

### 4.1 exact_overlap_dedup（严格 3D 重复杆）

terminal_pair_dedup 只删 tps×非tps 重叠。离线归因发现还有跨生成器的
严格 3D 重复（共线 ≥0.999、段距 ≤10mm、轴向重叠 ≥90% 短杆）：
panel_template×leg_synth 535 对、panel_template×terminal_pair 411、
leg_synth×terminal_pair 314 等（共 4284 对）。同一物理杆被多个
生成器各放一份，1:1 匹配下最多 1 个 TP，其余全计 FP。

实现（`tower_geometry.exact_overlap_dedup`，tower_symmetry 终态调用，
报告写 `drawing_file.exact_overlap_dedup`）：union-find 聚类后**仅当簇内
存在多数节点对（≥簇半数）**才删同节点对副本——首跑教训：无差别
保-1 删了 1052 杆、TP 920→823，因为链式传递把同 3D 轴线上的不同
物理杆（不同节点对，各自独立 TP）并进一簇。生产不可知匹配结果，
节点对多数判据是唯一诚实判据。终跑 removed=14。

为什么离线「同 GT 簇可删 684」而生产只删 14：那 684 杆全部是
**节点对不同但 3D 共线**的副本（端点细分 68 / 端点偏移 100），
生产侧无法区分「同杆双放」与「相邻真杆」。

### 4.1b 证据优先层（evidence-preferred，第二跑追加）

§4.1 节点对多数层之上再加一层**证据优先**规则：recognized 杆
（origin ∈ {dxf_geom, marker_synth, leg_synth, diag_synth,
diag_complete, side_direct, collinear_stitch}）被**非 recognized**
杆严格 3D 重复时，删非 recognized 副本（证据赢）。判据仍是 §4.1
严格重复测试，但**不要求节点对相同**——证据杆与模板杆节点对天然
不同（模板按网格生成），节点对判据在此失效；证据杆几何直接来自
DXF 图纸线，是「同一物理杆」更强的信号。

生产终跑：removed=248（其中证据优先层 238、节点对多数层 10）。
front full TP=913 FP=2451（P 25.6→27.1%），dual 并集 1069 红线
保持，A1 168/197 无回归，五层口径全部改善或持平
（parametric P 33.6→34.2%）。离线仿真预示 drop 181（41 TP + 140 FP）
≈ 双视图下净 +1 TP（1070），生产实测 913/2451 与离线一致性在
1:1 匹配抖动范围内。

### 4.2 headx 证据覆盖门（塔头模板杆不重复生成）

`complete_head_panel_chain`（S8.4）此前无证据门：塔头 156 根模板杆中
92 根被既有非模板杆（marker_synth/diag_synth/dxf_geom/diaphragm）
几何覆盖。新增门：候选杆若被既有非模板杆**完整覆盖**——共线 ≥0.985、
段距 ≤150mm、既有杆更长或等长（len≥1.0×）、轴向覆盖 ≥95%——跳过
生成（`_covered` 检查，tower_geometry.py）。

阈值是离线零损失点（cos 0.985 / len≥1.0 / ov≥0.95 @150mm：
删 40 杆，front TP 919 与 dual 1069 均不降）。两处泛化均被数据否决：
- 放宽门（cos 0.985 不要求 len≥1.0）→ dual 1069→1066（3 个 TP 模板杆
  被更短的证据杆部分覆盖，换手后证据杆够不着 GT）；
- 推广到全部模板杆 → front TP -23（模板 TP 杆常是唯一命中者）。
终跑：parametric 池 FP 1257→1130（S8.4 子簇 -41，kfan 子簇不变）。

## 5. 残余簇与口径（未动手）

- kfan 深层对（§2）与 xpanel 网格错位（~200mm 级）：诚实不可分。
- 对外主口径 A2-dual-view-pure P=63.5% 不受任何生成器 FP 影响；
  提升对外 P 的正确路径是提高 recognized 池识别率，不是删生成器。

## 5.1 「l 面斜杆迁移 side_direct」负结论（2026-09-06）

动机：pure 池 816 根 full-TP 杆被非 recognized 杆命中，其中 l 面
斜杆 35 根（diag_synth/dxf_geom/diag_complete）。离线仿真把它们的
语义改为 side_direct（side 视图直读）后 dual-pure TP 304→334
（P 63.5→65.0%）——一度看起来是坦途。

否决证据（02 册侧立面画线全量核查）：

1. **图纸不画 l 面单面斜腹杆**。侧立面 region 内 280 条杆图层
   LINE 里，同号（单面）长斜线只有主腿 4 条（5348/5878mm×2 对）
   与 320mm 塔头小撑；其余全是跨面 X 撑（±yd 对称对）、水平横梁
   与 <30mm 标记短线（206 条）。l 面斜腹杆在侧立面【无画线证据】。
2. 离线 +30 TP 的真相：把镜像面杆（face=b/l/r 的 dxf_geom 等）重标
   为 side_direct 在生产侧等于「侧立面没画也宣称直读」——与
   「不用算法补全」红线冲突；不重标只改 geometry_class 则是
   纯重分类虚增（对照实验 +47 TP，已否决）。
3. 「迁移对象与现有读数端点距 5-123mm」是节点共享的假象：跨面
   X 撑端点也在 l 面锥面上，端点近不代表杆是同一根。
4. 侧立面真实可读内容（主腿、跨面 X 撑 16 条、横梁）模型已全部
   通过其它通道覆盖（side_reads 漏斗观测：提取 81 杆全部过冻结门，
   x_face_plane=76，zy_unsolved=0——提取与冻结无损失）。

塔身 0-30m 各册（04-07/40）连侧立面都没有（view_regions 全
front-only）——该通道在全塔范围内物理不可用。

## 6. 决策记录

- kfan/xpanel 主体剪枝：**不做**（§2 证据对称性）。
- l 面斜杆迁移 side_direct：**不做**（§5.1 图纸无画线证据）。
- exact_overlap_dedup（节点对多数层）+ headx 覆盖门：**已落地**（零损失
  FP -55）。
- exact_overlap_dedup 证据优先层：**已落地**（FP -215，dual 红线保持）。
- 验证（证据优先层后终态）：full TP=913 FP=2451（P=27.1% R=85.2%），
  dual 并集 1069 红线保持，A1 168/197 无回归，pytest 736/5，
  web/demo 镜像 sha 一致。
- 复跑：`python3 scripts/run_35A1_jc1_full.py`；
  FP 结构归因：`python3 scripts/diag_fp_pairs.py`。
