---
name: engineering-traceability
description: >
  把工程图纸（扫描图、PDF、DWG、DXF）转换为可追溯、可验证、可变更管理的
  工程上下文。适用于 P&ID、铁塔结构图、电气单线图等需要「来源追溯 + 规则验证
  + 变更传播」的场景。不要直接给出"看起来对"的答案，而是产出带来源和状态
  的结构化对象。
version: 0.2.0
---

# Engineering Traceability Skill

## 核心理念

每个工程对象必须回答四个问题：

1. **来自哪张图？** → `source.reference` + `source.detail`
2. **实测还是猜的？** → `Dimension.origin`（measured / assumed / derived / placeholder）
3. **哪些规则验证过？** → `Rule.status` + `Connection.validation_status`
4. **改了之后哪些作废？** → 依赖 DAG + `staleness`（current / stale）

## 三阶段工作流

- DRAWING INTAKE：多源图纸接入，建立 SourceRef
- ENGINEERING COMPILATION：构件/尺寸/连接 + BOM 交叉核验
- VERIFIED DELIVERY：Agent Harness 验证 + 可信导出（JSON/OBJ/GLB/report）

## 铁塔主路径命令

```bash
# 一步全链：intake → compile → cross_check → verify → retry → export
python -m traceability.cli run-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv --merge \
  --golden examples/tower_110kv_golden.json --out-dir out/tower

# 一键交付包
python -m traceability.cli deliver-tower examples/tower_110kv.dxf \
  --bom examples/tower_110kv_bom.csv --merge --out-dir out/delivery

# 扫描图候选（人工复核队列）
python -m traceability.cli intake-scan examples/clear/tower_front_hd.png --tower --out scan.json
```

## AI 工作时的硬性要求

1. 禁止凭空编造工程值：每个 Dimension 必须带 origin 和 source。
2. 禁止悄悄改数据：交叉核验冲突 → 新建 pending 项，不覆盖原值。
3. 改动必须传播：改了节点调用 invalidate 标记下游 stale。
4. 交付前必须验证：pending 规则/连接走 Harness 后才能标 passed。
5. 置信度分级：confidence < 0.7 的对象醒目标注「低置信度，需人工复核」。
6. 扫描图产出默认 pending_review，人工确认 solve_status=verified 才可 export strict。
