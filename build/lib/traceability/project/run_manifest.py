"""运行清单（阶段 0.2）：每次运行可追溯的 run_manifest.json。

《35A1-JC1单塔修复计划》阶段 0.2：让每次运行可追溯——用了什么输入、什么
模型、哪些缓存、每阶段多少杆件节点、输出了什么文件。

设计原则：
    * ``build_run_manifest()`` 是纯函数：全部新字段集中在这里组装，
      只依赖显式传入的参数，不读全局状态，便于单测。
    * 字段取不到时写 null 并继续，绝不抛异常中断主管线；
      ``write_run_manifest()`` 落盘失败也只 warning。
    * 尽力而为（best effort）：stages / bar_changelog 只聚合现有代码已经
      记录的事件（steps.json detail、merge_report、模型属性），不为凑字段
      去深度改造几何代码。
    * 确定性边界：vector/layout/merge 哈希可复现；MLLM 步骤非确定性。
"""

from __future__ import annotations

import hashlib
import json
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..model import EngineeringModel

# 确定性边界声明（阶段 0.2 要求的原样字符串）。
DETERMINISTIC_SCOPE = "vector/layout/merge 哈希可复现；MLLM 步骤非确定性"

# 每 sheet 阶段计数字段（字段缺失一律 null，禁止编造）。
STAGE_COUNT_FIELDS = (
    "a2_vector_bars",
    "a2_nodes",
    "merged_bars",
    "merged_nodes",
    "physical_bars",
    "derived_bars",
)

# A2 几何步骤的 step id（含 hybrid 旧名 a2_vector，见 pipeline_stages 归一化）。
_A2_STEP_IDS = ("a2_geom", "a2_vector")

# 视觉缓存命中的标记（mllm_backend.call_agent_json 写入 step detail.source）。
_CACHE_SOURCE_MARK = "agent_vision_cache"

# 现有代码已记录的拆分/合并/拼接事件键（steps.json step detail / 合并报告）。
# 有则聚合，无则 null——绝不编造。
BAR_EVENT_KEYS = (
    "stitched_fragments",        # hybrid：MLLM 斜材碎片拼接（hybrid_geometry.stitch_mllm_diagonals）
    "split_nodes",               # 预留：几何层打断事件（当前代码未记录，通常为 null）
    "injected_bars",             # hybrid：MLLM 杆件注入数量
    "stripped_vector_components",  # hybrid：被 MLLM 几何替换剔除的矢量构件
    "synthetic_side_nodes",      # 跨视图合并：合成侧视节点（merge_report / drawing_file props）
)

# bar_changelog 样例条数上限（防止大册 manifest 膨胀）。
_MAX_SAMPLES = 20

_SHA_CHUNK = 1 << 20  # sha256 分块读取：1 MiB


def sha256_file(path: str | Path) -> Optional[str]:
    """分块读取文件计算 sha256 十六进制摘要；不可读返回 None（不抛异常）。"""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_SHA_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def utc_now_iso() -> str:
    """ISO8601 UTC 时间戳（带 +00:00 时区后缀）。"""
    return datetime.now(timezone.utc).isoformat()


