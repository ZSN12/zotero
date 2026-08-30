# 对标：仝心圆官网（承诺）vs engineering-trace（实现）

日期：2026-08-30
官网：https://concentriccirclesmrtt.github.io （源码 repo：ConcentricCirclesMRTT/ConcentricCirclesMRTT.github.io，React+TS+Vite，hash 路由）

> 取材方式：SPA 无法静态抓取，改为读取官网仓库 `src/App.tsx`（32KB，整站单文件）
> + `public/neube-sr-showcase/`，拿到的是**逐字文案与功能清单**，比渲染截图更完整。

---

## 0. 一句话结论

**叙事与架构层已对齐甚至超过官网；差的是「结果本身」。**
官网卖的是"结构准确、可核验的三维几何"，而我们目前拿不出一个
无缺口、能过自家验证的全塔 DXF 重构——A2 召回 2.2%、底段无源、横隔 z 错位。

---

## 1. 官网是什么

- **5 个页面**：#/（首页）· #/product · #/research · #/about · #/careers
- **2 个 3D 查看器**：TowerModelViewer（产品页）、LongContextTowerViewer（长上下文区，M1–M6）
- **1 个独立展示页**：/neube-sr-showcase/index.html（NeuBE SR，6 张图 + 2 个 GLB + STORY_SCRIPT 叙事脚本）
- **公司主体**：浙江每日互动研究院有限公司（关联：每日互动 ge.cn、个推 getui.com、泰昌集团）
- 岗位：多模态模型研究工程师 / 多模态 Agent 研究工程师（杭州）

## 2. 官网的四个核心承诺（产品页）

1. DRAWING READINGS 图纸读数：尺寸、标注、构件编号、跨视图对应
2. BOM CROSS-CHECK 物料表核验：规格/数量/编号交叉核对
3. GEOMETRY VALIDATION 几何求解验证：拓扑连接、几何约束、结构闭合、工程一致性
4. LONG-CONTEXT：跨页证据连续 / 跨视图身份一致 / 跨模块装配闭合（M1–M6）
   证明条：**6 个模块 ｜ 多页图纸 ｜ 多视图关联 ｜ 统一三维装配**

---

## 3. 逐板块对标

| 官网板块 | 承诺 | 我们现状 | 判定 |
|---|---|---|---|
| DRAWING INTAKE | 扫描图 / PDF / DWG / DXF | DXF✅ ezdxf+overlay+batch；DWG⚠️ 依赖 ODA 外部转换；扫描图 PNG✅ A0–A4 链；PDF✅ pymupdf | 🟢 85% |
| ENGINEERING COMPILATION | 标注/构件关系/BOM 交叉核验 | DXF 全有 + master_bom 交叉核验；扫描图无 MLLM API 时件号关联=0 | 🟡 60% |
| 几何求解验证 | 拓扑/约束/闭合/一致性 | Harness 五规则 + 依赖 DAG **存在**，但没拦住自家横隔 z 错位（见 P2） | 🟡 55% |
| LONG-CONTEXT | 跨页/跨视图/跨模块 M1–M6 统一装配 | 跨页✅ SourceRef+steps.json+cross_file；跨视图⚠️（40 段曾错标、底段无源）；跨模块⚠️ m1_m6 配置在但底段缺 5500mm、横担半宽 ±1352 vs GT ±2200 | 🔴 45% |
| VERIFIED DELIVERY | 可核验/可追溯/可编辑 | 依赖 DAG + 变更传播 + strict 导出阻断 + SourceRef——**官网只有口号，我们有实现** | 🟢 75% |
| NeuBE SR 开源 | fully open-source、可 Fork | git remote = ZSN12/zotero.git，**无独立公开开源仓**；SKILL.md/架构文档/benchmark 在本地 | 🔴 30% |
| 官网本体 | 5 页品牌站 + 2 查看器 + showcase | 本地 web/ 是工具型 demo（上传→GLB+Harness 摘要+追溯），无品牌叙事页 | 🔴 20%（若目标是建站） |

> 百分比是判断性估计，用于排优先级，不是精确度量。

## 4. 三个最大差距（按影响排序）

### ① 几何正确性 —— 官网的核心卖点，目前兑现不了
- A2 召回（physical 口径）**2.2%**；横隔 front 投影与 GT 端点中位差 **1192mm**，0/295 匹配。
- 根因：横隔**半宽与 z 不自洽**（半宽 2036 对应 GT z≈10371，却标成 7034）。
  `generate_diaphragms` 用了 DXF 段内局部 z。
