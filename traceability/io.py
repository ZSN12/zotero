"""JSON 读写与校验 —— 工程模型的持久化层。

多源图纸接入后，所有信息最终汇入一个版本化的 JSON 工程模型。
这个文件负责：
    * 加载 / 保存
    * 基础完整性校验（引用是否存在）
    * 生成可读的追溯报告
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import EngineeringModel, from_dict, to_dict


def save_model(model: EngineeringModel, path: str | Path) -> None:
    """保存模型到 JSON 文件（含版本信息）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(to_dict(model), f, ensure_ascii=False, indent=2)


def load_model(path: str | Path, enforce_schema: bool = False) -> EngineeringModel:
    """从 JSON 文件加载模型。

    enforce_schema=True 时先按 schema/engineering_model.json 校验，
    不合法则抛 ValueError（给出第一条错误）。
    """
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    if enforce_schema:
        problems = _schema_errors(data)
        if problems:
            raise ValueError("JSON Schema 校验失败：\n  - " + "\n  - ".join(problems[:10]))
    return from_dict(data)


def validate_against_schema(model: EngineeringModel) -> list[str]:
    """按 schema/engineering_model.json 校验模型，返回问题列表（空 = 通过）。"""
    return _schema_errors(to_dict(model))


def _schema_errors(data: dict) -> list[str]:
    import jsonschema

    schema_path = Path(__file__).resolve().parents[1] / "schema" / "engineering_model.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    problems = []
    for err in validator.iter_errors(data):
        where = ".".join(str(p) for p in err.path) or "(root)"
        problems.append(f"{where}: {err.message}")
    return problems


def _is_dxf_handle(s: str) -> bool:
    """判断一个字符串是否像 DXF 实体 handle（十六进制）。

    DXF handle 是纯十六进制字符串（如 '2F'、'A1B2'、'101'、'0x2F'），是外部实体
    句柄而非模型组件 ID，因此 validate_references 不应要求其在 components 内可解析。

    本代码库的组件 ID（bar_M0051_front、node_A、4f_..._F、drawing_file、bom_row）
    必含非十六进制字符（下划线/连字符/超出 a-f 的字母），因此「纯十六进制串」是
    与组件 ID 不相交的可靠判据——含纯数字（如 '101'）也算 handle（'101' 是合法
    十六进制）。
    """
    s = s.strip()
    if not s:
        return False
    if s.startswith("0x") or s.startswith("0X"):
        body = s[2:]
        return bool(body) and all(ch in "0123456789abcdefABCDEF" for ch in body)
    # 纯十六进制串（含纯数字）：与组件 ID 不相交（组件 ID 必含非十六进制字符）
    return all(ch in "0123456789abcdefABCDEF" for ch in s)


