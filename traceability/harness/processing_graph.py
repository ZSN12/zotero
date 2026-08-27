"""Processing Graph 可视化日志（P0-2）。

把多步编排的每一步记录为结构化 StepRecord，并导出 steps.json：
    intake / compile / cross_check / solve / export
每步包含 status / duration / error / detail / input / output。

这是 MRTT Research 页 Task Scaffold 的数据底座：前端可以直接消费
steps.json 画处理图，不必解析零散的 print 日志。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_PASSED = "passed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"


@dataclass
class StepRecord:
    """一个处理步骤的状态日志。"""
    id: str
    name: str
    status: str = STEP_PENDING
    started_at: Optional[float] = None
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    input: Optional[str] = None
    output: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status
        return d


class ProcessingGraph:
    """顺序步骤记录器。"""

    def __init__(self, name: str = "tower-processing"):
        self.name = name
        self.steps: List[StepRecord] = []
        self._current: Optional[StepRecord] = None
        self._t0: Optional[float] = None

    def start(self, step_id: str, name: str, input: Optional[str] = None,
              **detail: Any) -> StepRecord:
        rec = StepRecord(id=step_id, name=name, status=STEP_RUNNING,
                         started_at=time.time(), input=input, detail=detail)
        self.steps.append(rec)
        self._current = rec
        self._t0 = time.time()
        return rec

    def finish(self, output: Optional[str] = None, **detail: Any) -> StepRecord:
        rec = self._current
        if rec is None:
            raise RuntimeError("没有正在运行的步骤")
        rec.status = STEP_PASSED
        rec.duration_ms = round((time.time() - self._t0) * 1000, 2) if self._t0 else None
        rec.output = output
        rec.detail.update(detail)
        self._current = None
        return rec

    def fail(self, error: str, **detail: Any) -> StepRecord:
        rec = self._current
        if rec is None:
            raise RuntimeError("没有正在运行的步骤")
        rec.status = STEP_FAILED
        rec.error = error
        rec.duration_ms = round((time.time() - self._t0) * 1000, 2) if self._t0 else None
        rec.detail.update(detail)
        self._current = None
        return rec

    def pending(self, reason: str, **detail: Any) -> StepRecord:
        """把当前步骤标记为 pending（闸门未过但不构成失败，等待人工/下一轮）。"""
        rec = self._current
        if rec is None:
            raise RuntimeError("没有正在运行的步骤")
        rec.status = STEP_PENDING
        rec.error = reason
        rec.duration_ms = round((time.time() - self._t0) * 1000, 2) if self._t0 else None
        rec.detail.update(detail)
        self._current = None
        return rec

    def skip(self, step_id: str, name: str, reason: str) -> StepRecord:
        rec = StepRecord(id=step_id, name=name, status=STEP_SKIPPED, error=reason)
        self.steps.append(rec)
        return rec

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for rec in self.steps:
            counts[rec.status] = counts.get(rec.status, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "steps": [s.to_dict() for s in self.steps],
            "summary": self.summary(),
        }

    def export_json(self, path: str | Path) -> str:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)


def export_steps_json(graph: ProcessingGraph, path: str | Path) -> str:
    """导出 steps.json（P0-2 验收交付物）。"""
    return graph.export_json(path)
