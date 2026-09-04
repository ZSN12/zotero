# -*- coding: utf-8 -*-
"""P7：MLLM 候选输出协议 + 置信度分层验证 + 双源交叉验证证据。

背景（阶段 3.6 之后的缺口）：candidate_fusion=union_dedup 已实现
「MLLM/ezdxf 候选并集 + 空间去重」，但融合是静默的：
    * 候选没有统一协议（bar_uid/坐标/置信度/来源/像素→图纸坐标链）；
    * 低置信候选与高置信候选同权入模，「识别结果可信」无分层；
    * MLLM 杆与 ezdxf 杆的空间一致/单源/冲突关系不产出证据，
      「每个构件可追溯」在融合环节断链。

本模块（纯离线后处理，不改生产管线核心路径）：

    1. CandidateRecord（协议 v1）：候选的规范化记录。
    2. build_candidate_protocol()：把 MLLM bars（像素）+ ezdxf 杆（图纸
       坐标）按协议归一（同一坐标系下比对）。
    3. stratify_by_confidence()：置信度分层——
           high   >= 0.7  直接采信
           medium [0.4, 0.7) 采信但标 review
           low    < 0.4  拒绝入模（记 review_queue，证据保留）
       置信度来源：MLLM 自报 confidence 与「双源一致」加成（ezdxf 同位
       杆存在 → +0.2，封顶 1.0）——几何双源一致是最强的可信证据。
    4. cross_validate()：双源关系分类——
           consistent  MLLM 与 ezdxf 空间重复（角度/长度比/中点三条件 AND）
           mllm_only   仅 MLLM 检出（ezdxf 无同位杆）
           dxf_only    仅 ezdxf 检出（MLLM 漏检——union_dedup 的候补源）
       输出证据报告（进 drawing_file.properties.mllm_cross_validation）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 协议版本（变更时递增，消费方可按版本分支解析）
CANDIDATE_PROTOCOL_VERSION = 1

# 置信度分层阈值（P7 校准：MLLM 自报扫描图 ≤0.6 的约束下，
# 双源一致 +0.2 使一致杆进入 high，单源 MLLM 留在 medium）
CONF_HIGH = 0.7
CONF_MEDIUM = 0.4

# 双源一致加成
DUAL_SOURCE_BONUS = 0.2


@dataclass
class CandidateRecord:
    """协议 v1：单个杆件候选的规范化记录。

    坐标单位：像素（px）——MLLM 原生输出；图纸坐标由消费方用
    mapping（render_dxf_preview_with_mapping 的变换）换算，协议不假设。
    """

    bar_uid: str
    x1: float
    y1: float
    x2: float
    y2: float
    source_agent: str                  # "mllm" / "dxf"
    confidence_raw: float = 0.6        # 源自报置信度
    confidence_effective: float = 0.6  # 双源加成后的有效置信度
    stratium: str = "medium"           # high / medium / low
    cross_source: str = "mllm_only"    # consistent / mllm_only / dxf_only
    matched_component_id: Optional[str] = None   # ezdxf 同位杆组件 id
    model: Optional[str] = None        # MLLM 模型名（溯源）
    image_ref: Optional[str] = None    # 图源（PNG/裁剪图路径）
    protocol_version: int = CANDIDATE_PROTOCOL_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _seg_angle(s: Tuple[float, float, float, float]) -> float:
    return math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180.0


def _seg_len(s: Tuple[float, float, float, float]) -> float:
    return math.hypot(s[2] - s[0], s[3] - s[1])


def _seg_mid(s: Tuple[float, float, float, float]) -> Tuple[float, float]:
    return ((s[0] + s[2]) / 2.0, (s[1] + s[3]) / 2.0)


def _segments_match(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
    *,
    angle_tol_deg: float = 15.0,
    length_ratio_tol: float = 1.5,
    midpoint_ratio: float = 0.5,
) -> bool:
    """与 _vector_bars_not_covered 相同的三条件 AND（口径一致，宁漏判不多删）。"""
    la, lb = _seg_len(a), _seg_len(b)
    if la <= 1e-9 or lb <= 1e-9:
        return False
    da = abs(_seg_angle(a) - _seg_angle(b))
    da = min(da, 180.0 - da)
    if da > angle_tol_deg:
        return False
    if max(la, lb) / min(la, lb) > length_ratio_tol:
        return False
    ma, mb = _seg_mid(a), _seg_mid(b)
    d_mid = math.hypot(ma[0] - mb[0], ma[1] - mb[1])
    if d_mid > midpoint_ratio * min(la, lb):
        return False
    return True


def cross_validate(
    mllm_bars: Sequence[Dict[str, Any]],
    dxf_bars: Sequence[Dict[str, Any]],
    *,
    model_name: Optional[str] = None,
    image_ref: Optional[str] = None,
) -> Tuple[List[CandidateRecord], Dict[str, Any]]:
    """双源交叉验证：MLLM 候选 × ezdxf 候选 → 协议记录 + 证据报告。

    mllm_bars：[{bar_uid, x1, y1, x2, y2, confidence?, ...}]（像素）。
    dxf_bars：[{bar_uid/component_id, x1, y1, x2, y2}]（**同坐标系**——
    调用方须先换算到与 MLLM 相同的像素坐标系，即
    drawing_xy_to_px()）。

    返回 (records, report)：
        records——每个 MLLM 候选一条（含 dxf_only 的 ezdxf 候补记录，
        source_agent="dxf"，供 review 队列完整呈现）；
        report——计数 + 一致率（进 drawing_file 证据链）。
    """
    mllm_segs: List[Tuple[float, float, float, float]] = []
    for b in mllm_bars:
        try:
            mllm_segs.append((float(b["x1"]), float(b["y1"]),
                              float(b["x2"]), float(b["y2"])))
        except (KeyError, TypeError, ValueError):
            continue
    dxf_segs: List[Tuple[float, float, float, float]] = []
    dxf_ids: List[Optional[str]] = []
    for b in dxf_bars:
        try:
            dxf_segs.append((float(b["x1"]), float(b["y1"]),
                             float(b["x2"]), float(b["y2"])))
            dxf_ids.append(str(b.get("component_id") or b.get("bar_uid") or ""))
        except (KeyError, TypeError, ValueError):
            continue

    used_dxf: set = set()
    records: List[CandidateRecord] = []
    n_consistent = 0
    for i, b in enumerate(mllm_bars):
        seg = mllm_segs[i] if i < len(mllm_segs) else None
        if seg is None:
            continue
        raw_conf = float(b.get("confidence") or 0.6)
        rec = CandidateRecord(
            bar_uid=str(b.get("bar_uid") or f"mllm_{i:04d}"),
            x1=seg[0], y1=seg[1], x2=seg[2], y2=seg[3],
            source_agent="mllm",
            confidence_raw=raw_conf,
            model=model_name,
            image_ref=image_ref,
        )
        # 双源一致检测
        matched: Optional[str] = None
        for j, dseg in enumerate(dxf_segs):
            if j in used_dxf:
                continue
            if _segments_match(seg, dseg):
                matched = dxf_ids[j] if j < len(dxf_ids) else None
                used_dxf.add(j)
                break
        if matched is not None:
            rec.cross_source = "consistent"
            rec.matched_component_id = matched
            rec.confidence_effective = min(1.0, raw_conf + DUAL_SOURCE_BONUS)
            n_consistent += 1
        else:
            rec.cross_source = "mllm_only"
            rec.confidence_effective = raw_conf
        rec.stratium = stratify(rec.confidence_effective)
        records.append(rec)

    # dxf_only：MLLM 漏检、由 ezdxf 候补的候选（union_dedup 保留源）
    for j, dseg in enumerate(dxf_segs):
        if j in used_dxf:
            continue
        rec = CandidateRecord(
            bar_uid=str(dxf_ids[j] or f"dxf_{j:04d}") if j < len(dxf_ids) else f"dxf_{j:04d}",
            x1=dseg[0], y1=dseg[1], x2=dseg[2], y2=dseg[3],
            source_agent="dxf",
            confidence_raw=0.9,  # 矢量图坐标为真值（P1-1 约束：矢量 ≤0.9）
            confidence_effective=0.9,
            cross_source="dxf_only",
            matched_component_id=dxf_ids[j] if j < len(dxf_ids) else None,
        )
        rec.stratium = stratify(rec.confidence_effective)
        records.append(rec)

    n_mllm = len(mllm_segs)
    report = {
        "protocol_version": CANDIDATE_PROTOCOL_VERSION,
        "mllm_candidates": n_mllm,
        "dxf_candidates": len(dxf_segs),
        "consistent": n_consistent,
        "mllm_only": n_mllm - n_consistent,
        "dxf_only": len(dxf_segs) - len(used_dxf),
        "consistency_rate": round(n_consistent / n_mllm, 3) if n_mllm else None,
        "strata": {
            s: sum(1 for r in records if r.stratium == s)
            for s in ("high", "medium", "low")
        },
        "model": model_name,
        "image_ref": image_ref,
    }
    return records, report


def stratify(confidence: float) -> str:
    """置信度分层：high >= 0.7 > medium >= 0.4 > low。"""
    if confidence >= CONF_HIGH:
        return "high"
    if confidence >= CONF_MEDIUM:
        return "medium"
    return "low"


def apply_confidence_gate(
    records: Sequence[CandidateRecord],
    *,
    accept_strata: Sequence[str] = ("high", "medium"),
) -> Tuple[List[CandidateRecord], List[CandidateRecord], Dict[str, int]]:
    """置信度门：low 层拒绝入模（review 队列保留证据）。

    返回 (accepted, rejected, counts)。
    accepted 中的 medium 层带 stratium="medium" 标记，消费方可再送
    人工 review；low 一律拒绝——「识别结果可信」的下限保证。
    """
    accepted: List[CandidateRecord] = []
    rejected: List[CandidateRecord] = []
    for r in records:
        if r.stratium in accept_strata:
            accepted.append(r)
        else:
            rejected.append(r)
    counts = {
        "accepted": len(accepted),
        "rejected_low_confidence": len(rejected),
        "accepted_medium_review": sum(1 for r in accepted if r.stratium == "medium"),
    }
    return accepted, rejected, counts


def filter_mllm_bars_for_inject(
    mllm_bars: Sequence[Dict[str, Any]],
    dxf_bars_px: Sequence[Dict[str, Any]],
    *,
    model_name: Optional[str] = None,
    image_ref: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[CandidateRecord], Dict[str, Any]]:
    """P2.5：低置信 MLLM 候选阻断注入（accepted 才返回）。

    返回 (filtered_bars, rejected_records, audit)。
    """
    records, cv_report = cross_validate(
        mllm_bars, dxf_bars_px, model_name=model_name, image_ref=image_ref)
    accepted, rejected, gate_counts = apply_confidence_gate(records)
    accepted_uids = {r.bar_uid for r in accepted}
    # bar_uid 缺省时按序 C001 与 cross_validate 一致
    filtered: List[Dict[str, Any]] = []
    for i, b in enumerate(mllm_bars):
        uid = str(b.get("bar_uid") or f"mllm_{i:04d}")
        if uid in accepted_uids:
            filtered.append(b)
    audit = {
        "block_inject": True,
        "confidence_gate": gate_counts,
        "cross_validation": cv_report,
        "n_in": len(mllm_bars),
        "n_out": len(filtered),
        "n_rejected": len(rejected),
    }
    return filtered, rejected, audit


def records_to_evidence(
    records: Sequence[CandidateRecord],
    report: Dict[str, Any],
    gate_counts: Dict[str, int],
) -> Dict[str, Any]:
    """协议记录 → drawing_file.properties 证据块（可追溯交付）。"""
    return {
        "protocol_version": CANDIDATE_PROTOCOL_VERSION,
        "summary": report,
        "confidence_gate": gate_counts,
        "records": [r.to_dict() for r in records],
    }
