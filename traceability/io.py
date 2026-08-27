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
