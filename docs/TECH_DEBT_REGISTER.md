# 技术债登记簿（P3-7 审计落地）

日期：2026-09-04　来源：k3 + DSH 代码审计（用户确认清单）

## 已修复（本批次，commit dcf508d / 160c3bc / 8668923）

| # | 问题 | 状态 |
|---|------|------|
| 1 | tower_batch.py:432 `dxf_paths` NameError 死路径（side_horiz_synth 永不执行） | ✅ 改从 batch["files"] 取路径 + 异常留痕 |
| 2 | JC2/ZC1 run_*_full.py `# noqa` 拼进冻结基线路径字符串 | ✅ 修正路径 |
| 3 | 三份 run_*_full.py 崩溃兜底引用 main 局部 out_dir（兜底自身 NameError，traceback 永不落盘） | ✅ 模块级 _CRASH_OUT holder |
| 4 | experiment_collinear_stitch.py 防镜像洗白守卫 inherit_cls 被 update 无条件覆盖 | ✅ 删除硬编码键 |
| 5 | skill/contract.py「冲突不覆盖」承诺 vs add_component 静默覆盖 | ✅ to_engineering_model 层候选级裁决 + id_conflict 标记（含 k3 复审的三方冲突修复） |
| 6 | model.refresh docstring 与实现不符 | ✅ docstring 修正为实际语义 |
| 7 | tower_solver.py:259 注释说符号取质心方向但实现恒 +1 | ✅ 按 ±delta 近质心侧实现（A/B 指标不变） |
| 8 | __version__ 0.1.0 vs pyproject 0.4.0 | ✅ 统一 0.4.0 |
| 9 | pyproject 无 build-system/dependencies/scripts | ✅ 补齐（wheel 构建通过；离线沙箱无法验证依赖安装） |
| 10 | io.py schema 路径按仓库布局解析（装完即断） | ✅ 三级回退（仓库/包内/CWD）+ 可操作错误 |
| 11 | DXF TEXT 件号 mm 存进 x_px 像素键（跨源去重失效） | ✅ drawing_x/drawing_y + coord_space + 去重键统一 mm 空间（OCR 两处同修） |
| 12 | validate_references 循环内重建 all_nodes（O(n²)） | ✅ 提出循环外 |
| 13 | _dedup_exact_overlap_segments 全量对比（O(n²)） | ✅ 网格索引（200 组随机对照等价验证） |
| 14 | canonical_tower ~/Downloads 硬编码 | ✅ ETRACE_MOD/ETRACE_NODE 环境变量 → 仓库 examples → 旧路径三级解析 |

## 遗留（已评估，按风险/收益排期）

### L1 — 塔型硬编码泄漏（换塔泛化阻塞项）
- `generate_diaphragms` z 集合（22700/32700…）、`MODULE_DEFINITIONS` M1-M6、
  terminal_pair zone 默认值：全部 JC1 实测值内嵌代码，换塔靠 overlay 逐项
  覆盖，漏配 = 静默 FP。
- **处置**：Phase 3（35A2-JC1 接入）时把这些常量迁入 domain config
  （overlay/skill 层），代码只留通用算法。接入第二座塔是天然的压力测试。
- `gt_profile.py` 全部数值：仅 debug 用途，不进交付管线，风险低，随手修。

### L2 — 巨型函数拆分（可维护性）
- `expand_4_face_symmetry_model` ~1760 行 / `extract_tower_from_dxf` ~900 行 /
  `deliver_project` ~970 行 / `tower_views.py` side 链。
- 中文归因注释目前是唯一的行为文档——拆分时**必须**随函数搬迁注释，
  每步 A/B（JC1/ZC1 双回归）。
- **处置**：不建议本周期动。Phase 2 提分工作正在这些函数上叠加（P3-6
  刚落了折叠链修复），先冻结行为再拆结构。拆分排 Phase 4（产品叙事
  阶段，代码 churn 最低）。

### L3 — centerline_extract 四处 except: pass
- 整条重建链被静默吞错，失败只有 audit 可查。
- **处置**：与 P2.4（centerline_geom_filter）合并处理——towers 走 ezdxf
  矢量路径已绕开 centerline_extract 主链；MLLM 扫描路径是 fallback。
  排 Phase 3 回归网建设时统一加 graph.finish 留痕（hybrid_dxf_agent 的
  P4 模式：失败记录到 graph，不炸主链）。

