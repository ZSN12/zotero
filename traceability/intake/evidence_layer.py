"""P0 架构对齐（2026-09-05 审计）：observations / hypotheses 证据层。

背景：杆件的证据（件号文字关联、DIMENSION 标注样本）此前埋在
tower_bar.bar_id_evidence / drawing_file 报告字段里——没有组件身份、
没有稳定 ID、不可独立枚举。斜杆拓扑解释（fan/twist/kchain 候选）
在 build_interpretations 生成、select_interpretations 显式拒绝
（span_off_grid / duplicate_h / panel_crossing），但只以审计
dict 落盘，没有「假设」身份与状态机。

本模块给两类实体补身份（NeuBE 对照架构的 01 evidence 图谱对齐）：

* observation（观测）：图纸上的原始证据实体——件号文字、DIMENSION
  标注。稳定 ID `obs_{stem}_{kind}_{handle}`，带 confidence 与
  source 引用。杆件 DAG 依赖其证据观测（改标注 → 杆 stale）。
* hypothesis（假设）：由观测推导的结构解释候选——斜杆拓扑对
  (kind, z_lo, z_hi)。四态状态机：
      proposed   候选生成（评分 + 证据引用）
      accepted   通过筛选并生成几何
      rejected   显式拒绝（带 reason：span_off_grid 等）
      superseded 被更好的解释替代（原始投影杆撤下换模板杆）

两者都是普通 Component（kind=observation/hypothesis），随 model.json
序列化；expand_4_face_symmetry 的 _KEEP_KINDS 白名单放行（无几何面
语义，不参与展开）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..model import Component, EngineeringModel, SourceRef, SourceType

HYPOTHESIS_STATUSES = ("proposed", "accepted", "rejected", "superseded")


# --------------------------------------------------------------------------- #
# observations
# --------------------------------------------------------------------------- #

def label_observation_id(stem: str, label_component_id: str) -> str:
    """件号文字观测的稳定 ID：按文字实体标识（text_{handle}）。

    handle_label_evidence 的键是杆段 handle（同一条文字可关联多根杆
    ——变体段/分裂段），观测身份必须按文字实体（label_component_id）
    去重：一条文字 = 一个观测。
    """
    _tid = str(label_component_id or "")
    if _tid.startswith("text_"):
        _tid = _tid[len("text_"):]
    if not _tid:
        _tid = "unknown"
    return f"obs_{stem}_label_{_tid}"


def register_label_observations(
    model: EngineeringModel,
    stem: str,
    dxf_path: str,
    label_evidence: Dict[str, dict],
    *,
    dedupe: bool = True,
) -> List[str]:
    """件号文字关联 → observation 组件（kind=observation）。

    输入是 tower_dxf 阶段 4.4 的 handle_label_evidence（杆段 handle →
    关联记录：文字内容 / 方法 / 距离 / 置信度）。观测按**文字实体**
    （label_component_id）去重——同一文字关联多杆（变体段）只建一个
    观测；返回新建的 observation 组件 ID 列表。
    """
    made: List[str] = []
    seen: set = set()
    for handle, ev in label_evidence.items():
        obs_id = label_observation_id(stem, ev.get("label_component_id"))
        if obs_id in seen:
            continue
        seen.add(obs_id)
        if dedupe and obs_id in model.components:
            continue
        comp = Component(
            id=obs_id,
            name=f"观测·件号文字 {ev.get('text') or obs_id}",
            kind="observation",
            source=SourceRef(
                SourceType.DRAWING, dxf_path,
                detail=f"bar_handle={handle}, sheet={stem}, "
                       f"label_component={ev.get('label_component_id')}",
                confidence=float(ev.get("confidence") or 0.5),
            ),
            properties={
                "observation_kind": "bar_label",
                "sheet_id": stem,
                "text": str(ev.get("text") or ""),
                "label_component_id": str(ev.get("label_component_id") or ""),
                "association_method": str(ev.get("association_method") or ""),
                "distance": float(ev.get("distance") or 0.0),
                "distance_unit": str(ev.get("distance_unit") or "drawing"),
                "confidence": float(ev.get("confidence") or 0.5),
            },
            tags=["observation", "bar_label"],
        )
        model.add_component(comp)
        made.append(obs_id)
    return made


def register_dim_observations(
    model: EngineeringModel,
    stem: str,
    dxf_path: str,
    samples: Iterable[Any],
    *,
    context: Optional[str] = None,
) -> List[str]:
    """DIMENSION 标注样本 → observation 组件。

    输入是 scale_calibration.extract_dim_samples 的样本（DimSample：
    handle / defpoint / 值 / 视图区域）。标注是比例标定与节拍锚定的
    观测源头——改标注应传播 stale 到标定结果。
    """
    made: List[str] = []
    for s in samples:
        handle = getattr(s, "handle", None)
        if handle is None:
            # 兼容 dict 输入
            handle = (s or {}).get("handle") if isinstance(s, dict) else None
        if handle is None:
            continue
        obs_id = f"obs_{stem}_dim_{handle}"
        if obs_id in model.components:
            continue
        val = getattr(s, "value", None)
        if val is None and isinstance(s, dict):
            val = s.get("value")
        try:
            val = float(val) if val is not None else None
        except (TypeError, ValueError):
            val = None
        comp = Component(
            id=obs_id,
            name=f"观测·DIMENSION {val if val is not None else handle}",
            kind="observation",
            source=SourceRef(
                SourceType.DRAWING, dxf_path,
                detail=f"handle={handle}, sheet={stem}"
                       + (f", context={context}" if context else ""),
                confidence=0.95,
            ),
            properties={
                "observation_kind": "dim_sample",
                "sheet_id": stem,
                "value": val,
                "context": context,
            },
            tags=["observation", "dim_sample"],
        )
        model.add_component(comp)
        made.append(obs_id)
    return made


def depend_on_observations(
    model: EngineeringModel,
    comp_id: str,
    obs_ids: Sequence[str],
) -> int:
    """把 comp_id（通常是 tower_bar）的 DAG 上游登记到其证据观测。

    只登记实际存在的观测组件（防悬空）；不产生空集键。返回登记的边数。
    """
    if comp_id not in model.components:
        return 0
    valid = {o for o in obs_ids if o in model.components and o != comp_id}
    if not valid:
        return 0
    ups = model.dependencies.setdefault(comp_id, set())
    n0 = len(ups)
    ups.update(valid)
    return len(ups) - n0


# --------------------------------------------------------------------------- #
# hypotheses
# --------------------------------------------------------------------------- #

def register_hypotheses(
    model: EngineeringModel,
    stem: str,
    interpretations: Sequence[Dict[str, Any]],
    *,
    rejected: Optional[Sequence[Dict[str, Any]]] = None,
    superseded: Optional[Sequence[Dict[str, Any]]] = None,
    dxf_path: Optional[str] = None,
    generator: str = "diagonal_topology",
) -> List[str]:
    """斜杆拓扑解释候选 → hypothesis 组件（四态）。

    * interpretations：build_interpretations 的候选（含 kind/z_lo/z_hi/
      score/evidence）——登记为 proposed（调用方在确认生成几何后可升级
      为 accepted，见 mark_hypotheses_accepted）。
    * rejected：select_interpretations 审计里的被拒候选（含 reason）。
    * superseded：被模板杆替代的原始投影杆记录（removed_originals）。

    稳定 ID：hyp_{stem}_{generator}_{kind}_{zlo}_{zhi}（同名候选幂等，
    重复登记保留首个——分数会随后续证据漂移，ID 不漂移）。
    """
    made: List[str] = []

    def _hid(kind: str, z_lo: float, z_hi: float) -> str:
        return (f"hyp_{stem}_{generator}_{kind}"
                f"_{round(float(z_lo)):d}_{round(float(z_hi)):d}")

    def _mk(hid: str, status: str, interp: Dict[str, Any],
            reason: Optional[str]) -> Component:
        return Component(
            id=hid,
            name=(f"假设·{interp.get('kind', '?')} "
                  f"z[{round(float(interp.get('z_lo', 0))):d},"
                  f"{round(float(interp.get('z_hi', 0))):d}]"),
            kind="hypothesis",
            source=SourceRef(
                SourceType.DRAWING, dxf_path or f"{stem}.dxf",
                detail=f"generator={generator}, sheet={stem}, status={status}"
                       + (f", reject_reason={reason}" if reason else ""),
                confidence=1.0,
            ),
            properties={
                "hypothesis_kind": str(interp.get("kind") or "?"),
                "z_lo": round(float(interp.get("z_lo", 0.0)), 1),
                "z_hi": round(float(interp.get("z_hi", 0.0)), 1),
                "score": round(float(interp.get("score", 0.0)
                                     if interp.get("score") is not None else 0.0), 1),
                "evidence": list(interp.get("evidence") or [])[:8],
                "n_evidence": int(interp.get("n") or 0),
                "status": status,
                "reject_reason": reason,
                "sheet_id": stem,
                "generator": generator,
            },
            tags=["hypothesis", str(interp.get("kind") or "?")],
        )

    for interp in interpretations or ():
        hid = _hid(interp.get("kind", "?"),
                   interp.get("z_lo", 0.0), interp.get("z_hi", 0.0))
        if hid in model.components:
            continue
        model.add_component(_mk(hid, "proposed", interp, None))
        made.append(hid)

    for rej in rejected or ():
        hid = _hid(rej.get("kind", "?"),
                   rej.get("z_lo", 0.0), rej.get("z_hi", 0.0))
        if hid in model.components:
            # 候选已登记为 proposed：原地改状态而非双组件
            comp = model.components[hid]
            comp.properties["status"] = "rejected"
            comp.properties["reject_reason"] = str(rej.get("reason") or "")
            continue
        model.add_component(_mk(hid, "rejected", rej,
                                str(rej.get("reason") or "")))
        made.append(hid)

    for sup in superseded or ():
        interp = sup if isinstance(sup, dict) else {"kind": "?",
                                                    "z_lo": 0.0, "z_hi": 0.0}
        hid = _hid(interp.get("kind", "?"),
                   interp.get("z_lo", 0.0), interp.get("z_hi", 0.0))
        if hid not in model.components:
            model.add_component(_mk(hid, "superseded", interp, None))
            made.append(hid)
        else:
            model.components[hid].properties["status"] = "superseded"
    return made


def mark_hypotheses_accepted(
    model: EngineeringModel,
    hyp_ids: Iterable[str],
    *,
    generated_bar_ids: Optional[Sequence[str]] = None,
) -> int:
    """proposed → accepted：确认该解释生成了交付几何。

    generated_bar_ids 里的生成杆 DAG 上游登记到对应假设 ID——
    改假设（评分/层对调整）→ 生成杆 stale。返回升级数量。
    """
    n = 0
    for hid in hyp_ids:
        comp = model.components.get(hid)
        if comp is None or comp.kind != "hypothesis":
            continue
        if comp.properties.get("status") == "proposed":
            comp.properties["status"] = "accepted"
            n += 1
    if generated_bar_ids:
        for bid in generated_bar_ids:
            if bid in model.components:
                for hid in hyp_ids:
                    if hid in model.components and hid != bid:
                        model.dependencies.setdefault(bid, set()).add(hid)
    return n


def hypothesis_census(model: EngineeringModel) -> Dict[str, int]:
    """按状态统计假设（报表用）。"""
    from collections import Counter
    c = Counter()
    for comp in model.components.values():
        if comp.kind == "hypothesis":
            c[str(comp.properties.get("status") or "?")] += 1
    return dict(c)


def observation_census(model: EngineeringModel) -> Dict[str, int]:
    """按观测类型统计观测（报表用）。"""
    from collections import Counter
    c = Counter()
    for comp in model.components.values():
        if comp.kind == "observation":
            c[str(comp.properties.get("observation_kind") or "?")] += 1
    return dict(c)
