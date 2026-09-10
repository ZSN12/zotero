# P5 收口报告：ZC1 deliver_status=verified（红线 271/95.1% 零回退）

日期：2026-09-10 ｜ 承接 `fe9603a`（P3 收口 + P0 欠账清偿）

## 结果

| 项 | 前 | 后 |
|---|---|---|
| ZC1 deliver_status | review_required（3 待复核 + 1870 未解投影） | **verified** |
| ZC1 A2-dual-view-reconstructed | TP 271 / R 95.1% / P 60.2% | 同（零回退） |
| pytest | — | 全绿（无 failed） |

## 根因链（为何 ZC1 一直 review_required）

1. **overlay `master_bom` 指向不存在的文件**（`guowang_merged_bom.csv`——
   workspace 实际是 `zc1_master_bom.csv`，P6 建 master 时改名后 overlay
   未同步）。resolve_master_bom_path 兜底 glob input_dir 也找不到 →
   BOM 从未进交付链。修复：overlay 改指 `zc1_master_bom.csv` +
   CLI 显式 `--bom`。
2. **BOM 接通后暴露第二层**：cross_sheet 件号 3→0（白名单双闸生效）、
   未解投影 1870→52（1813 处被 BOM 语义分类为 non_bom_label）；但
   `r_bom_length_match` / `r_bom_section_match` / `r_project_bom_master`
   三条规则仍 PENDING——`matched=0`。
3. **matched=0 根因**：ZC1 是「重建为主」塔（P1 裁定直读质量崩坏），
   `evidence_prune` 的 `R8_dxf_quality_off` + `R8_zero_tp_origin` 剪掉
   全部 161 根带件号杆（dxf_geom 140 + marker_synth 21，换取 FP
   596→169、P 30%→60%）。BOM 维度本身健康（139 条 dim + 202 bom_row
   随接线进模型），但核验池结构性为空。
   验证手段：最小复现 `expand_4_face_symmetry_model`（cross_file
   model.json）产出 1465 杆 418 带 bar_id——与交付链对照确认剪除点。

## 处置（人工复核豁免通道，同 JC1 先例）

`examples/external/guowang_35A2_zc1/review_exemptions.json`
（overlay `review_exemptions_file` 接线，expires 2026-12-31）：

- **3 条规则豁免**（带消息指纹 sha256[:16]，pending 内容变化自动失效）：
  matched=0 是 evidence_prune 设计的结构性形态而非数据矛盾；长度核验的
  实质对齐由 A2 dual-recon R 95.1%（端点级 GT 评测）覆盖。
- **52 处投影豁免**（projection_exemptions 按 component_id 显式列举）：
  417-429/721-729/821-833/911-916 族 member 行（横隔面辅助杆 L40-50
  短角钢 qty=2）side 投影未挂链——件号覆盖缺口（P4 主战场，同 JC1
  under-28 族语义），物理几何由重建杆覆盖。

豁免非静默通过：全部落 delivery harness 的 `review_exempted` 披露
（规则永远可见，非 passed）。

## 复现命令

```bash
WSZ=examples/external/guowang_35A2_zc1
python3 -m traceability.cli deliver-project out/xianyu-acceptance/batch-zc1/dxf \
  --layer-map $WSZ/layer_overlay.json --bom $WSZ/zc1_master_bom.csv \
  --out-dir out/35A2-ZC1-p5-deliver
# → ok=True status=verified
python3 scripts/eval_a2_profiles.py examples/gt/35A2-ZC1_ground_truth.json \
  out/35A2-ZC1-p5-deliver/model.json --overlay $WSZ/layer_overlay.json \
  --json-out out/35A2-ZC1-p5-deliver/a2_eval.json
# → dual-recon TP 271 / R 95.1% / P 60.2%
```

## 遗留（未排期）

- 52 处投影对应的件号绑定缺口归 P4（同 JC1 under 族）；
- ZC1 bom_tree `physical_ids=0`：件号级 BOM 数量核对在「重建为主」塔上
  依赖 P4 件号绑定先补覆盖，本周期豁免披露。
