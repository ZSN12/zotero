"""依赖图与变更传播引擎。

回答三个核心工程问题：
    1. 这个对象依赖谁？          -> ancestors()
    2. 改了它会作废谁？          -> descendants() / invalidate()
    3. 哪些验证/尺寸需要重做？   -> stale_report()
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .model import EngineeringModel, Staleness


def ancestors(model: EngineeringModel, node: str) -> set[str]:
    """返回 node 的所有上游依赖（递归）。"""
    seen: set[str] = set()
    stack = list(model.dependencies.get(node, set()))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(model.dependencies.get(cur, set()))
    return seen


def descendants(model: EngineeringModel, node: str) -> set[str]:
    """返回 node 的所有下游（递归）。"""
    seen: set[str] = set()
    stack = list(model.downstream_of(node))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(model.downstream_of(cur))
    return seen


def affected_by(model: EngineeringModel, changed: Iterable[str]) -> set[str]:
    """计算改动 changed 节点后所有应作废的节点（含 changed 本身）。"""
    result = set(changed)
    for node in changed:
        result |= descendants(model, node)
    return result


def invalidate(model: EngineeringModel, changed: Iterable[str]) -> set[str]:
    """标记作废，返回被标记为 STALE 的节点集合。"""
    stale = model.invalidate(set(changed))
    return stale


def stale_report(model: EngineeringModel) -> dict[str, list[str]]:
    """按类型汇总当前所有 STALE 节点，方便生成「待重算/重验清单」。"""
    report: dict[str, list[str]] = defaultdict(list)
    for node, st in model.staleness.items():
        if st == Staleness.STALE:
            if node in model.components:
                report["components"].append(node)
            elif node in model.dimensions:
                report["dimensions"].append(node)
            elif node in model.connections:
                report["connections"].append(node)
            elif node in model.rules:
                report["rules"].append(node)
            else:
                report["unknown"].append(node)
    return dict(report)
