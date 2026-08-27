"""导出适配器。

阶段 3 VERIFIED DELIVERY 的落地：把验证过的工程模型输出成
下游系统能消费的格式。

    * export_cypher  -> Neo4j Cypher 脚本（数字孪生图数据库）
    * export_gexf    -> GEXF 图文件（Gephi / 图分析）
    * export_report  -> 文本交付报告
"""

from __future__ import annotations

from pathlib import Path

from ..model import EngineeringModel, Staleness, ValidationStatus


def export_cypher(model: EngineeringModel, path: str | Path | None = None) -> str:
    """生成 Neo4j Cypher 脚本，重建构件/尺寸/连接/规则及依赖关系。"""
    lines = ["// 工程模型 -> Neo4j（由 engineering-trace 导出）", ""]

    for c in model.components.values():
        lines.append(
            f"MERGE (n:Component {{id: '{c.id}'}}) "
            f"SET n.name = '{c.name}', n.kind = '{c.kind}'"
            f", n.source = '{_safe(c.source.reference if c.source else '')}'"
        )
    for d in model.dimensions.values():
        lines.append(
            f"MERGE (n:Dimension {{id: '{d.id}'}}) "
            f"SET n.value = '{d.value}', n.unit = '{d.unit}', n.origin = '{d.origin.value}'"
        )
    for conn in model.connections.values():
        lines.append(
            f"MERGE (n:Connection {{id: '{conn.id}'}}) "
            f"SET n.type = '{conn.connection_type}', n.validation = '{conn.validation_status.value}'"
        )
    for r in model.rules.values():
        lines.append(
            f"MERGE (n:Rule {{id: '{r.id}'}}) "
            f"SET n.name = '{r.name}', n.status = '{r.status.value}'"
        )

    for conn in model.connections.values():
        lines.append(
            f"MATCH (a {{id: '{conn.from_component}'}}), (b {{id: '{conn.to_component}'}}) "
            f"MERGE (a)-[:CONNECTS_TO]->(b)"
        )
    for node, upstreams in model.dependencies.items():
        for up in upstreams:
            lines.append(
                f"MATCH (a {{id: '{up}'}}), (b {{id: '{node}'}}) "
                f"MERGE (a)-[:DEPENDS_ON]->(b)"
            )
    for node, st in model.staleness.items():
        if st == Staleness.STALE:
            lines.append(f"MATCH (n {{id: '{node}'}}) SET n.stale = true")

    content = "\n".join(lines) + "\n"
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
    return content


def export_gexf(model: EngineeringModel, path: str | Path | None = None) -> str:
    """生成 GEXF 图文件（节点带状态属性，便于图分析）。"""
    nodes = model.all_nodes()
    edges: list[tuple[str, str, str]] = []
    for node, upstreams in model.dependencies.items():
        for up in upstreams:
            edges.append((up, node, "DEPENDS_ON"))
    for conn in model.connections.values():
        edges.append((conn.from_component, conn.to_component, "CONNECTS_TO"))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gexf xmlns="http://www.gexf.net/1.3" version="1.3">',
        '  <graph mode="static" defaultedgetype="directed">',
        '    <nodes>',
    ]
    for n in sorted(nodes):
        st = model.staleness.get(n, Staleness.CURRENT).value
        lines.append(f'      <node id="{n}" label="{n}"><attvalues>'
                     f'<attvalue for="0" value="{st}"/></attvalues></node>')
    lines.append('    </nodes>')
    lines.append('    <edges>')
    for i, (src, dst, kind) in enumerate(edges):
        lines.append(f'      <edge id="{i}" source="{src}" target="{dst}" label="{kind}"/>')
    lines.append('    </edges>')
    lines.append('  </graph>')
    lines.append('</gexf>')

    content = "\n".join(lines) + "\n"
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
    return content


def export_report(model: EngineeringModel, path: str | Path | None = None) -> str:
    """生成交付报告：验证状态 + 低置信度清单 + 失效清单。"""
    lines = [f"工程交付报告：{model.name} (v{model.version})", ""]

    lines.append("## 验证状态")
    for r in model.rules.values():
        lines.append(f"- {r.id} [{r.status.value}] {r.message or ''}")

    lines.append("")
    lines.append("## 低置信度对象（需人工复核）")
    low = []
    for c in model.components.values():
        if c.source and c.source.confidence < 0.7:
            low.append(f"构件 {c.id}（{c.source.confidence:.0%}）")
    for d in model.dimensions.values():
        if d.source and d.source.confidence < 0.7:
            low.append(f"尺寸 {d.id}（{d.source.confidence:.0%}）")
    lines.extend(f"- {x}" for x in (low or ["无"]))

    lines.append("")
    lines.append("## 失效清单")
    stale = [n for n, s in model.staleness.items() if s == Staleness.STALE]
    lines.extend(f"- {n}" for n in (sorted(stale) or ["无"]))

    content = "\n".join(lines) + "\n"
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
    return content


def _safe(s: str) -> str:
    return s.replace("'", "\\'")
