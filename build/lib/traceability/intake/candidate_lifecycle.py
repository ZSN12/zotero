# -*- coding: utf-8 -*-
"""P2.4 / Phase 6：MLLM 候选生命周期——accepted/rejected 审计 + review 队列条目。

MLLM keep/drop（centerline 分类）与 P2.5 置信度门拒候选均经此模块落盘，
供 drawing_file.properties 与 review_queue.json 消费。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .mllm_candidate_protocol import CandidateRecord

LIFECYCLE_VERSION = 1


@dataclass
class LifecycleEntry:
    """单条 rejected 候选（不入模，仅审计 + 人工 review）。"""

    bar_uid: str
    reason: str
    source: str
    sheet_stem: str = ""
    confidence: Optional[float] = None
    stratium: Optional[str] = None
    cross_source: Optional[str] = None
    segment_px: Optional[List[float]] = None
    component_id: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class LifecycleBlock:
    """单张 sheet 一次几何步骤的生命周期摘要。"""

    sheet_stem: str
    geom_method: str
    n_candidates_in: int = 0
    n_accepted: int = 0
    n_rejected: int = 0
    rejected: List[LifecycleEntry] = field(default_factory=list)
    audit: Dict[str, Any] = field(default_factory=dict)
    protocol_version: int = LIFECYCLE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "sheet_stem": self.sheet_stem,
            "geom_method": self.geom_method,
            "n_candidates_in": self.n_candidates_in,
            "n_accepted": self.n_accepted,
            "n_rejected": self.n_rejected,
            "rejected": [r.to_dict() for r in self.rejected],
            "audit": self.audit,
        }


def entries_from_confidence_rejects(
    records: Sequence[CandidateRecord],
    *,
    sheet_stem: str,
) -> List[LifecycleEntry]:
    """P2.5：低置信 MLLM 候选 → lifecycle 条目。"""
    out: List[LifecycleEntry] = []
    for r in records:
        out.append(LifecycleEntry(
            bar_uid=r.bar_uid,
            reason="low_confidence",
            source="mllm_confidence_gate",
            sheet_stem=sheet_stem,
            confidence=r.confidence_effective,
            stratium=r.stratium,
            cross_source=r.cross_source,
            segment_px=[r.x1, r.y1, r.x2, r.y2],
            detail=f"stratium={r.stratium} cross={r.cross_source}",
        ))
    return out


def entries_from_centerline_drops(
    candidates: Sequence[Dict[str, Any]],
    keep_ids: set,
    *,
    sheet_stem: str,
    cand_ids: Optional[Sequence[str]] = None,
) -> List[LifecycleEntry]:
    """P2.4：centerline 分类剔除的 DXF 候选 → lifecycle 条目。"""
    ids = list(cand_ids) if cand_ids is not None else [
        f"C{i + 1:03d}" for i in range(len(candidates))]
    out: List[LifecycleEntry] = []
    for c, cid in zip(candidates, ids):
        uid = str(c.get("bar_uid") or cid)
        if uid in keep_ids or cid in keep_ids:
            continue
        out.append(LifecycleEntry(
            bar_uid=uid,
            reason="centerline_drop",
            source="mllm_centerline_classify",
            sheet_stem=sheet_stem,
            component_id=str(c.get("component_id") or "") or None,
            segment_px=[
                float(c["x1"]), float(c["y1"]),
                float(c["x2"]), float(c["y2"]),
            ],
            detail=f"dropped_by={cid}",
        ))
    return out


def build_lifecycle_block(
    *,
    sheet_stem: str,
    geom_method: str,
    n_in: int,
    n_accepted: int,
    rejected_entries: Sequence[LifecycleEntry],
    audit: Optional[Dict[str, Any]] = None,
) -> LifecycleBlock:
    return LifecycleBlock(
        sheet_stem=sheet_stem,
        geom_method=geom_method,
        n_candidates_in=n_in,
        n_accepted=n_accepted,
        n_rejected=len(rejected_entries),
        rejected=list(rejected_entries),
        audit=dict(audit or {}),
    )


def append_lifecycle_to_model(model: Any, block: LifecycleBlock) -> None:
    """写入 drawing_file.properties.candidate_lifecycle（按 sheet 追加）。"""
    df = model.components.get("drawing_file")
    if df is None:
        return
    props = df.properties
    root = props.setdefault("candidate_lifecycle", {
        "protocol_version": LIFECYCLE_VERSION,
        "sheets": [],
        "totals": {"n_rejected": 0, "n_accepted": 0},
    })
    sheets: List[Dict[str, Any]] = root.setdefault("sheets", [])
    sheets.append(block.to_dict())
    totals = root.setdefault("totals", {"n_rejected": 0, "n_accepted": 0})
    totals["n_rejected"] = int(totals.get("n_rejected", 0)) + block.n_rejected
    totals["n_accepted"] = int(totals.get("n_accepted", 0)) + block.n_accepted


def lifecycle_blocks_from_model(model: dict) -> List[Dict[str, Any]]:
    """从 model.json dict 读取各 sheet lifecycle 块。"""
    df = (model.get("components") or {}).get("drawing_file") or {}
    cl = (df.get("properties") or {}).get("candidate_lifecycle") or {}
    return list(cl.get("sheets") or [])


def lifecycle_to_review_groups(blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """lifecycle rejected → review_queue 附加分组（不计 FP）。"""
    groups: List[Dict[str, Any]] = []
    for blk in blocks:
        stem = blk.get("sheet_stem") or "?"
        for r in blk.get("rejected") or []:
            groups.append({
                "kind": "mllm_rejected_candidate",
                "sheet_stem": stem,
                "bar_uid": r.get("bar_uid"),
                "reason": r.get("reason"),
                "source": r.get("source"),
                "confidence": r.get("confidence"),
                "segment_px": r.get("segment_px"),
                "component_id": r.get("component_id"),
                "detail": r.get("detail"),
                "suggested_action": "review_source_drawing_or_restore",
                "policy_note": "MLLM 拒候选不计入 A2 FP，仅人工复核",
            })
    return groups