- 讽刺点：官网承诺"几何求解验证"，而我们的验证器**没拦住自己生成的几何错误**。
  → 修复 `generate_diaphragms` 是当前最高优先级（UNIMPLEMENTED_PLAN §P2）。

### ② 三维装配不完整 —— 官网证明条四项只成立两项
- "6 个模块" ✅、"多页图纸" ✅、"多视图关联" ⚠️、"统一三维装配" ❌
- 底段 z[0,5500]（15% 塔高、最宽最重的部分）**全册无源**（已穷尽：最大底宽 4594mm vs 需要 5524mm）。
- 02 横担半宽 ±1352 vs GT ±2200，横担不完整。
- → 官网的 LongContextTowerViewer 展示的"完整塔"，DXF 管线目前重建不出来。

### ③ 开源承诺未兑现
- 官网写"fully open-source、可 Fork"，但仓库未发布（remote 是无关的 zotero 仓）。
- 官网 showcase 有完整的 STORY_SCRIPT 叙事 + 6 张过程图
  （drawing → hypothesis → rebuild → semantic-ir → validation-gate → complete-tower），
  本地没有对应的"可 fork 包 + 叙事"整理。

## 5. 已持平或领先的部分

- **三段式管线叙事逐字对齐官网**（README 明确写"对齐仝心圆官网"）。
- **证据链机制比官网深**：官网只有"可核验/可追溯"口号；我们有依赖 DAG、
  变更传播（invalidate 自动作废下游）、strict 导出阻断、SourceRef 全覆盖。
- **比例尺 DIMENSION 自动标定**（scale_calibration.py，禁 GT 反推）——官网未展示此能力。
- **评测体系**：benchmark/mllm_vs_scan.py 三列评测 + acceptance.sh 门禁，
  对应官网"DATA & EVALUATION 可靠评测"承诺。
- 交付物齐全：GLB/OBJ/report.md/steps.json/harness_summary.json。

## 6. 附带发现：官网展示资产版本落后

官网 showcase 的 GLB 与本地最新交付**大小不一致**：

| 资产 | 官网 | 本地最新 |
|---|---|---|
| complete-tower-head.glb | 183,944 B | dxf_deliver/tower.glb = 191,644 B |
| tower-assembly-1-6-s1602.glb | 544,880 B | dxf_deliver/assembly.glb = 659,012 B |

命名 1:1 对应（塔头 / M1-M6 装配），是早期版本产物。
→ **官网展示的是旧模型**；若几何修复后，官网资产需要同步更新。
（注：无法从本地确证官网 GLB 的确切出处，仅按命名与体积推断。）

## 7. 建议行动顺序

1. **P2 修横隔 z 错位**（对应官网"几何求解验证"卖点）—— UNIMPLEMENTED_PLAN §P2
2. **D1/D2/D3 裁决**（评测口径 / 横隔语义 / GT 注入边界）—— 需用户拍板
3. **D4 底段缺口**：接受缺口 or 另寻图册（本册已穷尽无源）
4. **开源发布**：把 SKILL.md + 架构文档 + showcase 整理成可 fork 公开仓，兑现官网承诺
5. **官网资产同步**：几何修复后重导 GLB，替换 showcase 旧版本
6. （可选）若目标是自建品牌站：官网仓库即现成模板（React+TS+Vite+hash 路由），
   文案数据结构（services/pilotSteps/customerGroups 等常量数组）改一处即生效

---

## 附：本次核实的关键事实

- 官网整站在 `src/App.tsx`（32KB）单文件，5 页复用同一批常量数组（services/
  customerGroups/pilotSteps/evidenceItems/researchAreas/jobs），改文案只需改一处。
- 官网无轮播组件（首页证据条是静态 4 卡片）；有汉堡菜单、岗位手风琴、动态 SEO。
- 3D 查看器是 lazy+Suspense 懒加载；three.js 已本地化 vendor（不依赖 CDN）。
- 本地 `web/` 仅 3 个前端文件（index.html 4.4KB / app.js 29KB / styles.css 4.3KB）+ server.py。
- 本地 git remote：`https://github.com/ZSN12/zotero.git`（非官网仓库、非开源发布仓）。
