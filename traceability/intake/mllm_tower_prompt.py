"""铁塔专用 MLLM Prompt + JSON Schema（P1-1，硬约束版）。

通用 `_MLLM_PROMPT` 只让模型「识别图中对象」，不约束铁塔语义；
铁塔扫描图需要模型输出杆件编号、截面、节点坐标和视图类型，且必须
能通过 `skill/contract.py` 的硬性契约。

硬约束（用户验收口径）：
    * 只允许 kind：tower_bar / tower_node / drawing_view
      （禁止 tower、bolt、gusset_plate 等）
    * tower_bar 必须输出 bar_id 与 from_node / to_node
    * tower_node 必须输出 node_id
    * 坐标只认 x_px/y_px 或 x/y/z（mm）；没有则 null + placeholder，
      禁止只写在 detail 字符串里
    * Schema 策略 A：非法 kind / 缺必须字段 → 丢弃该条 + parse_warnings，
      不整批拒（避免一个 tower 导致 0 产出）

本模块提供：
    * TOWER_MLLM_PROMPT      铁塔专用提示词（语义 + 字段约束）
    * TOWER_MLLM_SCHEMA      输出 JSON Schema（dict，jsonschema 可直接校验）
    * validate_tower_mllm_output(parsed)  结构级校验（整批致命才报 problems）
    * parse_tower_mllm_output(parsed)     按条校验，非法条丢弃 + warnings
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# 只允许这三种铁塔对象；其余 kind 一律丢弃（策略 A，不整批拒）
ALLOWED_TOWER_KINDS = {"tower_bar", "tower_node", "drawing_view"}

TOWER_MLLM_PROMPT = """你是铁塔结构施工图识别器（角钢塔/猫头塔）。请从图中识别铁塔对象，输出 JSON。

上下文：这是铁塔扫描图/渲染图，pixel 坐标，可能包含正立面、侧立面、平面或 BOM 表。
你的输出将进入工程追溯管线：每个对象必须有 source，每个尺寸必须有 origin，
读不到的数值必须为 null 且 origin="placeholder"，绝不猜值。

严格要求：
1. 只输出 JSON，不要任何解释文字。
2. 只允许以下 kind（component.data.kind），禁止 tower、bolt、gusset_plate 等其它类型：
   - tower_bar     杆件：必须给 bar_id（读不到给 UNLABELED_<序号>，不要编真实件号）、
                    from_node、to_node（指向同批 node 的 id）
   - tower_node    节点：必须给 node_id
   - drawing_view  视图区域：给 view_type（front/side/plan）
3. 坐标只写在这四个字段之一：x_px/y_px（pixel）或 x/y/z（mm）。
   坐标读不到就填 null 并写 origin="placeholder"；禁止把坐标只写在 detail 字符串里。
4. 每个对象必须带 source（reference/detail/confidence）；confidence 扫描图 ≤0.6，矢量图 ≤0.9。
5. 尺寸（dimension）必须带 origin：measured/assumed/derived/placeholder。
6. 输出格式示例：
{
  "objects": [
    {"obj_type": "component", "data": {"id": "node_001", "kind": "tower_node",
       "name": "节点", "properties": {"node_id": "N001", "x_px": 100, "y_px": 200,
       "view_type": "front", "solve_status": "pending_review"}},
     "source": {"source_type": "drawing", "reference": "tower.png",
       "detail": "bbox=(...)", "confidence": 0.6}, "confidence": 0.6},
    {"obj_type": "component", "data": {"id": "bar_0001", "kind": "tower_bar",
       "name": "杆件 M0001", "properties": {"bar_id": "M0001", "section": "L100x8",
       "length_px": 320.0, "from_node": "node_001", "to_node": "node_002",
       "view_type": "front", "solve_status": "pending_review"}},
     "source": {"source_type": "drawing", "reference": "tower.png",
       "detail": "line=(...)-(...)", "confidence": 0.6}, "confidence": 0.6}
  ]
}
7. 无法识别的对象不要输出；宁可少，不可错。
"""

# 铁塔 MLLM 输出 JSON Schema（结构级；语义按条校验在 parse_tower_mllm_output 做）
TOWER_MLLM_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["objects"],
    "properties": {
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["obj_type", "data"],
                "properties": {
                    "obj_type": {"type": "string", "enum": ["component", "dimension", "connection", "rule"]},
                    "data": {"type": "object"},
                    "source": {"type": "object"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
            },
        },
    },
}


# --------------------------------------------------------------------------
# 分步 Agent Prompt + 小 Schema（P1 多 Agent 编排）
#
# 与单轮 TOWER_MLLM_PROMPT 的区别：
#     * A1 件号 Agent 只读件号文字，不画杆、不输出 tower/bolt/gusset_plate
#     * A2 几何 Agent 只检线/节点，不挂件号、不读编号
#     * 坐标必须是 JSON 数字字段（x_px/y_px 或 x1..y2），禁止只写 detail
# --------------------------------------------------------------------------

LABEL_AGENT_PROMPT = """你是铁塔图纸「件号读取」Agent。只读取件号文字（杆件编号），
绝不识别杆件、节点、螺栓、材质、截面，也不输出 tower / bolt / gusset_plate 等对象。