def _file_entry(path: str | Path, base_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """单个输入文件的 {file, sha256, bytes}；不可读返回 None。

    file 字段优先给相对 base_dir 的 posix 路径，便于 manifest 跨机器可读。
    """
    try:
        p = Path(path)
        if not p.is_file():
            return None
        entry: Dict[str, Any] = {
            "file": p.name,
            "sha256": sha256_file(p),
            "bytes": p.stat().st_size,
        }
        if base_dir is not None:
            try:
                entry["file"] = p.resolve().relative_to(base_dir.resolve()).as_posix()
            except ValueError:
                entry["file"] = p.resolve().as_posix()
        return entry
    except Exception:
        return None


def collect_inputs(
    input_dir: Optional[str | Path],
    overlay_path: Optional[str | Path] = None,
    bom_path: Optional[str | Path] = None,
    fallback_paths: Optional[Iterable[str | Path]] = None,
) -> Dict[str, Any]:
    """inputs 段：每个输入 DXF/DWG 的 sha256 + 字节数；overlay / BOM sha256。

    与 intake.dwg.ensure_dxf_batch 的发现规则一致：input_dir 顶层 .dxf/.dwg。
    目录为空或不可读时回退 fallback_paths（通常是 project.sheets 登记的路径）。
    """
    inputs: Dict[str, Any] = {"dxfs": [], "overlay": None, "bom": None}
    try:
        base = Path(input_dir) if input_dir else None
        dxf_entries: List[Dict[str, Any]] = []
        if base is not None and base.is_dir():
            for f in sorted(base.iterdir()):
                if f.suffix.lower() not in (".dxf", ".dwg"):
                    continue
                entry = _file_entry(f, base_dir=base)
                if entry:
                    dxf_entries.append(entry)
        if not dxf_entries and fallback_paths:
            seen: set = set()
            for fp in fallback_paths:
                if not fp:
                    continue
                entry = _file_entry(fp, base_dir=base)
                if entry and entry["file"] not in seen:
                    seen.add(entry["file"])
                    dxf_entries.append(entry)
        inputs["dxfs"] = dxf_entries
        if overlay_path:
            inputs["overlay"] = _file_entry(overlay_path, base_dir=base)
        if bom_path:
            inputs["bom"] = _file_entry(bom_path, base_dir=base)
    except Exception:
        # inputs 收集失败不中断：保留已收集的部分，其余为空。
        pass
    return inputs


def read_steps_json(steps_path: Optional[str | Path]) -> Optional[Dict[str, Any]]:
    """读取 per-sheet steps.json（ProcessingGraph.to_dict 结构）；失败返回 None。"""
    if not steps_path:
        return None
    try:
        p = Path(steps_path)
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _steps_list(steps: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """安全取 steps 列表（结构异常时返回空列表）。"""
    raw = (steps or {}).get("steps")
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict)]


def steps_cache_hit(steps: Optional[Dict[str, Any]]) -> Optional[bool]:
    """该 sheet 的视觉缓存是否命中（任一 step detail.source == agent_vision_cache）。

    steps 缺失返回 None；有 steps 但无缓存标记返回 False（未命中）。
    """
    for rec in _steps_list(steps):
        detail = rec.get("detail")
        if isinstance(detail, dict) and detail.get("source") == _CACHE_SOURCE_MARK:
            return True
    return False if steps is not None else None


