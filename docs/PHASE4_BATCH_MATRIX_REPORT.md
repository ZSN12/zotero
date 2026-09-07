# 阶段四交付：证据置信分层剪枝器效果矩阵与双塔指标对比

**日期**：2026-09-07（阶段二/三收官 + 阶段四定量）
**范围**：35A2-ZC1（主攻坚塔）/ 35A1-JC1（红线参考塔）
**评测口径**：`eval_a2_dual_view`（FROZEN，front∪side 杆粒度并集、
Hungarian 1:1、端点误差和 < 500mm）

## 1. 双塔总指标（A2-dual-view，full 口径）

| 塔 | 规则前 | R1'+R2 后 | +R5 后 | +R6 后（当前） |
|---|---|---|---|---|
| ZC1 P% | 20.7 (TP259/FP991) | 29.3 (TP256/FP618) | 29.6 (TP256/FP608) | **30.0 (TP256/FP597)** |
| ZC1 R% | 90.5 | 89.8 | 89.8 | 89.8 |
| JC1 P% | 32.3 | 32.3 | 32.3 | 32.3（不变） |
| JC1 TP | 1067 | 1067 | 1067 | 1067（红线零变化） |

JC1 pure 口径（对外主口径）：TP=304 / P=63.5% / R=28.4%，全程精确保持。

## 2. 剪枝规则矩阵（evidence_prune.py，全部 overlay 显式开关，默认关）

| 规则 | 判据（杆自身结构属性，无塔名分支） | ZC1 削减 | JC1 影响 |
|---|---|---|---|
| R1 无投影证据 | origin ∈ 推断层（dtd/marker_synth/stitch/bridge）且无 projection_refs | 112 | 17（marker_synth 15 TP 带 refs 全豁免） |
| R2 镜像面直读 | dxf_geom + face∈b/l/r + generated_4face | 275 | overlay 未开启（离线 -269FP/+3TP） |
| R5 塔尖平台环 | derived_from=tip_platform + terminal_pair_gen + 无 refs | 10 | 生成器在 JC1 无角点证据，天然 0 杆 |
| R6 腿链拼接桥 | origin=leg_chain_stitch（refs 指向源线非自身证据） | 11 | overlay 未开启（离线 -19FP/TP 不变） |

**设计纪律**：
- 每根被剪杆写 `pruned_by` provenance，审计报告落 `drawing_file.properties.evidence_prune_report`；
- 规则只消费 model 组件属性 + overlay 配置，不读 GT、不读评测结果；
- R1 的 projection_refs 前置是双塔分流的关键——JC1 marker_synth 15 TP 全带 refs
  而同源杆在 ZC1 无 refs，一条规则两塔自适应。

## 3. 残余 FP 归因（ZC1，FP=597）

| 来源 | TP/FP | 治理结论 |
|---|---|---|
| panel_template_completion | 102/244 | 模板层，TP 主力（不可剪）；层位节拍与 GT 局部错配 |
| terminal_pair_gen | 143/132 | TP/FP ≈ 1:1，节拍层一半命中一半偏移 |
| dxf_geom 直读 f 面 | 4/95 | **端点级噪声**（中位误差 2.2m，dz 各册方向不一致——05:-556/08:-715/09:+195mm），非层位平移；校正实验（05 册 z 平移扫参）TP 不涨反跌。上游提取质量挂账 |
| derived_parametric_base | 0/52 | JC1 同构 12/44 有 TP——结构不可分，保守保留（决策记录） |
| marker_synth | 0/40 | JC1 同构 15/63 有 TP——refs 字段无法区分提取质量，保留 |
| diaphragm_reconstructed | 14/34 | 真实结构，比例尚可 |

**负结论存档**（防止重复探索）：
1. 塔头横杆层位治理：GT 层 (34000/36600/39400) 与非 GT 层的模板杆证据构成完全同构，无诚实判据（+2.9P 上限存在但不可达）；
2. R3 宽域悬空剪枝（any-origin no-refs + deg1）双塔各损 8/7 TP——伤害面太大；
3. R4 BOM 配额：843/1381 杆无 source_file，segment 配额咬不住模板层。

## 4. ladder_z 纯图纸 z 链三塔验证（生产标定路径）

| 塔 | ladder junctions | 命中 GT 层位(±300mm) | 备注 |
|---|---|---|---|
| 35A2-ZC1 | 11 | 8/11 | anchors 39400/33000 精确；总高=GT顶 |
| 35A3-SZC1 | 26 | 20/26 | 总高 36100 vs GT 39800（避雷针段不在阶梯） |
| 35A1-ZC1 | 失败 | — | 01-1 版式不同（H 列分散，子列和<塔高），留作已知限制 |

ZC1 junctions `[5500, 9100, 10500, 11900, 14400, 16900, 19400, 33000, 39400]`
与 GT 层位表逐一对应——ladder_z 是「GT 反投影标定」的可行生产替代，
三塔批跑矩阵的全自动标定路径成立（35A1-ZC1 版式兼容挂账）。

## 5. 交付物索引

- `traceability/solve/evidence_prune.py`——R1/R2/R5/R6 生产实现
- `tests/test_evidence_prune.py`——12 项单测全绿
- `examples/external/guowang_35A2_zc1/layer_overlay.json`——ZC1 规则开关
- `web/demo/35A2-ZC1/trace.html`——3D↔2D 双向追溯联动页（阶段一交付）
- 提交链：c0accbe → 56c4e46 → 7519d84（全部已推送 origin/main）

## 6. 下一步（阶段四续）

1. 35A1-ZC1 ladder 版式兼容（H 列聚类策略放宽）→ 三塔全自动批跑；
2. dxf 直读端点噪声治理（提取截断/粘连——A0 版面层问题）；
3. 跨塔型指标对比表扩展到第三塔后重发。