输入：一张铁塔视图裁剪图（pixel 坐标）。
输出：labels 数组。每个 label 只包含一件号文字：
{
  "labels": [
    {"text": "M0001", "bar_id": "M0001", "x_px": 120, "y_px": 340, "view": "front"}
  ],
  "note": "无文字"   // 图中确实没有件号文字时才写
}

严格要求：
1. 只输出 JSON，不要任何解释文字。
2. bar_id 是正则化后的件号（M0001 / G01 / 885 这类），不要写材质（Q235）、
   截面（L40X3）、螺栓（M16X40 / 2M16X50）这类非件号文字。
3. x_px / y_px 必须是 JSON 数字（文字中心点）；禁止只写在 detail 字符串里。
   坐标原点：x_px / y_px 是「你看到的这张输入裁剪图（已按最长边缩放后）」左上角为
   (0,0) 的像素坐标，向右为 x 正、向下为 y 正。不要换算到原整图坐标——
   后处理会统一按 crop 缩放倍率与左上角偏移还原到整图（与 _labels_to_full_image 一致）。
4. 读不到坐标或不是件号的文字不要输出；宁可少，不可错。
5. 整图没有任何件号文字时，输出 {"labels": [], "note": "无文字"}。
"""

LABEL_AGENT_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["labels"],
    "properties": {
        "labels": {"type": "array", "items": {"type": "object"}},
        "note": {"type": "string"},
    },
}

GEOM_AGENT_PROMPT = """你是铁塔图纸「几何检测」Agent。只检测线段和节点，不读取件号文字，
不输出 tower / bolt / gusset_plate 等对象，也不给杆件挂任何编号。

输入：一张铁塔视图裁剪图（pixel 坐标）。
输出：bars 数组 + nodes 数组：
{
  "bars": [
    {"bar_uid": "bar_0001", "x1": 100, "y1": 200, "x2": 300, "y2": 400}
  ],
  "nodes": [
    {"node_id": "N001", "x_px": 100, "y_px": 200}
  ]
}

严格要求：
1. 只输出 JSON，不要任何解释文字。
2. bar_uid 是杆件在本次输出里的唯一标识；x1/y1/x2/y2 必须是 JSON 数字。
3. 线段只检测铁塔杆件中心线，不检测尺寸线、图框线、表格线。
4. 斜材（K 形 / X 形交叉的腹杆）是塔身主体，务必完整检测：
   即使两条斜材在图上交叉重叠，也要分别输出，不要合并或漏掉交叉后的任一段。
   斜材通常比主材细（角钢 L40-L63），不要因为线细就跳过。
   注意：斜材是「通长杆」，一根斜材往往跨越多个节间（长达数米）、中间与
   其他斜材多次交叉。交叉点不是端点——不要把一根通长斜材在交叉处断成
   几段输出；每根斜材只输出一次，用它的两个真实端点（塔身主腿上的连接点）
   作为 x1/y1/x2/y2，覆盖整根通长杆。
5. 优先保证召回（尽量全），在不确定时宁可用近似坐标输出、也不要整根漏掉；
   但坐标要落在杆件中心线上，不要凭空编造不存在的杆件。
