# MLLM A/B 三模型横评结论（2026-09-06）

> 问题：多模态视觉模型对 A2 几何检测分数有没有帮助？
> 结论：**没有。A2 自由画线路线（MLLM 直接输出杆件坐标）召回天花板过低，正式弃用；
> MLLM 的定位收敛为件号识别（A1）、版面理解（A0）、构件语义判别（中心线二分类）。**

## 实验设置

- 数据：35A1-JC1 全册 10 张 DXF（5 张结构图 02/04/05/06/07）
- 管线：`scripts/run_35A1_jc1_full.py --agent-mode hybrid --skip-sync`，独立 out-dir
- 基线：纯规则 ezdxf 矢量提取（out/_jc1_final.log，A2-pure TP=175 @tol=500mm）
- 模型可插拔（MLLM_PROVIDER_PRESETS），同图同 prompt，唯一变量是模型

## 三模型对照（A2-pure 口径，tol=500mm）

| | 纯规则 ezdxf | Gemini 3.7 Flash（antigravity-ocx） | GLM 5.3 Flash（glm-relay） |
|---|---|---|---|
| A2 方法 | 矢量提取 | mllm_geom 五册全通 | 3 册通 / 2 册降级 hough |
| MLLM 报杆数 | —（340） | 108 | 47 |
| A2-pure TP | **175** | 0 | deliver failed（合并全悬空） |
| A1 件号 Exact | 111（P 83.5% / R 56.4%） | 46（P 88.5% / R 23.4%） | 0（无从评） |
| 单次 A2 调用耗时 | — | ~20s | ~600-900s（relay 600s 墙，需 stream 聚合） |
| 合并 3D 框架 | 正确 | 正确（x ±2.46m） | 全悬空（206 节点 0 解，门禁拦截） |

## 关键事实

1. **GLM 全册失败的技术根因**：本地 opencodex relay 非流式请求 ~600s 硬超时，
   A2 大 JSON 生成超时即 502。已修（mllm_backend.py stream 聚合，commit 5ab8948），
   但 GLM 即便跑通：front crop 多次返回空内容（`Expecting value: char 0`）、
   杆数召回仅为 Gemini 的 1/3、速度慢 44 倍。
2. **Gemini 速度完全可用**（19.7s/crop），几何质量不差（合并框架坐标正确），
   但召回只有基线 32%（108 vs 340 根）——密集斜腹杆大量漏检，TP=0。
3. **A2 召回不足不是模型调参问题，是任务形式问题**：让 MLLM 自由生成几百根杆的
   坐标 JSON，输出长、慢、漏检结构性存在。两个模型、两轮全册实验一致。
4. **A1 件号线是可复现的净收益**：Gemini P 88.5%（基线 83.5%），零失败调用，
   7-16s/册。召回受几何杆数拖累（件号要挂在几何杆上）。
5. **30m 中横担缺口（半宽 1.10m vs GT 4.4m）未修复**：Gemini 在塔身中上段漏杆最严重。

## 决策（用户拍板 2026-09-06）

- MLLM 正式定位：**识别构件语义 + 件号 + 版面理解**；不做几何生成。
- 官网文案同步收敛（index.html "多模态只做它擅长的" 卡片、careers.html 技术栈行、
  product.html 三段式管线第一段）。
- 未尽事项（可选后续）：
  - 两段式 A2：ezdxf 矢量高召回提候选 → MLLM 只做是构件/噪声二分类
    （代码已有 centerline_classify 路径雏形）——保留矢量精度 + 用模型语义眼。
  - A1 件号线进主口径的工程化（BOM-valid orphan 判定复用）。

## 产物路径

- Gemini run：out/35A1-JC1-hybrid-gemini/（log: out/_hybrid_gemini_run.log）
- GLM run：out/35A1-JC1-hybrid-glm53/（log: out/_hybrid_glm53_run.log，deliver failed）
- 首轮 GLM（超时全灭）：out/35A1-JC1-hybrid-glm/（log: /tmp/hybrid_glm_run.log）
- 任务书：out/TASK_gemini_a2_ab.md
