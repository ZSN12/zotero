# P3-5 水平层位佐证过滤 —— k3 审查证据与结论（2026-09-04 夜）

## 变更定位

- 代码：`traceability/intake/tower_symmetry.py` P3-5 `dxf_horiz_level_corroboration`（d735aea 引入，0fb35ff 加固）。
- 配置：`examples/external/guowang_35A1/layer_overlay.json` → `dxf_horiz_level_corroboration: {enabled, tol_mm=300}`。
- 效果（JC1 canonical）：dual-pure 275/59.4/25.7 → **274/61.4/25.6**（−1 TP 匹配重排、FP 188→172、P +2.0pp）。

## 过滤规则

近水平（`|dz| < 100mm`）且 `L ≥ 200mm` 的 `dxf_geom`（直读）杆，若 z 中点不在
**全塔所有册** `beam_marker_levels_mm` 并集 ±300mm 内 → `pure_excluded = "dxf_horiz_off_level"`。
pure 池除名、full 池不变、dual-recon 红线零风险。

## 关键证据

1. **首版误杀教训（per-stem → 并集）**：首版只用杆件来源册自身的层位表，全量
   A/B 净 −5（07 窗 z=6500 平台横杆由 06 册直读，而 06 册表无 6500）。改为全塔
   并集后消除；残余 −1 TP（PM_0087）为 Hungarian 竞争释放重排（其搭档是
   marker_synth，非 dxf_geom 直杀）。
2. **跨口径去向（k3 S2 要求的循环论证复核）**：32 根除名杆在 front/side 任一
   视图 500mm 邻域内 GT 杆数 = **0/32**——全部为爬梯/栓排/节点板排线噪声
   （07 册实测 160mm 等距排线族 z=7740/7900/8061/8221 + 近腿 stub），对 full
   口径 TP 零贡献。除名只影响 pure FP。
3. **加固（0fb35ff，k3 复审通过）**：两阶段事务（先收集后打标）；层位并集
   为空 → no-op + stderr 留痕；<3 层 → no-op + stderr 留痕（佐证面过窄，
   宁可放过不可误杀）；异常路径零污染。
4. **ZC1 回归**：9/6.2/3.2 无变化（ZC1 overlay 无该配置，过滤正确未激活）。
   dual-reconstructed 1066/1071 = 99.5% 红线不变。

## 审查记录

- k3-256k 首轮：S1 `sys` 未导入崩溃（已修）、S2 误杀面（已加固+证据）、
  S3 CLI 坏路径静默（已修 fail-fast）、S4 超时输出丢失（已修）。
- k3-256k 复审：S1/S3/S4 通过；S2 加固通过，指出 elif 文案与行为矛盾
  （已修：文案改为 no-op，与代码一致）。
