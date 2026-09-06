# Phase 2b 图纸意图四分类：MLLM 主判 + 确定性复核链（2026-09-04）

> 模块：`traceability/intake/sheet_intent.py`（Phase 2a/2b 交付物）
> 测试：`tests/test_sheet_intent.py`（19 用例）
> 结果：**JC1+ZC1 全部 16 张分册功能命中 16/16（0 BAD），精确命中 11/16**（5 张 front↔side 标签互换，接线语义等价——都映射 sheet_role=elevation）

## 1. 定位与铁律对齐

Phase 2 目标：消灭 per-stem 手工 overlay（意图部分）。本模块只产出
「这张图是什么视图类型」的**意图标签 + 置信度 + 留痕**，不产任何 3D
坐标——坐标仍由确定性求解器（`_infer_assembly_views` 区域推断 +
DIMENSION 比例标定 + z_offset 塔级路由）产出。MLLM 的角色严格限定在
「图纸意图分类」（铁律允许的三类 MLLM 用途之一）。

四类：`assembly_elevation_front` / `assembly_elevation_side` /
`fabrication_detail` / `plan_projection`；意图→接线角色映射
`INTENT_TO_SHEET_ROLE`：front/side→elevation、detail→node_detail、
plan→plan。

## 2. 判据链（按执行顺序）

```
文件名规则出局（-00-*/-ML，零成本，不经 MLLM）
  → MLLM 视觉判图（主判据；结构簇裁剪渲染，verdict 按 DXF hash 缓存）
      ├─ 表格指纹复核：n_text>500 且 text/line>1.5 → fabrication_detail
      │    （JC1-01-2 材料表 text/line≈6.3，真立面页 0.3~0.9）
      ├─ 双线角钢门：MLLM 判立面但短碎线占比 <0.10 → 降级 detail
      │    （JC1-01-1 多呼高单线图 dbl≈0.02，真立面 0.36~0.72）
      ├─ 缩微模型门：MLLM 判立面但塔形簇跨度 <35% 图册参照 → 降级 detail
      │    （JC1-03 节点大样被 MLLM 看成塔身；跨度仅 24%）
      └─ 立面反证：MLLM 判非立面但 dbl≥0.30+跨度≥50%+主簇≥100 线
           → 改判 elevation（MLLM run 间波动漏判 06/07/10/12 的兜底）
  → MLLM 不可用/异常 → 几何启发式兜底（aspect/beats/相对跨度）
```

所有复核阈值从 16 张实测分布推出，无 per-stem 特调。

## 3. 关键工程判据（负结论驱动）

| 判据 | 数值依据（16 张实测） | 解决的失败模式 |
|---|---|---|
| 结构簇裁剪（多分量并集） | 显著分量=线数≥30%最大簇（≤6 个）的 bbox 并集 | 整页渲染塔成图钉（MLLM 误判）；单簇裁剪丢多视图页（ZC1-10 三视图并排） |
| 端点吸附 tol=4 | tol=8 时 JC1-07 塔段+右侧大样粘连成 422×331 一坨 | 粘连后 crop 一半塔一半大样，MLLM 判 detail |
| 不炸 INSERT 取结构线 | GWTKA1 图框块 806×574 | 炸开后图框/图签线污染连通分量与跨度统计 |
| 表格指纹 text/line>1.5 & n_text>500 | 01-2=6.3 vs 立面页 0.3~0.9 | BOM 数字矩阵里 MLLM 看不出结构 |
| 双线角钢指纹（短碎线占比，尺度归一 span<1%主簇） | 真立面 0.36~0.72；01-1 单线图 0.02；**03 大样 0.42（重叠！）** | 单线示意图骗过 MLLM 视觉；**不能单独定立面**（与大样重叠） |
| 塔形簇跨度（显著簇 aspect 带通 [0.9,4.5]） | 真立面 rel 0.52~1.00；03 大样=0；01-2 表格=0 | 节点大样放大后「像塔身」+ 双线画法 → 唯一干净分离判据是同图册相对尺度 |

**核心负结论**：双线指纹与跨度必须**绑定**使用——单独任何一个都会
被某种版式攻破（01-1 攻破「有塔形就判立面」，03 攻破「双线就是立面」）。
确定性证据链的价值不在「替代 MLLM」而在「不波动」：MLLM 对同一张图
的判定在 run 间漂移（06/07/10/12 均观测到），确定性判据是稳定锚。

## 4. MLLM 供应商现状

- Kimi key（.env/.zshrc）双失效（401）。
- 在用：本地 opencodex 中继 `http://127.0.0.1:10100`（glm-relay preset，
  model `workbuddy/glm-5.3`，视觉可用）。
- 备选不可用：gemini-3.7-flash（502 未声明工具）、cursor/glm-5.2（429）、
  gpt-5.5（429）。
- 渲染缓存 + verdict 缓存均在 `out/sheet_intent/`（gitignored，按 DXF
  content hash 失效）。

## 5. 16 张验证矩阵（overlay view_regions 为真值）

| 分册 | 预测 | 真值 | 命中 |
|---|---|---|---|
| JC1-00-1/00-2 | fabrication_detail（文件名出局） | detail | OK |
| JC1-01-1 | fabrication_detail（双线门拦 MLLM front 误判） | detail | OK |
| JC1-01-2 | fabrication_detail（表格指纹） | detail | OK |
| JC1-02 | front | front+side | fn（等价） |
| JC1-03 | fabrication_detail（缩微门） | detail | OK |
| JC1-04 | front | front | OK |
| JC1-05 | front | front | OK |
| JC1-06 | front | front | OK |
| JC1-07 | front | front | OK |
| ZC1-05 | side | front+side | OK |
| ZC1-07 | side | front | fn（等价） |
| ZC1-08 | side | front+side | OK |
| ZC1-09 | front | front+side | fn（等价） |
| ZC1-10 | front | front+side | fn（等价） |
| ZC1-12 | front（立面反证） | front+side | fn（等价） |

fn 的处理：front/side 标签由 `_infer_assembly_views` 的 x 中位切分在
region 层解决（同册双塔自动切 front+side），意图层只需「是否立面源」
这一比特——16/16 正确。

## 6. 下一步（Phase 2c 接线）

意图标签 → 管线选择的接线点（overlay 缺省时生效）：
1. `sheet_is_spatial_mergeable`：intent∈{front,side} → 可合并源；
2. `cross_file_merge_stems` 合法 stem 认定：elevation 意图才进合并池；
3. `resolve_drawing_kind` 的 role 补充 intent 覆盖（detail/plan 分流）；
4. z_offset 塔级路由（cross_file_views.sheets）保持 overlay/人工通道
   （图纸内 z 歧义，ZC1 bands 重叠实测不可从图纸自证）。

Phase 2e 验收（goal 原文）：剥离 per-stem 意图后 JC1/ZC1 端到端跑通
且红线不回退（TP 913/dual-full 1067/dual-pure 304/A1 168/union 1069/
pytest 全绿）。
