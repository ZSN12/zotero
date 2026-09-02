# 任务书：35A1-JC1 塔斜材碎片回收（pure 口径召回提升）

> 交接自 Claude 会话（2026-09-04，commit `40b6055`）。目标：修复 05 册（及
> 同型 06/07 册）DXF 斜线「碎片化 → 被最小长度门槛卡掉」的漏斗损失，
> 提升 A2-dual-view-pure 召回，同时严格保住 dual 红线。

## 0. 仓库与运行环境

- 仓库：`/Users/zsn/Documents/zotore/engineering-trace`
- 管线（全量，约 10 分钟）：
  `python3 scripts/run_35A1_jc1_full.py --out-dir out/35A1-JC1-<tag> --skip-sync`
- 评测（秒级）：
  `python3 scripts/eval_a2_profiles.py examples/gt/35A1-JC1_ground_truth.json out/35A1-JC1-<tag>/model.json --json-out /tmp/<tag>.json`
- 单册快速闭环（秒级，用于迭代，勿跑全管线）：
  `/tmp/fast_sheet_eval.py` 提供 `eval_sheet(dxf, stem, overlay, beat_anchors=...)`；
  beat 锚点从 `out/35A1-JC1-legsynth15/cross_file/model.json` 的
  `components.drawing_file.properties.dimension_beat_anchors_by_sheet` 取。
- 测试基线：`python3 -m pytest tests/ -q` → 595 passed（必须保持全绿）

## 1. 当前分数（legsynth15，验收基准）

| 口径 | TP | P | R |
|------|----|----|----|
| A2-dual-view-pure（要提升的对外口径） | 130 | 38.6% | 12.1% |
| **A2-dual-view-reconstructed（红线，必须 ≥95.0%）** | 1021 | — | **95.3%** |
| A2-front-full | 909 | 15.1% | 84.9% |

硬约束：任何改动后 dual-view-reconstructed 召回 **不得 < 95.0%**（余量仅
0.3pp，每轮必须复测）；595 测试全绿。

## 2. 问题定位（已完成的取证，直接采信）

05 册 front 立面画了 285 条斜线段，但进模型的 ≥300mm 斜线只有 **16 条**，
而 GT 05 册有 **120 根斜材**。漏斗（`traceability/intake/centerline_extract.py`）：

```
raw(collect_segments):  斜线 285 条，≥15u(300mm) 84 条（中位 47u，最长 195u）
stitch_collinear:       斜线 137 条，≥15u 28 条   ← 拼接反而减少（-148 条）
pair_double_lines:      斜线 123 条，≥15u 16 条
min_cand_mm=300 过滤后: 仅 16 条斜线进入 segs_out
```

关键事实：
- 被卡掉的 centers 短斜线长度直方（u）：0-3u:54、3-6u:10、6-9u:37、9-12u:4、
  12-15u:2 —— 大量 6-9u（120-180mm）碎段。
- 图纸斜线被画成「过节点断开」的短段（节间交叉点 T 打断），raw 最长 195u
  （3900mm）接近 GT 整跨斜材，说明**整跨长斜线确实存在**，但大量中等
  长度段在 stitch 阶段消失（285→137：共线拼接把非共线但相接的段吞了？
  或 pair_double_lines 配对失败丢弃？需要逐环节定位）。
- 下游 `traceability/intake/tower_dxf.py` 还有 T 打断/节点聚类/共线合并，
  也会再切一刀。

## 3. 任务目标

让 05 册 ≥300mm 斜线输出从 16 条提升到 ≥60 条（GT 120 根的 front 可见
部分约 60-80 条），且不引入噪声：

1. **诊断 stitch_collinear 285→137 的损失构成**：是被拼接合并、被配对
   丢弃、还是被去重？给出逐环节计数表（写在 PR 描述里）。
2. **修复碎片回收**：对「共线/近共线且端点相接（gap ≤ 1.5u）」的斜线
   碎段做拼接，目标把 6-9u 碎段串回 ≥15u 长段。注意 `stitch_collinear`
   已有 but 显然不够——查它的角度/间隙容差与方向约束。
3. **扩展到 06/07 册**（同型问题，先 05 验证后再推广）。
4. 若拼接后斜线端点与 GT 整跨端点仍有 >500mm 端点差（图纸实际起止与
   GT 分段不一致），**不要**强行拉伸——那属于 P2.4「斜材层位重参数化」
   （类比 leg_synth：z 端点取层位表常数、x 与斜率取图纸线），另立任务。

## 4. 纪律红线（违反 = 重做）

- **pure 口径证据纪律**：pure 池只收 `dxf_geom / leg_synth / collinear_stitch /
  marker_synth`（图纸直读或其确定性合并）。拼接产物标
  `geometry_origin="collinear_stitch"` 即可进 pure。
- **禁止 GT 坐标注入**：不得读 GT 的 x/y/杆件拓扑。z-only 的层位/跨型
  常数表允许（先例：`beam_marker_levels_mm`、`leg_synth_spans_mm`，见
  `examples/external/guowang_35A1/layer_overlay.json`）。
- **禁止改评测器**（`traceability/eval/metrics.py` 的匹配/容差逻辑）。
- 历史教训（代码注释里有完整记录，改动前必读）：
  - `tower_geometry.py` stitch 系列：max_single_len_mm=800、max_segments=2
    是血泪参数——中长杆（>800mm）参与拼接曾毁掉已有匹配（TP 208→188）；
  - 拼接端点**严禁吸附现存节点**，必须用精确投影极值新建节点；
  - leg_synth 的 x 钳位（centerline_extract.py P2.2 注释）：新节点会成为
    聚类极值 → 全塔刚性平移 → dual 破红线。

## 5. 验收标准

1. 05 册 segs_out 的 ≥15u 斜线 ≥60 条（用第 0 节快速闭环量）。
2. 全管线跑 `legsynth16`：A2-dual-view-pure TP ≥ 150（当前 130），
   dual-view-reconstructed R ≥ 95.0%，A2-front-full TP 不降超过 5。
3. `pytest tests/ -q` 全绿。
4. PR 描述附：漏斗计数表（修复前后）、pure/dual/full 三口径前后对照、
   新增 origin 杆数。