def steps_a2_detail(steps: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """取 A2 几何步骤的 detail（id=a2_geom / 旧名 a2_vector / 名称含 A2 兜底）。"""
    recs = _steps_list(steps)
    for sid in _A2_STEP_IDS:
        for rec in recs:
            if rec.get("id") == sid and isinstance(rec.get("detail"), dict):
                return rec["detail"]
    for rec in recs:
        name = str(rec.get("name") or "")
        if "A2" in name and isinstance(rec.get("detail"), dict):
            return rec["detail"]
    return None


def steps_bar_events(steps: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """从 per-sheet steps 聚合已记录的拆分/合并/拼接事件计数（仅取正整数）。"""
    events: Dict[str, int] = {}
    for rec in _steps_list(steps):
        detail = rec.get("detail")
        if not isinstance(detail, dict):
            continue
        for key in BAR_EVENT_KEYS:
            val = detail.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                events[key] = events.get(key, 0) + int(val)
    return events


def _int_or_none(val: Any) -> Optional[int]:
    """宽容取整（bool / 非数值 / None 一律 None）。"""
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    return None


def _count_by_source(
    merged_model: Optional[EngineeringModel],
    stem: str,
    kind: str,
    predicate=None,
) -> Optional[int]:
    """按 source_file/drawing_view 归属统计合并模型里某 sheet 的组件数。

    merged_model 缺失返回 None；杆件可再叠加 predicate（如物理/派生语义过滤）。
    """
    if merged_model is None:
        return None
    try:
        n = 0
        for comp in merged_model.components.values():
            if comp.kind != kind:
                continue
            props = comp.properties or {}
            src = props.get("source_file") or props.get("drawing_view")
            if src != stem:
                continue
            if predicate is not None and not predicate(props):
                continue
            n += 1
        return n
    except Exception:
        return None


def _stage_counts_for_sheet(
    stem: str,
    *,
    steps: Optional[Dict[str, Any]],
    sheet_stats: Optional[Dict[str, Any]],
    merged_model: Optional[EngineeringModel],
) -> Dict[str, Any]:
    """单个 sheet 的六项阶段计数；每一项独立尽力获取，取不到写 null。

    a2_vector_bars 优先级：steps(a2).ezdxf_bars > steps(a2).vector_bars
    > sheet_stats.bars（ezdxf 纯矢量路径的 A2 计数）。
    a2_nodes 优先级：steps(a2).nodes > sheet_stats.nodes。
    merged/physical/derived 来自合并模型按 source_file 归属计数。
    """
    a2 = steps_a2_detail(steps) if steps else None
    stats = sheet_stats if isinstance(sheet_stats, dict) else None

    a2_vector_bars: Optional[int] = None
    a2_nodes: Optional[int] = None
    if a2 is not None:
        a2_vector_bars = _int_or_none(a2.get("ezdxf_bars"))
        if a2_vector_bars is None:
            a2_vector_bars = _int_or_none(a2.get("vector_bars"))
        a2_nodes = _int_or_none(a2.get("nodes"))
    if a2_vector_bars is None and stats is not None:
        a2_vector_bars = _int_or_none(stats.get("bars"))
    if a2_nodes is None and stats is not None:
        a2_nodes = _int_or_none(stats.get("nodes"))

    # 物理/派生语义过滤复用 eval.metrics 的 fail-closed 判定（只读，不改）。
    try:
        from ..eval.metrics import is_derived_bar, is_physical_bar
    except Exception:  # eval 不可用时不阻断 manifest
        is_derived_bar = is_physical_bar = None  # type: ignore[assignment]

    def _phys(props: Dict[str, Any]) -> bool:
        return bool(is_physical_bar(props)) if is_physical_bar else False

    def _derived(props: Dict[str, Any]) -> bool:
        return bool(is_derived_bar(props)) if is_derived_bar else False

    return {
        "a2_vector_bars": a2_vector_bars,
        "a2_nodes": a2_nodes,
        "merged_bars": _count_by_source(merged_model, stem, "tower_bar"),
        "merged_nodes": _count_by_source(merged_model, stem, "tower_node"),
        "physical_bars": _count_by_source(merged_model, stem, "tower_bar", predicate=_phys),
        "derived_bars": _count_by_source(merged_model, stem, "tower_bar", predicate=_derived),
    }


def collect_stages(
    sheet_ids: Iterable[str],
    *,
    steps_by_stem: Optional[Dict[str, Dict[str, Any]]] = None,
    sheet_stats: Optional[Dict[str, Dict[str, Any]]] = None,
    sheet_models: Optional[Dict[str, EngineeringModel]] = None,
    merged_model: Optional[EngineeringModel] = None,
) -> Dict[str, Dict[str, Any]]:
    """stages 段：每 sheet 的阶段计数（a2 / merged / physical / derived）。"""
    steps_map = steps_by_stem or {}
    stats_map = sheet_stats or {}
    models_map = sheet_models or {}
    stages: Dict[str, Dict[str, Any]] = {}
    for stem in sheet_ids:
        try:
            counts = _stage_counts_for_sheet(
                stem,
                steps=steps_map.get(stem),
                sheet_stats=stats_map.get(stem),
                merged_model=merged_model,
            )
        except Exception:
            counts = {field: None for field in STAGE_COUNT_FIELDS}
        stages[stem] = counts
    return stages


def collect_mllm_info(
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    steps_by_stem: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """mllm 段：provider / model / cache_used（尽力获取，取不到 null）。

    ezdxf 纯矢量路径没有 MLLM 上下文，provider/model/cache_used 均为 null。
    cache_used 语义：本次运行任一 sheet 命中视觉缓存即 True；
    有 per-sheet steps 但全未命中为 False；无可判定信息为 null。
    """
    by_sheet: Dict[str, bool] = {}
    for stem, steps in (steps_by_stem or {}).items():
        hit = steps_cache_hit(steps)
        if hit is not None:
            by_sheet[stem] = hit
    cache_used: Optional[bool] = None
    if by_sheet:
        cache_used = any(by_sheet.values())
    return {
        "provider": provider,
        "model": model,
        "cache_used": cache_used,
        "cache_used_by_sheet": by_sheet or None,
    }


def collect_bar_changelog(
    steps_by_stem: Optional[Dict[str, Dict[str, Any]]] = None,
    merged_model: Optional[EngineeringModel] = None,
    merge_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """bar_changelog 段（尽力而为）：聚合现有代码已记录的杆件变更事件。

    事件来源（不改造几何代码）：
        * per-sheet steps.json step detail（hybrid：stitched_fragments 等）
        * 合并模型 drawing_file 属性（synthetic_side_nodes 等）
        * merge_report（跨视图合并报告里的同类计数）
    几何代码未记录的事件（如 split_nodes）保持 null，绝不编造。
    """
    totals: Dict[str, int] = {}
    samples: List[Dict[str, Any]] = []

    for stem, steps in sorted((steps_by_stem or {}).items()):
        events = steps_bar_events(steps)
        for key in BAR_EVENT_KEYS:
            if key in events:
                totals[key] = totals.get(key, 0) + events[key]
                if len(samples) < _MAX_SAMPLES:
                    samples.append({
                        "sheet": stem,
                        "event": key,
                        "count": events[key],
                    })

    # 合并模型 drawing_file 属性里已记录的合成/派生事件计数。
    try:
        if merged_model is not None:
            df = merged_model.components.get("drawing_file")
            if df is not None:
                for key in BAR_EVENT_KEYS:
                    val = (df.properties or {}).get(key)
                    n = _int_or_none(val)
                    if n and n > 0:
                        totals[key] = totals.get(key, 0) + n
    except Exception:
        pass

    # merge_report 里已有的同类计数（不重复计 drawing_file 已聚合的键）。
    try:
        if isinstance(merge_report, dict):
            for key in BAR_EVENT_KEYS:
                if key in ("synthetic_side_nodes",) and key in totals:
                    continue
                n = _int_or_none(merge_report.get(key))
                if n and n > 0 and key not in totals:
                    totals[key] = n
    except Exception:
        pass

    return {
        "counts": {key: totals.get(key) for key in BAR_EVENT_KEYS},
        "total_events": sum(totals.values()),
        "samples": samples,
        "note": "仅聚合现有代码已记录的拆分/合并/拼接事件；未记录的事件为 null，不深度改造几何代码",
    }


def collect_outputs(
    out_dir: Optional[str | Path],
    candidates: Iterable[str | Path],
) -> List[str]:
    """outputs 段：本次运行写出的关键文件相对 out_dir 的 posix 路径清单。

    只收录真实存在的文件；路径解析失败时保留原样字符串。
    """
    base = Path(out_dir) if out_dir else None
    outputs: List[str] = []
    seen: set = set()
    for cand in candidates:
        if not cand:
            continue
        try:
            p = Path(cand)
            rel: Optional[str] = None
            if base is not None:
                try:
                    rel = p.resolve().relative_to(base.resolve()).as_posix()
                except ValueError:
                    rel = None
            entry = rel or p.as_posix()
            if p.is_file() and entry not in seen:
                seen.add(entry)
                outputs.append(entry)
        except Exception:
            continue
    return sorted(outputs)


def build_run_manifest(
    *,
    project_id: Optional[str] = None,
    input_dir: Optional[str | Path] = None,
    overlay_path: Optional[str | Path] = None,
    bom_path: Optional[str | Path] = None,
    out_dir: Optional[str | Path] = None,
    sheet_ids: Optional[Iterable[str]] = None,
    sheet_stats: Optional[Dict[str, Dict[str, Any]]] = None,
    sheet_models: Optional[Dict[str, EngineeringModel]] = None,
    merged_model: Optional[EngineeringModel] = None,
    steps_by_stem: Optional[Dict[str, str | Path]] = None,
    merge_report: Optional[Dict[str, Any]] = None,
    output_candidates: Optional[Iterable[str | Path]] = None,
    mllm_provider: Optional[str] = None,
    mllm_model: Optional[str] = None,
    run_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """组装 run_manifest dict（纯函数，阶段 0.2 的全部新字段集中在这里）。

    所有参数显式传入、可为 None；每个字段独立尽力获取，缺失写 null，
    任何子段失败都不抛异常（返回部分 manifest）。

    参数：
        project_id:      项目 ID（可空）。
        input_dir:       输入目录（发现顶层 .dxf/.dwg 并计算 sha256）。
        overlay_path:    overlay/layer_map 文件（sha256）。
        bom_path:        master BOM 文件（sha256，若有）。
        out_dir:         输出目录（outputs 相对路径的基准）。
        sheet_ids:       sheet stem 列表（stages 键）。
        sheet_stats:     {stem: {"bars": n, "nodes": n}}（ezdxf 路径 A2 统计）。
        sheet_models:    {stem: EngineeringModel}（备用上下文，当前仅保留语义）。
        merged_model:    跨文件合并模型（merged/physical/derived 计数来源）。
        steps_by_stem:   {stem: steps.json 路径}（hybrid 路径的 A2 明细/缓存/事件）。
        merge_report:    跨文件合并报告（bar_changelog 补充来源）。
        output_candidates: 关键输出文件候选路径（只收录真实存在者）。
        mllm_provider / mllm_model: MLLM 上下文（hybrid 路径才有，否则 null）。
        run_id / created_at: 可注入以便测试复现；缺省自动生成。
    """
    rid = run_id or uuid.uuid4().hex
    ts = created_at or utc_now_iso()

    # per-sheet steps 预读一次，stages / mllm / bar_changelog 共用。
    parsed_steps: Dict[str, Dict[str, Any]] = {}
    for stem, sp in (steps_by_stem or {}).items():
        data = read_steps_json(sp)
        if data is not None:
            parsed_steps[stem] = data

    manifest: Dict[str, Any] = {
        "run_id": rid,
        "created_at": ts,
        # 阶段 0.2 确定性边界：原样字符串，vector/layout/merge 可复现、MLLM 非确定。
        "deterministic_scope": DETERMINISTIC_SCOPE,
        "project_id": project_id,
    }
    try:
        manifest["inputs"] = collect_inputs(
            input_dir, overlay_path=overlay_path, bom_path=bom_path,
        )
    except Exception:
        manifest["inputs"] = {"dxfs": [], "overlay": None, "bom": None}
    try:
        manifest["mllm"] = collect_mllm_info(
            provider=mllm_provider, model=mllm_model, steps_by_stem=parsed_steps,
        )
    except Exception:
        manifest["mllm"] = {
            "provider": mllm_provider, "model": mllm_model,
            "cache_used": None, "cache_used_by_sheet": None,
        }
    try:
        manifest["stages"] = collect_stages(
            sheet_ids or [],
            steps_by_stem=parsed_steps,
            sheet_stats=sheet_stats,
            sheet_models=sheet_models,
            merged_model=merged_model,
        )
    except Exception:
        manifest["stages"] = {}
    try:
        manifest["outputs"] = collect_outputs(out_dir, output_candidates or [])
    except Exception:
        manifest["outputs"] = []
    try:
        manifest["bar_changelog"] = collect_bar_changelog(
            parsed_steps, merged_model, merge_report=merge_report,
        )
    except Exception:
        manifest["bar_changelog"] = {
            "counts": {key: None for key in BAR_EVENT_KEYS},
            "total_events": 0,
            "samples": [],
            "note": "事件聚合失败",
        }
    return manifest


def write_run_manifest(
    manifest: Dict[str, Any],
    out_dir: str | Path,
) -> Optional[str]:
    """落盘 run_manifest.json；失败只 warning、返回 None，绝不中断主管线。"""
    try:
        base = Path(out_dir)
        base.mkdir(parents=True, exist_ok=True)
        path = base / "run_manifest.json"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)
    except Exception as exc:
        warnings.warn(f"run_manifest.json 写失败（不中断主管线）：{exc}")
        return None