6. 不要给 bars 加 bar_id 字段——件号由后续 Agent 关联。
"""

GEOM_AGENT_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["bars", "nodes"],
    "properties": {
        "bars": {"type": "array", "items": {"type": "object"}},
        "nodes": {"type": "array", "items": {"type": "object"}},
    },
}


def _jsonschema_validate(parsed: Any, schema: Dict[str, Any]) -> List[str]:
    """结构级 JSON Schema 校验。"""
    import jsonschema

    if parsed is None:
        return ["MLLM 输出为空"]
    if not isinstance(parsed, dict):
        return ["MLLM 输出必须是 JSON 对象"]
    validator = jsonschema.Draft202012Validator(schema)
    problems = []
    for err in validator.iter_errors(parsed):
        where = ".".join(str(p) for p in err.path) or "(root)"
        problems.append(f"{where}: {err.message}")
    return problems


def validate_label_agent_output(parsed: Any) -> List[str]:
    """A1 件号 Agent 结构级校验。"""
    if not isinstance(parsed, dict):
        return ["A1 输出必须是 JSON 对象"]
    if "labels" not in parsed:
        return ["A1 输出缺少 labels 数组"]
    if not isinstance(parsed.get("labels"), list):
        return ["A1 输出 labels 必须是数组"]
    return _jsonschema_validate(parsed, LABEL_AGENT_SCHEMA)


def parse_label_agent_output(parsed: Any) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """按条校验 A1 labels：非法条丢弃 + warning（策略 A），不整批 0 产出。

    返回 (labels, problems, warnings)。coordinates 缺失/非数字、text 缺失的
    条目丢弃并记 warning；bar_id 可缺省（由 A3 用正则从 text 提取）。
    """
    problems = validate_label_agent_output(parsed)
    if problems:
        return [], problems, []

    labels: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for i, item in enumerate(parsed.get("labels", []) or []):
        text = item.get("text")
        x_px = item.get("x_px")
        y_px = item.get("y_px")
        if not isinstance(text, str) or not text.strip():
            warnings.append(f"labels[{i}]: 缺少 text 已丢弃")
            continue
        if not isinstance(x_px, (int, float)) or not isinstance(y_px, (int, float)):
            warnings.append(f"labels[{i}]: 缺少数字 x_px/y_px 已丢弃（detail 字符串坐标不采信）")
            continue
        bar_id = item.get("bar_id")
        labels.append({
            "text": text.strip(),
            "bar_id": bar_id.strip() if isinstance(bar_id, str) and bar_id.strip() else None,
            "x_px": float(x_px),
            "y_px": float(y_px),
            "view": item.get("view"),
        })
    return labels, [], warnings


def validate_geom_agent_output(parsed: Any) -> List[str]:
    """A2 几何 Agent 结构级校验。"""
    if not isinstance(parsed, dict):
        return ["A2 输出必须是 JSON 对象"]
    if "bars" not in parsed:
        return ["A2 输出缺少 bars 数组"]
    if not isinstance(parsed.get("bars"), list):
        return ["A2 输出 bars 必须是数组"]
    if not isinstance(parsed.get("nodes"), list):
        return ["A2 输出 nodes 必须是数组"]
    return _jsonschema_validate(parsed, GEOM_AGENT_SCHEMA)


def parse_geom_agent_output(parsed: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str], List[str]]:
    """按条校验 A2 bars/nodes：非法条丢弃 + warning（策略 A）。

    返回 (bars, nodes, problems, warnings)。
    """
    problems = validate_geom_agent_output(parsed)
    if problems:
        return [], [], problems, []

    bars: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for i, item in enumerate(parsed.get("bars", []) or []):
        vals = [item.get(k) for k in ("bar_uid", "x1", "y1", "x2", "y2")]
        if not isinstance(vals[0], str) or not vals[0].strip():
            warnings.append(f"bars[{i}]: 缺少 bar_uid 已丢弃")
            continue
        if not all(isinstance(v, (int, float)) for v in vals[1:]):
            warnings.append(f"bars[{i}]: 坐标必须是 JSON 数字已丢弃")
            continue
        bars.append({
            "bar_uid": vals[0].strip(),
            "x1": float(vals[1]), "y1": float(vals[2]),
            "x2": float(vals[3]), "y2": float(vals[4]),
        })
    for i, item in enumerate(parsed.get("nodes", []) or []):
        node_id = item.get("node_id")
        x_px, y_px = item.get("x_px"), item.get("y_px")
        if not isinstance(node_id, str) or not node_id.strip():
            warnings.append(f"nodes[{i}]: 缺少 node_id 已丢弃")
            continue
        if not isinstance(x_px, (int, float)) or not isinstance(y_px, (int, float)):
            warnings.append(f"nodes[{i}]: 坐标必须是 JSON 数字已丢弃")
            continue
        nodes.append({"node_id": node_id.strip(), "x_px": float(x_px), "y_px": float(y_px)})
    return bars, nodes, [], warnings


def validate_tower_mllm_output(parsed: Any) -> List[str]:
    """结构级校验：只有整批不可用才算 problems。

    单条语义问题（非法 kind / 缺字段）不在这里报，由 parse_tower_mllm_output
    按策略 A 丢弃并记 warnings，避免一个 tower 对象导致全批 0 产出。
    """
    import jsonschema

    if parsed is None:
        return ["MLLM 输出为空"]
    if not isinstance(parsed, dict):
        return ["MLLM 输出必须是 JSON 对象"]
    if "objects" not in parsed:
        return ["MLLM 输出缺少 objects 数组"]
    if not isinstance(parsed.get("objects"), list):
        return ["MLLM 输出 objects 必须是数组"]

    validator = jsonschema.Draft202012Validator(TOWER_MLLM_SCHEMA)
    problems = []
    for err in validator.iter_errors(parsed):
        where = ".".join(str(p) for p in err.path) or "(root)"
        problems.append(f"{where}: {err.message}")
    return problems


def _coord_present(props: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """检查坐标是否落在 x_px/y_px 或 x/y/z（mm）字段。

    返回 (是否至少有一个坐标字段, 缺失坐标列表)。
    只在 detail 字符串里写坐标不算数（硬约束）。
    """
    axes_px = ["x_px", "y_px"]
    axes_mm = ["x", "y", "z"]
    present = []
    for k in axes_px + axes_mm:
        if props.get(k) is not None:
            present.append(k)
    return bool(present), present


def parse_tower_mllm_output_with_warnings(
    parsed: Any,
) -> Tuple[List[Any], List[str], List[str]]:
    """按条校验 MLLM 输出，返回 (objects, problems, warnings)。

    策略 A：
        * 结构级问题 -> problems（整批不可用，objects=[]）
        * 单条非法 kind -> 丢弃该条 + warning（不整批拒）
        * tower_bar 缺 bar_id/from_node/to_node、tower_node 缺 node_id ->
          保留对象但记 warning（交给 Harness 判 failed，不让一条导致 0 产出）
        * 坐标只写在 detail 字符串 -> 不采信 + warning
    """
    from .mllm_backend import CandidateObject

    problems = validate_tower_mllm_output(parsed)
    if problems:
        return [], problems, []

    objects: List[Any] = []
    warnings: List[str] = []
    for i, item in enumerate(parsed.get("objects", []) or []):
        data = item.get("data", {}) or {}
        props = data.get("properties", {}) or {}

        if item.get("obj_type") == "component":
            kind = data.get("kind")
            # 策略 A：非法 kind 丢弃，不整批拒
            if kind not in ALLOWED_TOWER_KINDS:
                warnings.append(
                    f"objects[{i}]: 非法铁塔 kind '{kind}' 已丢弃"
                    f"（只允许 {sorted(ALLOWED_TOWER_KINDS)}）")
                continue

            if kind == "tower_bar":
                missing = []
                if not props.get("bar_id"):
                    missing.append("bar_id")
                for end in ("from_node", "to_node"):
                    if not props.get(end):
                        missing.append(end)
                if missing:
                    warnings.append(
                        f"objects[{i}]: tower_bar 缺少 {missing}，"
                        f"保留对象但 Harness 可能判 failed")
            elif kind == "tower_node":
                if not props.get("node_id") and not data.get("id"):
                    warnings.append(f"objects[{i}]: tower_node 缺少 node_id")
            elif kind == "drawing_view":
                if not props.get("view_type"):
                    warnings.append(
                        f"objects[{i}]: drawing_view 缺少 view_type，"
                        f"保留对象但视图合并可能跳过该条")

            if kind in ("tower_bar", "tower_node"):
                has_coord, _ = _coord_present(props)
                if not has_coord:
                    warnings.append(
                        f"objects[{i}]: {kind} 无 x_px/y_px 或 x/y/z 坐标，"
                        f"保持 placeholder（detail 字符串坐标不采信）")

        objects.append(CandidateObject(
            obj_type=item.get("obj_type", "component"),
            data=data,
            source=item.get("source"),
            confidence=float(item.get("confidence", 0.6)),
        ))

    return objects, [], warnings


def parse_tower_mllm_output(parsed: Any) -> Tuple[List[Any], List[str]]:
    """兼容旧调用：返回 (objects, problems)。需要 warnings 请用
    parse_tower_mllm_output_with_warnings。"""
    objects, problems, _warnings = parse_tower_mllm_output_with_warnings(parsed)
    return objects, problems