def validate_references(model: EngineeringModel) -> list[str]:
    """检查跨对象引用是否完整，返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    comp_ids = set(model.components)

    for dim in model.dimensions.values():
        if dim.applies_to and dim.applies_to not in comp_ids:
            problems.append(f"dimension '{dim.id}' 引用了不存在的构件 '{dim.applies_to}'")

    for conn in model.connections.values():
        if conn.from_component not in comp_ids:
            problems.append(f"connection '{conn.id}' 的 from_component '{conn.from_component}' 不存在")
        if conn.to_component not in comp_ids:
            problems.append(f"connection '{conn.id}' 的 to_component '{conn.to_component}' 不存在")
        for rid in conn.rule_ids:
            if rid not in model.rules:
                problems.append(f"connection '{conn.id}' 引用了不存在的规则 '{rid}'")

    for rule in model.rules.values():
        for cid in rule.applies_to:
            if cid not in comp_ids and cid not in model.connections:
                problems.append(f"rule '{rule.id}' 引用了不存在的对象 '{cid}'")

    # 阶段 4.6：杆件证据链引用完整性
    #   * from_node / to_node 必须指向存在的 tower_node
    #   * derived_from 指向的原始组件若不存在，属于悬空引用（追溯链断裂）
    for cid, comp in model.components.items():
        if comp.kind != "tower_bar":
            continue
        p = comp.properties or {}
        for end in ("from_node", "to_node"):
            nid = p.get(end)
            if nid and nid not in comp_ids:
                problems.append(f"杆件 '{cid}' 的 {end} '{nid}' 不存在")
        derived = p.get("derived_from")
        if derived and derived not in comp_ids:
            # 阶段 4.3 语义：仅 reconstructed（mirrored）杆件的 derived_from
            # 必须指向组件内存在的 front 物理杆件。recognized（源头，指向外部
            # DXF 二维构件）与 derived（纯几何派生，指向对称展开操作）的
            # derived_from 是外部/操作引用，不要求组件内可解析。
            if p.get("geometry_class") == "reconstructed":
                problems.append(f"杆件 '{cid}' 的 derived_from '{derived}' 悬空（mirrored 杆件必须指向 front 物理杆件）")
        # 阶段 4.3：扩展校验 projection_refs 中的组件引用。
        # source_component_id 若指向组件内 ID 则必须存在；但 DXF 实体 handle /
        # 稳定 URI（sheet://...）是外部自包含引用，不要求组件内可解析。
        for ref in (p.get("projection_refs") or []) or []:
            if not isinstance(ref, dict):
                continue
            scid = ref.get("source_component_id")
            if not scid:
                continue
            # 跳过外部稳定引用（handle 十六进制 / sheet:// URI）
            s = str(scid)
            if s.startswith("sheet://"):
                continue
            # DXF entity handle 是十六进制字符串（如 '2F'、'0x2F'、'A1B2'），
            # 属外部实体句柄，非组件内 ID，不要求可解析。
            if _is_dxf_handle(s):
                continue
            # 否则视为组件内引用，必须可解析
            if s not in comp_ids:
                problems.append(f"杆件 '{cid}' 的 projection_refs.source_component_id '{s}' 悬空")

    for node, upstreams in model.dependencies.items():
        all_nodes = model.all_nodes()
        if node not in all_nodes:
            problems.append(f"依赖图节点 '{node}' 不是任何已知对象")
        for up in upstreams:
            if up not in all_nodes:
                problems.append(f"依赖 '{node} -> {up}' 中的上游 '{up}' 不是任何已知对象")

    return problems


def render_report(model: EngineeringModel) -> str:
    """生成人类可读的工程模型追溯报告。"""
    lines: list[str] = []
    lines.append(f"# 工程模型：{model.name} (v{model.version})")
    lines.append("")
    lines.append(f"构件 {len(model.components)} 个 / 尺寸 {len(model.dimensions)} 个 "
                 f"/ 连接 {len(model.connections)} 个 / 规则 {len(model.rules)} 条")
    lines.append("")

    lines.append("## 尺寸来源分级")
    for d in model.dimensions.values():
        src = d.source.reference if d.source else "-"
        conf = f"{d.source.confidence:.0%}" if d.source else "-"
        lines.append(f"- {d.id}: {d.value}{d.unit}  [{d.origin.value}] 来源={src} 置信度={conf} 状态={d.status.value}")

    lines.append("")
    lines.append("## 连接验证")
    for c in model.connections.values():
        lines.append(f"- {c.id}: {c.from_component} -> {c.to_component} "
                     f"[{c.connection_type}] 验证={c.validation_status.value} 规则={','.join(c.rule_ids) or '-'}")

    lines.append("")
    lines.append("## 规则")
    for r in model.rules.values():
        lines.append(f"- {r.id}: {r.name} -> {r.status.value}" + (f" ({r.message})" if r.message else ""))

    lines.append("")
    lines.append("## 当前失效清单（改动后需重算/重验）")
    from .graph import stale_report
    report = stale_report(model)
    if not report:
        lines.append("- 无，全部为 current")
    else:
        for kind, nodes in report.items():
            lines.append(f"- {kind}: {', '.join(sorted(nodes))}")

    return "\n".join(lines)
