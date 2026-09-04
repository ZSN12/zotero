# Changelog

本文件记录对外可见的版本变化。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

- LevelGridSolver：从 DXF 尺寸/标注投票自推层网格（设计论证中）。

## [0.4.0] - 2026-09-05

### Added
- P2.5 腿链合并豁免：`leg_synth` 表驱动跨型杆不参与链合并/重复去重
  （双拼角钢邻段差 100mm 被 120mm 容差误杀的通用修复，+23 TP）。
- web/demo 镜像一致性门禁：`scripts/check_demo_mirror_sync.py`（sha 级）
  + CI 夹具测试（`tests/test_demo_mirror_sync.py`，7 例）。
- `stitch_leg_chains` 报告新增 `skipped` 豁免通道计数（可审计）。

### Fixed
- 撤回 P2.6/P2.6b 跨型腿 span 合成（+28 TP）：span 表系 GT FN 分析
  挑选而非设计常数——外部审计裁定为 GT 注入，全额 revert；
  对外基线回到 dual-pure 304 / P 63.5% / R 28.4%。
- 审计遗留 B1-B4：ZC1 产物滞后重跑、leg 链豁免零测试、
  README/SKILL/CALIBER_DISCIPLINE/task_brief 数字漂移（220→304）、
  tower_geometry.py 硬编码层常量归属修正。

### Security
- 发现并记录 `~/.zshrc` 明文 API key 风险（本地环境，不涉仓库）；
  仓库侧确认无 kimi/k3 调用路径，k3 例行审查停用并关闭待办。

## [0.3.0] - 2026-09-03

- 开源发布第一轮：LICENSE（MIT）/ CI（tests + gates + full-suite）/
  README 前台化 + 品牌站 5 页（0→1）。
- ZC1 换塔泛化基线：dual-union R 75.8%（216/284）。
- P0/P1 审计修复：observations/hypotheses 证据层、ZC1 多塔层表泛化。

（更早历史见 `git log`；版本锚点以 pyproject.toml 为准。）
