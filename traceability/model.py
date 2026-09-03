"""工程对象数据模型。

核心理念：工程制图中的每一个对象都不是「画出来的像素/线条」，
而是一个带唯一 ID、来源、置信度和验证状态的数据实体。
只有数据实体之间有关联（依赖 DAG），才谈得上「追溯」和「变更作废」。

对象类型：
    * Component   构件（泵、阀门、管道、设备……）
    * Dimension   尺寸（实测 / 假设 / 派生 / 占位）
    * Connection  连接（两个构件之间的物理/逻辑连接）
    * Rule        规则（设计规范 / 连接约束，可被执行并记录结果）
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class SourceType(str, Enum):
    """数据的来源类型 —— 回答「来自哪张图」。"""
    DRAWING = "drawing"              # 从某张图纸提取
    MEASUREMENT = "measurement"      # 现场实测
    ASSUMPTION = "assumption"        # 工程师假设
    DERIVED = "derived"              # 由其他数据计算得出
    VENDOR = "vendor"                # 供应商数据
    UNKNOWN = "unknown"


class DimensionOrigin(str, Enum):
    """尺寸的来源分级 —— 回答「实测还是猜的」。"""
    MEASURED = "measured"      # 实测
    ASSUMED = "assumed"        # 假设/估算
    DERIVED = "derived"        # 派生计算
    PLACEHOLDER = "placeholder"  # 占位，等待补测


class ValidationStatus(str, Enum):
    """验证状态 —— 回答「验证过没有」。"""
    PENDING = "pending"        # 待验证
    PASSED = "passed"          # 验证通过
    FAILED = "failed"          # 验证失败
    NOT_APPLICABLE = "not_applicable"


class Staleness(str, Enum):
    """失效状态 —— 回答「改了之后哪些作废」。"""
    CURRENT = "current"        # 当前有效
    STALE = "stale"            # 上游变更，本对象待重算/重验


@dataclass
class SourceRef:
    """来源引用：追溯到某张图、某个坐标、某次提取。"""
    source_type: SourceType
    reference: str                      # 图纸号 / 点云号 / 报告号
    detail: Optional[str] = None        # 图内定位，如 "Sheet A-102, View 3"
    confidence: float = 1.0             # 提取置信度 0.0~1.0
    extracted_at: Optional[str] = None  # 提取时间戳
    extracted_by: Optional[str] = None  # 工具或人


@dataclass
class Component:
    """一个构件：泵、阀门、管道、设备……"""
    id: str
    name: str
    kind: str                              # pump / valve / pipe / vessel / ...
    source: Optional[SourceRef] = None
    properties: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass
class Dimension:
    """一个尺寸/属性值，必须声明「实测还是猜的」。"""
    id: str
    name: str
    value: Any
    unit: str
    origin: DimensionOrigin = DimensionOrigin.PLACEHOLDER
    source: Optional[SourceRef] = None
    applies_to: Optional[str] = None       # 所属构件 ID
    status: ValidationStatus = ValidationStatus.PENDING


@dataclass
class Connection:
    """两个构件之间的连接，附验证状态和规则引用。"""
    id: str
    from_component: str
    to_component: str
    connection_type: str = "physical"      # physical / logical / flow / signal
    source: Optional[SourceRef] = None
    validation_status: ValidationStatus = ValidationStatus.PENDING
    rule_ids: list[str] = field(default_factory=list)
    validated_at: Optional[str] = None


@dataclass
class Rule:
    """一条设计/连接规则，可执行、可记录验证结果。"""
    id: str
    name: str
    description: str
    applies_to: list[str] = field(default_factory=list)  # 适用的连接/构件 ID
    status: ValidationStatus = ValidationStatus.PENDING
    message: Optional[str] = None


@dataclass
class EngineeringModel:
    """完整工程模型 = 构件 + 尺寸 + 连接 + 规则。"""
    name: str
    version: str = "1"
    components: dict[str, Component] = field(default_factory=dict)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    connections: dict[str, Connection] = field(default_factory=dict)
    rules: dict[str, Rule] = field(default_factory=dict)
    # 依赖图：节点 -> 它依赖的上游节点集合。
    # 例如 dimension 依赖 component，rule 依赖 connection，派生尺寸依赖其他尺寸。
    dependencies: dict[str, set[str]] = field(default_factory=dict)
    # 失效状态：节点 -> CURRENT/STALE
    staleness: dict[str, Staleness] = field(default_factory=dict)

    # ---- 构造辅助 ----
    def add_component(self, c: Component) -> "EngineeringModel":
        self.components[c.id] = c
        self.staleness.setdefault(c.id, Staleness.CURRENT)
        return self

    def add_dimension(self, d: Dimension) -> "EngineeringModel":
        self.dimensions[d.id] = d
        self.staleness.setdefault(d.id, Staleness.CURRENT)
        return self

    def add_connection(self, c: Connection) -> "EngineeringModel":
        self.connections[c.id] = c
        self.staleness.setdefault(c.id, Staleness.CURRENT)
        return self

    def add_rule(self, r: Rule) -> "EngineeringModel":
        self.rules[r.id] = r
        self.staleness.setdefault(r.id, Staleness.CURRENT)
        return self

    def depend(self, node: str, *upstream: str) -> "EngineeringModel":
        """声明 node 依赖 upstream 列表（node 是下游，upstream 是上游）。"""
        self.dependencies.setdefault(node, set()).update(upstream)
        return self

    # ---- 查询 ----
    def all_nodes(self) -> set[str]:
        nodes = set(self.components)
        nodes |= set(self.dimensions)
        nodes |= set(self.connections)
        nodes |= set(self.rules)
        return nodes

    def downstream_of(self, node: str) -> set[str]:
        """返回 node 的所有直接下游。"""
        result = set()
        for downstream, upstreams in self.dependencies.items():
            if node in upstreams:
                result.add(downstream)
        return result

    def invalidate(self, changed: set[str]) -> set[str]:
        """改动了 changed 中的节点后，沿依赖图传播，标记下游为 STALE。

        返回所有被标记为 STALE 的节点集合（含 changed 本身）。
        """
        stale = set(changed)
        frontier = set(changed)
        while frontier:
            nxt = set()
            for node in frontier:
                for downstream in self.downstream_of(node):
                    if downstream not in stale:
                        stale.add(downstream)
                        nxt.add(downstream)
            frontier = nxt
        for n in stale:
            self.staleness[n] = Staleness.STALE
        return stale

    def refresh(self, nodes: set[str]) -> None:
        """重算/重验后，把指定节点及其未受影响的上游恢复 CURRENT。"""
        for n in nodes:
            self.staleness[n] = Staleness.CURRENT


def to_dict(model: EngineeringModel) -> dict[str, Any]:
    """序列化为 JSON 友好结构。

    P2 门禁对齐（2026-09-03）：source=None 的组件（生成器输出节点等）
    序列化时省略 source 键而非写 null——schema 的 sourceRef 是对象类型，
    "source": null 违反 schema（validate_public_ir 门禁会拦）。
    """
    def _comp_dict(v) -> dict[str, Any]:
        d = asdict(v)
        if d.get("source") is None:
            d.pop("source", None)
        return d

    return {
        "name": model.name,
        "version": model.version,
        "components": {k: _comp_dict(v) for k, v in model.components.items()},
        "dimensions": {k: asdict(v) for k, v in model.dimensions.items()},
        "connections": {k: asdict(v) for k, v in model.connections.items()},
        "rules": {k: asdict(v) for k, v in model.rules.items()},
        "dependencies": {k: sorted(v) for k, v in model.dependencies.items()},
        "staleness": {k: v.value for k, v in model.staleness.items()},
    }


def from_dict(data: dict[str, Any]) -> EngineeringModel:
    """从 JSON 结构反序列化。"""
    model = EngineeringModel(name=data["name"], version=str(data.get("version", "1")))

    for cid, c in data.get("components", {}).items():
        model.add_component(Component(
            id=c["id"], name=c["name"], kind=c["kind"],
            source=_source_from(c.get("source")),
            properties=c.get("properties", {}),
            tags=c.get("tags", []),
        ))

    for did, d in data.get("dimensions", {}).items():
        model.add_dimension(Dimension(
            id=d["id"], name=d["name"], value=d["value"], unit=d["unit"],
            origin=DimensionOrigin(d.get("origin", "placeholder")),
            source=_source_from(d.get("source")),
            applies_to=d.get("applies_to"),
            status=ValidationStatus(d.get("status", "pending")),
        ))

    for cid, c in data.get("connections", {}).items():
        model.add_connection(Connection(
            id=c["id"],
            from_component=c["from_component"],
            to_component=c["to_component"],
            connection_type=c.get("connection_type", "physical"),
            source=_source_from(c.get("source")),
            validation_status=ValidationStatus(c.get("validation_status", "pending")),
            rule_ids=c.get("rule_ids", []),
            validated_at=c.get("validated_at"),
        ))

    for rid, r in data.get("rules", {}).items():
        model.add_rule(Rule(
            id=r["id"], name=r["name"], description=r["description"],
            applies_to=r.get("applies_to", []),
            status=ValidationStatus(r.get("status", "pending")),
            message=r.get("message"),
        ))

    for node, upstream in data.get("dependencies", {}).items():
        model.depend(node, *upstream)

    for node, st in data.get("staleness", {}).items():
        model.staleness[node] = Staleness(st)

    return model


def _source_from(d: Optional[dict]) -> Optional[SourceRef]:
    if not d:
        return None
    return SourceRef(
        source_type=SourceType(d["source_type"]),
        reference=d["reference"],
        detail=d.get("detail"),
        confidence=float(d.get("confidence", 1.0)),
        extracted_at=d.get("extracted_at"),
        extracted_by=d.get("extracted_by"),
    )
