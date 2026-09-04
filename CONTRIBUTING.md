# 贡献指南（engineering-trace）

本项目是图纸结构化追踪管线：DXF 进、杆件模型 + 证据链 + 多口径评测出。
贡献流程很短，但**门禁纪律是硬约束**——详见下文「提分纪律」。

## 开发环境

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-tower.txt
pytest            # 快层（unit/pipeline，秒级~分钟）
```

- Python ≥ 3.10。主管线**离线运行**（不需要任何 API key）。
- 全量跑批示例：`python3 scripts/run_35A1_jc1_full.py --out-dir <dir> --skip-sync`
  评测：`python3 scripts/eval_a2_profiles.py <gt.json> <model.json>`

## 提交流程

1. 分支/直接提交均可，commit message 用中文 conventional 风格
   （`feat:` / `fix:` / `docs:` / `revert:` …，历史一致）。
2. 提交前自查：
   - `pytest` 全绿；
   - `python3 domains/angle-tower/scripts/self_test.py --quick`（门禁 1）；
   - 涉及管线产物的改动：跑全量 A/B（改动前后各一次完整跑批），
     JC1 `A2-dual-view-reconstructed` Recall 红线 **≥ 99%** 不得回退。
3. 涉及对外口径数字（TP/P/R）的改动：三处文档必须同步
   （`domains/angle-tower/SKILL.md`、`domains/angle-tower/docs/CALIBER_DISCIPLINE.md`、
   `docs/task_brief_pure60_jc1_zc1.md`）——CI 的 Bug D 一致性门禁会抓漂移。
4. 重跑 `out/` canonical 后：`python3 scripts/sync_demo_assets.py`
   同步 `web/demo/`，再跑 `python3 scripts/check_demo_mirror_sync.py`
   （镜像停更/跨塔指纹污染都会被抓）。

## 提分纪律（硬约束，违反 = 改动作废）

- **口径诚实**：对外主口径 `A2-dual-view-pure` 只收
  `recognized` + 指定 origin（`dxf_geom`/`marker_synth`/`collinear_stitch`/
  `leg_synth`/`diag_synth`/`diag_complete`）的杆件；镜像/重建杆件归
  `reconstructed` 辅助口径，**不得冒充直读能力**。
- **禁 GT 注入**：任何从 ground truth 反推的量（坐标、区间、层表）
  不得进入管线并标注为"设计常数"。z-only 层表注入必须走
  `level-assisted` 口径并在 `version.json` 的 `gt_injected` 段披露。
  历史教训见 `docs/TECH_DEBT_REGISTER.md`「P2.6 注入撤回记录」。
- **评测器不动**：`traceability/eval/metrics.py` 的容差与匹配器
  （Hungarian、endpoint_sum_cost）不随优化改动。
- **全量 A/B**：每个提分改动跑完整跑批对比 + `pytest` +
  六层审计（`domains/angle-tower/scripts/run_layer.py 1..6`）+
  换塔回归（ZC1）不回退。
- **可追溯**：每条修复带 commit 出处；技术债/撤回决策记
  `docs/TECH_DEBT_REGISTER.md`。

## 代码风格

- 行长 ≤ 100；中文注释允许（历史一致）；关键算法处写「为什么」
  （病灶实测数据），不是复述代码。
- 新算法必须带单元测试进 `tests/`（参考
  `tests/test_leg_chain_stitch.py`：真实病灶场景 + 合成夹具）。