## 红线（不变）
- JC1 dual-pure TP ≥ 304（P2.6/P2.6b 注入撤回后基线，987abd7；+23 系 6b7831b 通用真修复）
- JC1 dual-recon R ≥ 99.6%（现 99.6%）
- ZC1 dual-union R ≥ 75.8%
- 任何改动全量 A/B + pytest（k3 审查按用户 2026-09-05 指示停用）

## 待办
- [x] ~~P3-7 第二批（8668923）k3 审查~~（2026-09-05 关闭：k3 例行审查已按
      用户指示停用，不再排期补审。第一批（dcf508d/160c3bc）已审毕并落地
      其修复；代码注释里的「k3 审查（2026-09-04）」是历史修复的出处标注，
      非活动调用。核验：kimi CLI 最后活动 09-04 10:47，此后零调用；当前
      管线/CI/测试均无 k3 调用路径。）

## 召回天花板分析（2026-09-05，dual-pure 332 后 FN 版图）
- 双胞胎竞争（~440 FN，cost≤500 未匹配）：GT 在同位置计 N 根物理杆
  （双拼角钢/多件号同位），图纸只画一条线（角钢轮廓线对）。一条画线
  ↔ N 根杆 → Hungarian 每位置最多 1 TP。结构性天花板，无 GT 注入
  不可破。500-800 近失群（~107 FN）经查同一现象（best cost 18-95
  的孪生）。
- z<7000 底段（82 GT 杆 + 44 严格角腿 FN）：00-1/00-2/01-1/01-2 均
  为小区域详图（50-636 线），全图册无底段立面。证据不可观测，非
  可回收池。
- 塔头（z>33000，~29 FN）：横担横向构件（塔尖横担端部横杆/桅杆箱
  水平弦）需 3D 头部建模；现有 crossarm 补全为 parametric 层。02
  册头部画线密集（368 线/层）但碎屑化严重。
- side 20k 簇（~28 FN）：画线为水平线，GT 为斜深杆（depth diag），
  画图与 GT 结构不一致，不可诚实回收。

## P2.6 注入撤回记录（2026-09-05，外部审计裁定）
- 外部审计决定性发现核实成立：cross_sheet_leg_spans_mm 11 条 span 与 GT
  区间 11/11 精确对应（144 根 GT 杆），span z 端对来自 GT FN 分析而非
  设计常数；docstring/overlay 标注「z-only 设计常数/纯 DXF 证据链」与
  来源不符。git revert 987abd7 全额撤回 +28 TP（1e1c8e0 +26、4a637bb
  +2）；保留 6b7831b +23（leg_synth 豁免链合并，通用真修复）。
- 撤回后复核：dual-pure 304 / 63.5% / 28.4%（精确恢复 P2.5 基线），
  dual-recon 1067/99.6% 红线保持；ZC1 重跑 9 / 216/75.8% 持平。
- 规则重建评估（否决）：跨册节点不共享 id；front 投影每侧 6 条平行
  腿线、家族 x 差（~30mm）与跨家族错配同量级；图纸分段（07 表止于
  12000 / 06 表起于 12000）与物理分段（11500-14500 等）结构性不一致且
  无图纸证据可分辨——并集配对容差只能靠 GT 校准，属二次拟合。
- 跨册物理分段（GT 144 根）改判「非诚实可回收池」，与 z<7000 底段、
  双胞胎竞争池并列。
- 审计遗留项处置：
  - B1（ZC1 产物滞后）：重跑 35A2-ZC1-rerun1 并提升 canonical（生成
    于当前 HEAD）；注：审计所指"MISMATCH"系对比目录用错（deliverable
    用 guowang_35A2_zc1 专用 overlay，sha 匹配；真实问题仅产物滞后）。
  - B2（零新增测试）：补 LegSynthExemptTest 三例（豁免保留/不与 dxf
    碎段合并/审计计数）；stitch_leg_chains report 新增 skipped 通道
    计数并落 leg_chain_stitch_report（实测 leg_synth_table=64、
    terminal_pair_structure=304）。
  - B3（对外文档漂移）：README:53 与 SKILL.md:111 由 220 更新为 304
    （P 63.5% / R 28.4%）。
  - B4（L1 硬编码）：tower_geometry.py:1218 层常量组经 blame 属
    f224da0（2026-09-02 P3.12c，前轮遗留）而非本轮新增——审计归属
    有误但债本身真实，维持登记（L1 硬编码 Phase 3 项内）。
