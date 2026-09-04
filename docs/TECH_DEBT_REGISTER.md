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
- JC1 dual-pure TP ≥ 330（P2.6 后新基线，1e1c8e0）
- JC1 dual-recon R ≥ 99.6%（现 99.6%）
- ZC1 dual-union R ≥ 75.8%
- 任何改动全量 A/B + pytest（k3 审查按用户 2026-09-05 指示停用）

## 待办
- [ ] P3-7 第二批（8668923）k3 审查：沙箱离线（OAuth TLS 断连）未完成，
      网络恢复后补审。第一批（dcf508d/160c3bc）已审毕并落地其修复。
      （注：k3 例行审查已按用户指示停用；此条仅在网络恢复后按需补做。）
