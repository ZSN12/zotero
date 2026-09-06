"""图册级一键交付（M6 / Gap 1）。

build-project → cross_file_batch → Harness → strict GLB → 交付 manifest。
M7：图册级 Project Harness + 件号索引 + BOM 树汇总。
M8：master BOM 物理件号核对 + 模块装配 demo + Web 工作台增强。
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..harness.harness import run_harness
from ..model import EngineeringModel
from .model import ProjectModel, build_project_from_directory, save_project
from .module_build import (
    physical_bar_counts,
    resolve_master_bom_path,
    try_assembly_from_merged,
    try_assembly_m1_m6_from_merged,
)
from .run_manifest import build_run_manifest, write_run_manifest


def export_detail_qa_atlas(
    sheet_models: List[tuple[str, EngineeringModel]],
    out_path: Path,
    overlay_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """P4：二维详图/模块页杆件按 sheet 分块平铺（z 方向错开）的 QA 视图。

    这是「二维详图分 Z 层平铺」的非真实 3D 结构，仅供目视检查 MLLM 几何。
    detail 页节点只有 2D 图纸坐标（x/y，缺 z，且是图纸绝对坐标），这里做
    局部归一化——每 sheet 节点 x/y 减自身 bbox 中心，再写 x/y/z 三轴
    （z=分块偏移），使 solve_tower 能拿到三轴、杆件可实体化。

    返回 {"present", "path", "sheets", "bars"}，并标注 non_structural。
    """
    from ..intake.tower_spec import sheet_is_spatial_mergeable
    from ..model import Component, SourceRef, SourceType
    from ..solve.tower_solver import export_tower_glb, SolveError

    atlas = EngineeringModel(name="hybrid-detail-atlas")
    z_step = 8000.0
    z_off = 0.0
    sheet_count = 0
    bar_total = 0

    for stem, model in sheet_models:
        if sheet_is_spatial_mergeable(stem, overlay=overlay_path):
            continue
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        if not bars:
            continue
        sheet_count += 1
        prefix = f"{stem}__"

        # 局部归一化：该 sheet 节点 x/y 的 bbox 中心 → 平移原点
        sheet_nodes = [c for c in model.components.values() if c.kind == "tower_node"]
        xs = [float(c.properties.get("x")) for c in sheet_nodes
              if c.properties.get("x") is not None]
        ys = [float(c.properties.get("y")) for c in sheet_nodes
              if c.properties.get("y") is not None]
        cx = (min(xs) + max(xs)) / 2.0 if xs else 0.0
        cy = (min(ys) + max(ys)) / 2.0 if ys else 0.0

        for cid, comp in model.components.items():
            if comp.kind not in ("tower_node", "tower_bar"):
                continue
            props = dict(comp.properties)
            if comp.kind == "tower_node":
                px = float(props.get("x") or 0.0)
                py = float(props.get("y") or 0.0)
                props["x"] = round(px - cx, 2)
                props["y"] = round(py - cy, 2)
                props["z"] = z_off
                props["solve_status"] = "solved"
            elif comp.kind == "tower_bar":
                fn = props.get("from_node")
                tn = props.get("to_node")
                if fn:
                    props["from_node"] = f"{prefix}{fn}"
                if tn:
                    props["to_node"] = f"{prefix}{tn}"
                bar_total += 1
            atlas.add_component(Component(
                id=f"{prefix}{cid}",
                name=f"[{stem}] {comp.name}",
                kind=comp.kind,
                source=comp.source or SourceRef(SourceType.DRAWING, stem, confidence=0.5),
                properties=props,
            ))
        z_off += z_step

    if bar_total == 0:
        return {"present": False, "error": "无详图杆件可导出", "non_structural": True}

    try:
        export_tower_glb(atlas, out_path, strict=False, allow_derived_y=True)
        return {
            "present": True, "path": str(out_path), "sheets": sheet_count,
            "bars": bar_total, "non_structural": True,
            "note": "二维详图分 Z 层平铺的 QA 视图，非真实 3D 结构",
        }
    except SolveError as exc:
        return {"present": False, "error": str(exc), "non_structural": True}


def _load_review_exemptions(ov: Dict[str, Any],
                            layer_map_path: Optional[str | Path] = None) -> Optional[Path]:
    """线1 verified delivery（2026-09-03）：加载人工复核豁免文件。

    人工复核豁免 = 明确、可审计、带指纹的 review 结论（对标
    confirm_tower_scan → verified 的既有纪律）。文件从 overlay 的
    review_exemptions_file 键解析（相对路径优先按 overlay 目录，其次
    CWD）。豁免不是静默通过：每个豁免规则在交付报告里显式列为
    review_exempted（带 reason / reviewed_by / reviewed_at），且 pending
    内容指纹不匹配时豁免自动失效（防止过期橡皮章）。
    """
    rel = ov.get("review_exemptions_file")
    if not rel:
        return None
    candidates = [Path(rel)]
    if layer_map_path and not Path(rel).is_absolute():
        try:
            candidates.insert(0, Path(layer_map_path).parent / rel)
        except (TypeError, ValueError):
            pass
    for p in candidates:
        if p.exists():
            return p
    return None


def _apply_review_exemptions(
    harness: Dict[str, Any],
    exemption_path: Optional[Path],
) -> Dict[str, Any]:
    """把有效的人工豁免应用到 harness 摘要（in-place 修改 + 返回披露）。

    有效性三条件：规则当前 pending；豁免未过期；消息指纹匹配
    （sha256 前 16 位 == 豁免文件的 message_fingerprint）。三者任一
    不满足则豁免不生效，规则保持 pending。

    应用后：rule 从 pending 列表移入 review_exempted 列表（不是
    passed——报告中永远可见）；counts 里 pending-1、新增
    review_exempted 计数。
    """
    disclosure: Dict[str, Any] = {
        "exemption_file": str(exemption_path) if exemption_path else None,
        "applied": [],
        "rejected": [],
    }
    if exemption_path is None or not Path(exemption_path).exists():
        return disclosure
    try:
        import hashlib
        from datetime import date as _date
        doc = json.loads(Path(exemption_path).read_text(encoding="utf-8"))
        expires = doc.get("expires")
        if expires and str(expires) < _date.today().isoformat():
            disclosure["rejected"].append({
                "rule": "*", "reason": f"豁免已过期（expires={expires}）"})
            return disclosure
        exemptions = doc.get("exemptions") or {}
        for ex_rule, ex in exemptions.items():
            msg = next((r["message"] for r in harness.get("results", [])
                        if r.get("rule") == ex_rule
                        and r.get("status") == "pending"), None)
            if msg is None:
                disclosure["rejected"].append({
                    "rule": ex_rule, "reason": "规则当前非 pending（豁免无对象）"})
                continue
            fp = hashlib.sha256(str(msg).encode("utf-8")).hexdigest()[:16]
            if ex.get("message_fingerprint") != fp:
                disclosure["rejected"].append({
                    "rule": ex_rule,
                    "reason": "消息指纹不匹配（pending 内容已变化，豁免失效）"})
                continue
            # 生效：pending → review_exempted（显式，非 passed）
            for r in harness.get("results", []):
                if r.get("rule") == ex_rule and r.get("status") == "pending":
                    r["status"] = "review_exempted"
                    r["message"] = (f"[人工复核豁免] {ex.get('reason', '')}"
                                    f"（reviewed_by={doc.get('reviewed_by')}, "
                                    f"at={doc.get('reviewed_at')}）")
            harness["pending"] = [p for p in harness.get("pending", [])
                                  if p != ex_rule]
            harness.setdefault("review_exempted", []).append(ex_rule)
            counts = harness.setdefault("counts", {})
            if counts.get("pending"):
                counts["pending"] -= 1
            counts["review_exempted"] = counts.get("review_exempted", 0) + 1
            disclosure["applied"].append({
                "rule": ex_rule,
                "reason": ex.get("reason", ""),
                "reviewed_by": doc.get("reviewed_by"),
                "reviewed_at": doc.get("reviewed_at"),
            })
        return disclosure
    except (json.JSONDecodeError, OSError) as exc:
        disclosure["rejected"].append({"rule": "*",
                                       "reason": f"豁免文件不可读：{exc}"})
        return disclosure


def _harness_summary(model: EngineeringModel) -> Dict[str, Any]:
    results = run_harness(model)
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    failed = [r.target_id for r in results if r.status.value == "failed"]
    pending = [r.target_id for r in results if r.status.value == "pending"]
    return {
        "counts": counts,
        "failed": failed,
        "pending": pending,
        "results": [
            {"rule": r.target_id, "status": r.status.value, "message": r.message}
            for r in results
        ],
    }


def _resolve_canonical_tower_path(
    input_dir: Path,
    ov: Dict[str, Any],
    layer_map_path: Optional[str | Path],
) -> Optional[Path]:
    """解析 L0 权威塔源路径（GIM/.NODE 的 GT JSON），只认 overlay 配置。

    优先级：overlay.canonical_tower.gt_json / canonical_tower_gt
    > 目录内 *canonical*.json / *ground_truth*.json。
    相对路径先按 overlay 文件目录解析，再按 input_dir 解析。
    找不到就返回 None（L0 缺失，不影响 skeleton/index 交付）。
    """
    if not isinstance(ov, dict):
        return None
    raw = ov.get("canonical_tower")
    rel = None
    if isinstance(raw, dict):
        rel = raw.get("mod") or raw.get("gt_json") or raw.get("json")
    elif isinstance(raw, str):
        rel = raw
    if not rel and ov.get("canonical_tower_gt"):
        rel = ov.get("canonical_tower_gt")
    candidates: List[Path] = []
    if rel:
        rel_p = Path(str(rel))
        if rel_p.is_absolute():
            candidates.append(rel_p)
        else:
            if layer_map_path and not isinstance(layer_map_path, dict):
                candidates.append(Path(layer_map_path).parent / rel_p)
            candidates.append(input_dir / rel_p)
            candidates.append(rel_p)
    for pattern in ("*canonical*.json", "*ground_truth*.json"):
        candidates.extend(sorted(input_dir.glob(pattern)))
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    return None


def _sheet_model_stats(models: List[EngineeringModel], sources: List[str]) -> Dict[str, Any]:
    """M1 全册 per-sheet 解析统计（index.json 数据源）。"""
    out: Dict[str, Any] = {}
    for i, model in enumerate(models):
        src = sources[i] if i < len(sources) else model.name
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        nodes = [c for c in model.components.values() if c.kind == "tower_node"]
        labeled = [c for c in bars if not str(c.properties.get("bar_id", "")).startswith("UNLABELED")]
        out[src] = {
            "bars": len(bars),
            "nodes": len(nodes),
            "labeled": len(labeled),
            "association_rate": round(len(labeled) / len(bars), 4) if bars else 0.0,
        }
    return out


def _write_index_artifact(
    out_dir: Path,
    project: ProjectModel,
    sheet_stats: Dict[str, Any],
) -> Path:
    """Phase A3：写 index.json（M1 全册索引，含角色与空间合并资格）。"""
    sheets = {}
    for sid, sheet in project.sheets.items():
        entry = {
            "sheet_id": sid,
            "path": sheet.path,
            "kind": sheet.kind,
            "role": sheet.role,
            "spatial_mergeable": bool(sheet.spatial_mergeable),
            "module_id": sheet.module_id,
            "view_kinds": sheet.view_kinds,
            "model_path": sheet.model_path,
            "projection_refs": sheet.projection_refs,
            "parse": sheet_stats.get(sid, {}),
        }
        sheets[sid] = entry
    index_path = out_dir / "index.json"
    index_path.write_text(json.dumps({
        "project_id": project.project_id,
        "name": project.name,
        "sheet_count": len(project.sheets),
        "spatial_merge_sheets": sorted(
            sid for sid, sh in project.sheets.items() if sh.spatial_mergeable
        ),
        "sheets": sheets,
        "modules": project.modules,
        "metadata": project.metadata,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return index_path


def _write_project_artifacts(
    out_dir: Path,
    *,
    bar_inventory: Dict[str, Any],
    bom_tree: Dict[str, Any],
    project_harness: Dict[str, Any],
) -> Dict[str, str]:
    paths = {
        "bar_inventory": str(out_dir / "bar_inventory.json"),
        "bom_tree": str(out_dir / "bom_tree.json"),
        "project_harness": str(out_dir / "project_harness.json"),
    }
    Path(paths["bar_inventory"]).write_text(
        json.dumps(bar_inventory, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    Path(paths["bom_tree"]).write_text(
        json.dumps(bom_tree, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    Path(paths["project_harness"]).write_text(
        json.dumps(project_harness, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return paths


def _build_hybrid_project(
    input_dir: Path,
    out_dir: Path,
    pid: str,
    *,
    layer_map_path: Optional[str | Path],
    bom_path: Optional[str | Path],
) -> tuple[ProjectModel, str, Dict[str, EngineeringModel], Dict[str, Dict[str, Any]]]:
    """agent_mode="hybrid"：用 Kimi/MLLM Agent 链跑每张 sheet，构建 Project 索引。

    每张 sheet 用 run_hybrid_dxf_agent_pipeline 产出 model.json（MLLM 几何替换
    ezdxf 垃圾几何、节点带 view_x/view_y + view_type=front），再登记进
    ProjectModel（复用 build_project_from_directory 的索引/模块/证据聚合逻辑）。
    返回 (project, project_path, {sheet_id: EngineeringModel}, pipelines)，
    pipelines 为每张 sheet 的 {steps_path, mllm_provider, mllm_model}
    （阶段 0.2 run_manifest 的 mllm / 视觉缓存 / 事件聚合来源）。
    """
    from ..intake.dwg import ensure_dxf_batch
    from ..intake.hybrid_dxf_agent import run_hybrid_dxf_agent_pipeline
    from ..intake.mllm_backend import MLLMBackend
    from ..intake.tower_dxf import resolve_drawing_kind
    from ..intake.tower_spec import canonical_sheet_role, sheet_is_spatial_mergeable
    from ..io import load_model
    from .model import ProjectSheet, _infer_module_id, _view_kinds

    dxf_dir = out_dir / "dxf"
    dxf_paths = ensure_dxf_batch(input_dir, dxf_dir)
    sheets_out = out_dir / "sheets"

    # Phase 2c：意图注册（overlay 未声明的 stem 由 sheet_intent 四分类
    # 补挂 view_regions）。失败不阻断（回退旧行为）。
    try:
        from ..intake.intent_router import register_sheet_intents
        register_sheet_intents(dxf_paths, layer_map_path)
    except Exception:
        pass

    mllm = MLLMBackend()
    if not mllm.available():
        raise RuntimeError(
            "agent_mode=hybrid 需要 MLLM API Key（KIMI_API_KEY / OPENAI_API_KEY 等），"
            "当前未配置；请先 export 或改用 agent_mode=ezdxf"
        )

    project = ProjectModel(project_id=pid, name=pid)
    sheet_models: Dict[str, EngineeringModel] = {}
    # 阶段 0.2：每张 sheet 的 steps.json 路径 + MLLM 上下文（run_manifest 来源）。
    pipelines: Dict[str, Dict[str, Any]] = {}
    failures: List[Dict[str, str]] = []
    dxf_list = sorted(dxf_paths)
    total = len(dxf_list)
    for i, dxf in enumerate(dxf_list, start=1):
        stem = Path(dxf).stem
        sheet_out = sheets_out / stem
        # P1 健壮性：单张 sheet 失败不得杀死整个批量交付。捕获异常、落盘
        # traceback、继续下一张，最后汇总到 project.metadata["sheet_failures"]。
        try:
            # 非空间段（detail 详图/模块页）不进入 3D 空间合并，仅用于件号追溯/BOM。
            # 降级：跳过 MLLM 几何+件号，只用 ezdxf 矢量 + DXF TEXT 件号（秒级），
            # 避免 dpi=800 下 detail 详图 MLLM 检测耗时数分钟却对塔身无贡献。
            mergeable = sheet_is_spatial_mergeable(stem, overlay=layer_map_path)
            from ..intake.tower_spec import resolve_geom_method_for_sheet
            sheet_geom = resolve_geom_method_for_sheet(
                stem, layer_map_path, mergeable=mergeable)
            pipe_info = run_hybrid_dxf_agent_pipeline(
                dxf, sheet_out,
                layer_map_path=str(layer_map_path) if layer_map_path else None,
                mllm=mllm,
                use_ocr_fallback=False,
                geom_method=sheet_geom,
                skip_mllm=not mergeable,
            )
            pipelines[stem] = {
                "steps_path": pipe_info.get("steps_path"),
                "mllm_provider": pipe_info.get("mllm_provider"),
                "mllm_model": pipe_info.get("mllm_model"),
            }
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            failures.append({"stem": stem, "error": f"{type(exc).__name__}: {exc}"})
            (sheet_out / "pipeline_error.log").write_text(tb, encoding="utf-8")
            print(f"[{i}/{total}] FAIL {stem}: {type(exc).__name__}: {exc}", flush=True)
            continue
        model_path = sheet_out / "model.json"
        if not model_path.exists():
            print(f"[{i}/{total}] skip {stem} (无 model.json)", flush=True)
            continue
        m = load_model(model_path)
        sheet_models[stem] = m
        kind = resolve_drawing_kind(stem, overlay=layer_map_path)
        role = kind.get("role") or canonical_sheet_role(kind["kind"])
        sheet = ProjectSheet(
            sheet_id=stem,
            path=str(dxf),
            kind=kind["kind"],
            role=role,
            spatial_mergeable=sheet_is_spatial_mergeable(stem, overlay=layer_map_path),
            module_id=_infer_module_id(stem, role=role),
            view_kinds=_view_kinds(m),
            model_path=str(model_path),
        )
        project.add_sheet(sheet)
        project.aggregate_evidence(m, stem)
        if sheet.module_id:
            project.register_module(sheet.module_id, stem, kind=kind["kind"], role=role)
        print(f"[{i}/{total}] ok {stem}", flush=True)

    if failures:
        project.metadata["sheet_failures"] = failures

    if bom_path:
        project.metadata["master_bom_path"] = str(bom_path)
    project.metadata["agent_mode"] = "hybrid"
    project_path = save_project(project, out_dir / "project.json")
    return project, project_path, sheet_models, pipelines


def _select_assembly(
    merged_model: EngineeringModel,
    layer_map_path: Optional[str | Path],
    ov: Dict[str, Any],
) -> tuple[Optional[Dict[str, Any]], bool]:
    """Phase 3 装配选择：真 M1-M6 优先，仅显式请求 demo 时才回退 z 拆分。

    清单 P1：真 M1-M6（module_definitions 已配置）装配失败时不得用
    assembly_demo_z_split 冒充成功——返回带 error 的 dict（enabled=False），
    由 deliver_project 判 failed（不闭合）。

    返回 (assembly_info, fallback_to_demo)。
    """
    from .module_build import try_assembly_from_merged, try_assembly_m1_m6_from_merged

    if isinstance(ov, dict) and ov.get("module_definitions"):
        info = try_assembly_m1_m6_from_merged(merged_model, layer_map_path)
        if info is None:
            info = {
                "model": None,
                "mode": "m1_m6_rigid_chain",
                "enabled": False,
                "error": "M1-M6 长链条装配未闭合（模块不足或刚性链失败），"
                         "拒绝回退 assembly_demo_z_split 冒充成功",
            }
        return info, False
    info = try_assembly_from_merged(merged_model, layer_map_path)
    return info, info is not None


def deliver_project(
    input_dir: str | Path,
    out_dir: str | Path,
    *,
    project_id: Optional[str] = None,
    layer_map_path: Optional[str | Path] = None,
    bom_path: Optional[str | Path] = None,
    export_glb: bool = True,
    agent_mode: str = "ezdxf",
) -> Dict[str, Any]:
    """图册级交付：ProjectModel + cross_file 合并 + Harness + GLB + manifest。

    agent_mode:
        * "ezdxf"  （默认）走纯矢量提取（extract_tower_from_dxf）。
        * "hybrid" 走 Kimi/MLLM Agent 链（A0 版面 → A2 几何 MLLM → A1 件号
          MLLM → A3 关联 → A4 Harness），每张 sheet 用 run_hybrid_dxf_agent_pipeline
          产出 model.json（含 view_x/view_y，MLLM 几何替换 ezdxf 垃圾几何）。
    """
    from ..intake.tower_batch import cross_file_bar_id_report, intake_tower_batch
    from ..intake.tower_spec import load_tower_spec, should_use_cross_file_merge
    from ..intake.tower_symmetry import expand_4_face_symmetry_model
    from ..io import load_model, save_model
    from ..solve.tower_solver import export_tower_glb, inspect_model, tower_geometry_gate, SolveError
    from .bar_inventory import aggregate_bar_inventory
    from .bom_tree import aggregate_bom_tree
    from .harness import run_project_harness

    input_dir = Path(input_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pid = project_id or input_dir.name
    ov = load_tower_spec(layer_map_path) if layer_map_path else {}

    resolved_bom = resolve_master_bom_path(input_dir, layer_map_path, bom_path)
    if bom_path is None and resolved_bom:
        bom_path = resolved_bom

    # 阶段 0.2：run_manifest 数据来源。
    #   * pipelines：hybrid 路径每张 sheet 的 steps.json + MLLM provider/model
    #   * ezdxf 路径无 MLLM / steps.json，对应字段在 manifest 中为 null
    pipelines: Dict[str, Dict[str, Any]] = {}
    if agent_mode == "hybrid":
        project, project_path, sheet_models, pipelines = _build_hybrid_project(
            input_dir, out_dir, pid,
            layer_map_path=layer_map_path, bom_path=bom_path,
        )
    else:
        project = build_project_from_directory(
            input_dir,
            pid,
            layer_map_path=layer_map_path,
            out_dir=out_dir / "sheets",
        )
        if bom_path:
            project.metadata["master_bom_path"] = str(bom_path)
        if ov.get("enable_module_assembly"):
            project.metadata["module_assembly_requested"] = True
        project_path = save_project(project, out_dir / "project.json")

    sheet_model_list: List[EngineeringModel] = []
    sheet_sources: List[str] = []
    if agent_mode != "hybrid":
        sheet_models = {}
        for sid, sheet in project.sheets.items():
            if sheet.model_path and Path(sheet.model_path).exists():
                m = load_model(sheet.model_path)
                m.name = sid
                sheet_models[sid] = m
                sheet_model_list.append(m)
                sheet_sources.append(sid)
    else:
        for sid, m in sheet_models.items():
            m.name = sid
            sheet_model_list.append(m)
            sheet_sources.append(sid)

    cross_result: Optional[Dict[str, Any]] = None
    model_path: Optional[Path] = None

    if agent_mode == "hybrid":
        # hybrid：直接用 MLLM 批跑产出的 sheet_models 做 cross-file 空间合并，
        # 不重新走 ezdxf 提取（MLLM 几何已是唯一来源）。
        from ..intake.tower_batch import merge_cross_file_views
        from ..intake.tower_pipeline import finalize_tower_model
        from ..intake.tower_spec import cross_file_merge_stems

        merge_stems = set(cross_file_merge_stems(layer_map_path))
        spatial_models = [
            m for sid, m in sheet_models.items() if sid in merge_stems
        ]
        cross_result = {
            "mode": "hybrid_cross_file",
            "merge_stems": sorted(merge_stems),
            "files": len(spatial_models),
            "merge_report": {"mode": "hybrid", "files": len(spatial_models)},
        }
        if spatial_models:
            merged = merge_cross_file_views(spatial_models, layer_map_path=str(layer_map_path))
            merged = finalize_tower_model(
                merged, bom_path=str(bom_path) if bom_path else None,
                merge=True, layer_map_path=str(layer_map_path),
            )
            model_path = out_dir / "cross_file" / "model.json"
            model_path.parent.mkdir(parents=True, exist_ok=True)
            save_model(merged, model_path)
            cross_result["model_path"] = str(model_path)
            cross_result["merge_report"].update({
                "bars": sum(1 for c in merged.components.values() if c.kind == "tower_bar"),
                # 2026-09-06：计数口径改为「x&z 已知」（进入 3D 链的节点）。
                # front_xz_fallback 节点（y=None 诚实 partial）会被四面展开
                # 物化为 solved，merge 时点只数 'solved' 会把它们漏报成
                # NO_NODES_SOLVED（Gemini hybrid 02 册实测 97 节点全 partial_xz
                # 但展开后 577 节点全 solved）。两个分支（hybrid/ezdxf）同改，
                # ezdxf 纯矢量节点 merge 时已 solved 且 x&z 非空，计数不变。
                "nodes_solved": sum(
                    1 for c in merged.components.values()
                    if c.kind == "tower_node"
                    and c.properties.get("x") is not None
                    and c.properties.get("z") is not None
                ),
            })
    elif should_use_cross_file_merge(layer_map_path):
        # P1-4：复用 build_project_from_directory 已 extract 的 per-sheet 模型
        # （sheet_models），直接做 cross-file 空间合并；严禁再走 cross_file_batch
        # 重新 ensure_dxf_batch + extract_tower_from_dxf（每张 DXF 只解析一次）。
        from ..intake.tower_batch import merge_cross_file_views
        from ..intake.tower_pipeline import finalize_tower_model
        from ..intake.tower_spec import cross_file_merge_stems

        merge_stems = set(cross_file_merge_stems(layer_map_path))
        spatial_models = [
            m for sid, m in sheet_models.items() if sid in merge_stems
        ]
        cross_result = {
            "mode": "cross_file_view",
            "merge_stems": sorted(merge_stems),
            "files": len(spatial_models),
            "merge_report": {"mode": "cross_file_view", "files": len(spatial_models)},
        }
        if spatial_models:
            merged = merge_cross_file_views(spatial_models, layer_map_path=str(layer_map_path))
            merged = finalize_tower_model(
                merged, bom_path=str(bom_path) if bom_path else None,
                merge=True, layer_map_path=str(layer_map_path),
            )
            model_path = out_dir / "cross_file" / "model.json"
            model_path.parent.mkdir(parents=True, exist_ok=True)
            save_model(merged, model_path)
            cross_result["model_path"] = str(model_path)
            cross_result["merge_report"].update({
                "bars": sum(1 for c in merged.components.values() if c.kind == "tower_bar"),
                # 2026-09-06：计数口径改为「x&z 已知」（进入 3D 链的节点）。
                # front_xz_fallback 节点（y=None 诚实 partial）会被四面展开
                # 物化为 solved，merge 时点只数 'solved' 会把它们漏报成
                # NO_NODES_SOLVED（Gemini hybrid 02 册实测 97 节点全 partial_xz
                # 但展开后 577 节点全 solved）。两个分支（hybrid/ezdxf）同改，
                # ezdxf 纯矢量节点 merge 时已 solved 且 x&z 非空，计数不变。
                "nodes_solved": sum(
                    1 for c in merged.components.values()
                    if c.kind == "tower_node"
                    and c.properties.get("x") is not None
                    and c.properties.get("z") is not None
                ),
            })
    else:
        batch = intake_tower_batch(
            input_dir, out_dir / "batch",
            layer_map_path=layer_map_path, merge=True,
        )
        cross_result = batch
        if batch.get("model_path"):
            model_path = Path(batch["model_path"])

    merged_model: Optional[EngineeringModel] = None
    if model_path and model_path.exists():
        merged_model = load_model(str(model_path))

    # Phase 2：单立面 -> 四面封闭空间网架（overlay 开启时自动展开）。
    # 展开会重写 tower_node / tower_bar 组件；BOM / 节点板 / 图纸上下文保留。
    if merged_model is not None and ov.get("enable_4_face_expansion"):
        # P2.4j：侧立面横杆直读（side_horiz_synth）——在四面展开前追加
        # （展开会重写 front 节点为四面镜像节点，hw 锥拟合需用展开前节点）。
        try:
            from ..intake.tower_views import side_horiz_synth
            _dxf_by_stem = {}
            for _base in (input_dir, input_dir / "_dxf_scope"):
                if _base.exists():
                    for _dp in sorted(_base.glob("*.dxf")):
                        _dxf_by_stem.setdefault(_dp.stem, str(_dp))
            if _dxf_by_stem:
                _n_h = side_horiz_synth(merged_model, layer_map_path, _dxf_by_stem)
                if _n_h:
                    _dfh = merged_model.components.get("drawing_file")
                    if _dfh is not None:
                        _dfh.properties.setdefault(
                            "side_horiz_synth_report", {"added": _n_h})
        except Exception as _exc_sh:
            # P1 修复（2026-09-05）：拒绝必须显式记录。失败原因写入
            # drawing_file.properties（落盘可审计），不再静默吞。
            _dfh = merged_model.components.get("drawing_file")
            if _dfh is not None:
                _dfh.properties.setdefault(
                    "side_horiz_synth_error",
                    {"error": f"{type(_exc_sh).__name__}: {_exc_sh}"})
            print(f"[P2.4j] side_horiz_synth 失败：{_exc_sh!r}")
        expand_4_face_symmetry_model(
            merged_model, layer_map_path,
            sheets_dir=out_dir / "sheets")
        # P2.4b（JC1）：side 直读杆注入——merge_view_bars 冻结的 side_reads
        # （side 画线 y/z + 面平面 x）在此处以全新组件落地：face='l' 直读
        # （side_direct→recognized）+ face='r' 镜像孪生（side_mirror→
        # reconstructed）。overlay 显式关闭（side_read_promotion=false）才跳过。
        if ov.get("side_read_promotion", True):
            try:
                from ..intake.tower_views import apply_side_reads
                _n_side = apply_side_reads(merged_model)
                _dfp = merged_model.components.get("drawing_file")
                if _dfp is not None and _n_side:
                    _dfp.properties.setdefault(
                        "side_read_promotion_report", {"injected": _n_side})
            except Exception as _exc_sr:
                # P1 修复（2026-09-05）：注入失败不阻断交付（side_reads
                # 冻结证据仍在 drawing_file），但拒绝必须显式记录——
                # 失败原因落盘可审计，不再静默吞。
                _dfp = merged_model.components.get("drawing_file")
                if _dfp is not None:
                    _dfp.properties.setdefault(
                        "side_read_promotion_error",
                        {"error": f"{type(_exc_sr).__name__}: {_exc_sr}"})
                print(f"[P2.4b] apply_side_reads 失败：{_exc_sr!r}")
            # P2（2026-09-05）：塔尖区 side 杆修剪。塔顶收尖段（四棱金字塔）
            # 的 side 画线被 face_plane 投影到假想竖直面——实测 JC1 z≥34200
            # 段 52 根 side 杆全 FP（真结构已由 tps/panel 链覆盖）。overlay
            # 指定尖段下界 z（side_lift_prune_above_z_mm）后按杆 z 中点剪除。
            _prune_z = ov.get("side_lift_prune_above_z_mm")
            if _prune_z is not None:
                _prune_z = float(_prune_z)
                _node_z: dict = {}
                for _cid, _comp in merged_model.components.items():
                    if _comp.kind == "tower_node":
                        _pp = _comp.properties or {}
                        if _pp.get("z") is not None:
                            _node_z[_cid] = float(_pp["z"])
                _rm = []
                for _cid, _comp in merged_model.components.items():
                    if _comp.kind != "tower_bar":
                        continue
                    _pr = _comp.properties or {}
                    if not _pr.get("side_promoted"):
                        continue
                    _za = _node_z.get(_pr.get("from_node"))
                    _zb = _node_z.get(_pr.get("to_node"))
                    if _za is None or _zb is None:
                        continue
                    if (_za + _zb) / 2.0 >= _prune_z:
                        _rm.append(_cid)
                for _cid in _rm:
                    del merged_model.components[_cid]
                _dfp = merged_model.components.get("drawing_file")
                if _dfp is not None and _rm:
                    _dfp.properties["side_lift_tip_prune_report"] = {
                        "pruned": len(_rm), "above_z_mm": _prune_z}
            # P2（2026-09-05）：合成 x 源剪除。x_source=z_pair 的 side 杆
            # x 完全由跨册 z 配对解算合成（非图面读数），实测 JC1 全部
            # 6 根皆 FP（含 4m 级塔顶尖刺）。overlay 列表指定后按源剪除。
            _drop_xs = ov.get("side_lift_drop_x_source") or []
            if _drop_xs:
                _drop_xs = {str(s) for s in _drop_xs}
                _rm2 = [
                    _cid for _cid, _comp in merged_model.components.items()
                    if _comp.kind == "tower_bar"
                    and (_comp.properties or {}).get("side_promoted")
                    and str((_comp.properties or {}).get("x_source")) in _drop_xs
                ]
                for _cid in _rm2:
                    del merged_model.components[_cid]
                _dfp = merged_model.components.get("drawing_file")
                if _dfp is not None and _rm2:
                    _dfp.properties["side_lift_xsource_prune_report"] = {
                        "pruned": len(_rm2), "x_source": sorted(_drop_xs)}
        # P3.20（ZC1）：同几何杆去重。多册同段重复出图 + 四面镜像展开
        # 产生完全相同几何的多份拷贝，Hungarian 1:1 评测下互抢 FP
        # （ZC1 实测 58% 重复）。overlay 显式开启才执行（默认关闭，
        # JC1/JC2 行为零变化）。
        if ov.get("dedup_identical_bars"):
            from ..solve.tower_geometry import dedup_identical_bars
            _dd = dedup_identical_bars(
                merged_model, tol_mm=float(ov.get("dedup_identical_tol_mm", 60.0)))
            _dfm = merged_model.components.get("drawing_file")
            if _dfm is not None:
                _dfm.properties["dedup_identical_bars_report"] = dict(_dd)

    # A1 证据集 BOM 白名单核验（2026-09-06）：识别件号与 master BOM 交叉
    # 核对，非 BOM 件号降级「待验证」（bar_id=UNLABELED_BOM_PENDING_*，原值
    # 留 bar_id_raw，A1 不进预测集）。必须放在 4-face expansion 之后——
    # apply_side_reads 注入的 sidegen 杆（side_direct/side_mirror）在展开
    # 阶段才挂 bar_id，核验早于注入会漏（实测 4 个 sidegen FP 漏网）。
    # overlay 显式开关控制，默认关闭保证既有口径零变化。
    if merged_model is not None and ov.get("bom_validate_bar_ids") and bom_path:
        try:
            import csv as _csv_bv
            from ..intake.tower_bom import cross_validate_bar_ids
            _bom_rows_bv = list(_csv_bv.DictReader(
                Path(bom_path).read_text(encoding="utf-8-sig").splitlines()))
            _bv_report = cross_validate_bar_ids(merged_model, _bom_rows_bv)
            print(
                f"[A1 BOM 核验] checked={_bv_report.get('n_checked')} "
                f"kept={_bv_report.get('n_kept')} pending={_bv_report.get('n_pending')}",
                flush=True,
            )
        except Exception as _exc_bv:
            print(f"[A1 BOM 核验] 失败（跳过，不影响交付）: {_exc_bv}", flush=True)

    # Phase 2.5（仅调试/评测）：GT 权威拓扑对齐。默认关闭，生产交付永不启用。
    # 阶段 0.2 GT 隔离：此路径只允许 overlay 显式 `gt_align: true`（debug/eval），
    # 且必须由 debug.gt_align 模块执行，每根替换杆件打 gt_aligned=True 标记。
    # 正式评测脚本检测到该标记时直接拒绝评测。
    gt_aligned = False
    if merged_model is not None and ov.get("gt_align"):
        canonical_tower_path = _resolve_canonical_tower_path(
            input_dir, ov, layer_map_path,
        )
        if canonical_tower_path and canonical_tower_path.exists():
            from ..debug.gt_align import align_skeleton_to_canonical
            from ..solve.canonical_tower import load_gt, load_from_mod
            node_file = None
            if canonical_tower_path.suffix.lower() == ".mod":
                if isinstance(ov, dict):
                    raw = ov.get("canonical_tower")
                    if isinstance(raw, dict) and raw.get("node_file"):
                        node_file = Path(str(raw["node_file"]))
                canonical = load_from_mod(canonical_tower_path, node_file=node_file, merge=False)
            else:
                canonical = load_gt(canonical_tower_path)
            align_skeleton_to_canonical(merged_model, canonical)
            gt_aligned = True

            # 图纸件号 ↔ 计算模型件号映射（仅用于调试/评测，不改变骨架语义）。
            # 用 section + 长度（容差 60mm）把 BOM 数字件号（105/108/...）映射到
            # GT 的 PM_XXXX 杆集合；主腿合并件按 4 象限 1:1 映射。
            try:
                from ..project.bar_id_mapping import build_bar_id_mapping
                import csv as _csv
                gt_dict = canonical.to_dict()
                bom_rows = []
                if bom_path and Path(bom_path).exists():
                    bom_rows = list(_csv.DictReader(Path(bom_path).read_text(encoding="utf-8-sig").splitlines()))
                _map_result = build_bar_id_mapping(gt_dict, bom_rows)
                _map_path = out_dir / "bar_id_mapping.json"
                _map_path.write_text(
                    json.dumps(_map_result, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as _exc:
                _map_path = None
                _map_error = str(_exc)

    physical_counts: Dict[str, int] = {}
    if merged_model is not None:
        physical_counts = physical_bar_counts(merged_model)

    # Phase A3：L0 权威塔源（overlay 配置驱动；缺失则不产出 L0，不拉低交付）
    canonical_tower_path = _resolve_canonical_tower_path(
        input_dir, ov, layer_map_path,
    )
    sheet_stats = _sheet_model_stats(sheet_model_list, sheet_sources)
    index_path = _write_index_artifact(out_dir, project, sheet_stats)

    bar_inventory = aggregate_bar_inventory(
        sheet_model_list, model_sources=sheet_sources,
    ) if sheet_model_list else {}
    cross_sheet_bar_id = cross_file_bar_id_report(sheet_model_list) if sheet_model_list else {}
    bom_tree = aggregate_bom_tree(
        sheet_model_list,
        master_bom_path=str(bom_path) if bom_path else None,
        model_sources=sheet_sources,
        physical_bar_counts=physical_counts or None,
    ) if sheet_model_list or physical_counts else {}

    assembly_info: Optional[Dict[str, Any]] = None
    assembly_fallback_to_demo = False
    if merged_model is not None:
        assembly_info, assembly_fallback_to_demo = _select_assembly(
            merged_model, layer_map_path, ov,
        )
        if assembly_info and assembly_info.get("model"):
            asm_path = out_dir / "assembly_model.json"
            save_model(assembly_info["model"], asm_path)
            assembly_info["model_path"] = str(asm_path)
            assembly_info["enabled"] = True

    project_harness = run_project_harness(
        project,
        sheet_models=sheet_models,
        cross_sheet_bar_id=cross_sheet_bar_id,
        bom_tree=bom_tree,
        bar_inventory=bar_inventory,
        assembly_info=assembly_info,
    )
    artifact_paths = _write_project_artifacts(
        out_dir,
        bar_inventory=bar_inventory,
        bom_tree=bom_tree,
        project_harness=project_harness,
    )

    harness: Optional[Dict[str, Any]] = None
    skeleton_gate: Optional[Dict[str, Any]] = None
    skeleton_glb_path: Optional[Path] = None
    canonical_glb_path: Optional[Path] = None
    assembly_glb_path: Optional[Path] = None
    skeleton_glb_error: Optional[str] = None
    assembly_glb_error: Optional[str] = None
    canonical_error: Optional[str] = None
    mesh_stats: Dict[str, int] = {}

    if merged_model is not None:
        harness = _harness_summary(merged_model)
        # 线1 verified delivery（2026-09-03）：人工复核豁免（显式、带指纹、
        # 有时效）。豁免的 pending 规则 → review_exempted（非 passed，
        # 报告可见），pending 清零后 deliver_status 才可能 verified。
        exemption_path = _load_review_exemptions(ov, layer_map_path)
        exemption_disclosure = _apply_review_exemptions(harness, exemption_path)
        harness["review_exemptions"] = exemption_disclosure
        save_model(merged_model, out_dir / "model.json")
        model_path = out_dir / "model.json"

        # P1（可选）：export 前仅保留最大连通子图杆件，剔除悬空短线/漂浮碎块。
        prune_cfg = (ov.get("prune_to_largest_component") if isinstance(ov, dict) else None)
        prune_before_gate = bool(prune_cfg.get("enabled")) if isinstance(prune_cfg, dict) else False
        pruned_bars = 0
        if prune_before_gate:
            from ..solve.tower_solver import keep_largest_connected_component
            pruned_bars = keep_largest_connected_component(merged_model)
            save_model(merged_model, out_dir / "model.json")

        # P3.15（JC2 泛化）：export 前清理悬空杆——引用不存在节点的
        # 杆件（4f_headx 链在 JC2 上锚层错位时产生 50 根悬空杆，
        # strict GLB 导出整体失败）。删杆不删节点，行为对 JC1 无影响
        # （JC1 模型无悬空杆）。
        _node_pos = {}
        for cid, c in merged_model.components.items():
            if c.kind == "tower_node":
                p = c.properties
                _node_pos[cid] = (p.get("x"), p.get("y"), p.get("z"))
        import math as _math
        _dangling = []
        for cid, c in merged_model.components.items():
            if c.kind != "tower_bar":
                continue
            f = _node_pos.get(c.properties.get("from_node"))
            t = _node_pos.get(c.properties.get("to_node"))
            if f is None or t is None:
                _dangling.append(cid)
                continue
            try:
                if _math.dist(f, t) < 1e-6:
                    _dangling.append(cid)
            except TypeError:
                _dangling.append(cid)
        if _dangling:
            for cid in _dangling:
                merged_model.components.pop(cid, None)
            # P1 修复（2026-09-05）：清理计数此前挂 merged_model.meta——
            # EngineeringModel 无 meta 字段、to_dict 不序列化，数字只活在
            # 内存里无法审计。改为写入 drawing_file.properties（随
            # save_model 落盘 model.json，事后可审计删了多少/哪些）。
            _dfd = merged_model.components.get("drawing_file")
            if _dfd is not None:
                _dfd.properties["dropped_dangling_bars"] = {
                    "count": len(_dangling),
                    "removed_ids": _dangling[:200],
                }
            save_model(merged_model, out_dir / "model.json")
            print(f"[P3.15] 清理悬空杆 {len(_dangling)} 根（export 前）")

        skeleton_gate = tower_geometry_gate(merged_model, layer_map_path)
        if prune_before_gate:
            skeleton_gate["pruned_bars"] = pruned_bars

        if export_glb:
            if not skeleton_gate["ok"]:
                skeleton_glb_error = "skeleton GLB 几何门禁未通过：" + "；".join(skeleton_gate["reasons"])
            else:
                skeleton_glb_path = out_dir / "skeleton.glb"
                try:
                    # Phase 4：交付 GLB 按几何来源分类着色（recognized 绿/
                    # reconstructed 蓝/collinear_stitch 黄/derived 灰），叠加
                    # review_queue 残留悬空节点红球。HANDOFF 3.2 分色清单含
                    # derived 灰，故用 qa_all 全量导出（含 internal helpers）。
                    # review_queue.json 由 scripts/generate_review_queue.py
                    # 生成（可能不存在，红球缺省为 0）。
                    export_tower_glb(
                        merged_model, skeleton_glb_path, strict=True,
                        mode="qa_all",
                        color_by="provenance",
                        review_queue_path=out_dir / "review_queue.json",
                    )
                    try:
                        import trimesh
                        scene = trimesh.load(str(skeleton_glb_path), force="scene")
                        mesh_stats["total_meshes"] = len(scene.geometry)
                    except Exception as exc:
                        # P4：trimesh 统计失败属非关键，记录而非静默吞。
                        mesh_stats["mesh_stats_error"] = str(exc)
                    bars = sum(1 for c in merged_model.components.values() if c.kind == "tower_bar")
                    gussets = sum(
                        1 for c in merged_model.components.values()
                        if c.kind == "gusset_plate" and c.properties.get("polygon_global")
                    )
                    bolts = sum(1 for c in merged_model.components.values() if c.kind == "bolt_group")
                    mesh_stats.update({"bars": bars, "gussets": gussets, "bolt_groups": bolts})
                except SolveError as exc:
                    skeleton_glb_error = str(exc)
                    skeleton_glb_path = None

                # P4：删除 tower.glb 兼容副本——图册交付主产物统一为 skeleton.glb。
                # web demo / 旧脚本已改为读 skeleton.glb，不再维护 tower.glb 别名。

        if export_glb and assembly_info and assembly_info.get("model"):
            assembly_glb_path = out_dir / "assembly.glb"
            try:
                export_tower_glb(assembly_info["model"], assembly_glb_path, strict=False)
            except Exception as exc:
                # 阶段 8.2：assembly GLB 导出失败必须显式传播为 failed，
                # 不得只设 path=None 后静默吞掉（enabled=True 时 assembly_failed
                # 不覆盖，需独立 assembly_glb_error 参与 has_failed）。
                assembly_glb_path = None
                assembly_glb_error = f"assembly GLB 导出失败：{exc}"
                assembly_info["error"] = assembly_info.get("error") or assembly_glb_error

    # ---- L0 权威塔：canonical.glb（只走 CanonicalTower，门禁与 skeleton 分开）----
    if export_glb and canonical_tower_path:
        try:
            from ..solve.canonical_tower import (
                load_gt,
                load_from_mod,
                export_glb as export_canonical_glb,
            )

            # P2：优先 GIM .mod（+ 计算 .NODE 单塔提纯）——这是 GT 的唯一权威几何源；
            # 无 .mod 时才回退到仓库内已提纯的 GT JSON。
            node_file = None
            if canonical_tower_path.suffix.lower() == ".mod":
                mod_path = canonical_tower_path
                if isinstance(ov, dict):
                    raw = ov.get("canonical_tower")
                    if isinstance(raw, dict) and raw.get("node_file"):
                        node_file = Path(str(raw["node_file"]))
                canonical = load_from_mod(mod_path, node_file=node_file, merge=False)
            else:
                canonical = load_gt(canonical_tower_path)
            canonical_glb_path = out_dir / "canonical.glb"
            export_canonical_glb(canonical, canonical_glb_path, strict=True)
        except Exception as exc:  # L0 失败不拉低 M3 交付
            canonical_error = str(exc)
            canonical_glb_path = None

    # ---- 二维详图 QA 平铺：detail_qa_atlas.glb（非真实 3D，仅目视检查 MLLM 几何）----
    detail_qa_atlas_info: Dict[str, Any] = {"present": False, "non_structural": True}
    if export_glb and sheet_models:
        try:
            detail_qa_atlas_path = out_dir / "detail_qa_atlas.glb"
            detail_qa_atlas_info = export_detail_qa_atlas(
                list(sheet_models.items()), detail_qa_atlas_path, overlay_path=layer_map_path,
            )
        except Exception as exc:
            detail_qa_atlas_info = {"present": False, "error": str(exc), "non_structural": True}

    mr = (cross_result or {}).get("merge_report") or {}
    nodes_solved = int(mr.get("nodes_solved") or 0)
    skeleton_glb_ok = (not export_glb) or (
        skeleton_glb_path is not None and skeleton_glb_path.exists() and not skeleton_glb_error
    )
    # A3：skeleton 门禁只评 M3 骨架本身；L0 canonical / M1 index 各自独立，
    # 不再用「完整塔」标准混评 DXF 骨架，也不因 L0 缺失判失败。
    skeleton_gate_ok = bool(skeleton_gate and skeleton_gate.get("ok"))

    # P0-2 失败传播：交付状态三态判定（verified / review_required / failed）。
    # 禁止沿用「没有 failed 就 ok」的模糊语义——必须逐项检查：
    #   * sheet_failures（任一张分册解析失败）
    #   * skeleton_gate（几何门禁未通过）
    #   * single_model_harness（单模型规则 pending / failed）
    #   * project_harness（图册规则 pending / failed）
    #   * assembly_closed（仅开启装配时，接口闭合失败）
    sheet_failures: List[Dict[str, str]] = list(
        (project.metadata or {}).get("sheet_failures") or []
    )
    single_failed = bool(harness and harness.get("failed"))
    single_pending = bool(harness and harness.get("pending"))
    proj_failed = bool(project_harness.get("failed"))
    proj_pending = bool(project_harness.get("pending"))

    # 装配闭合：仅当装配开启且存在装配报告时参与判定。
    assembly_enabled = bool(assembly_info and assembly_info.get("enabled"))
    assembly_reports = (assembly_info or {}).get("reports") or []
    assembly_closed = (assembly_info or {}).get("closed")
    assembly_failed = False
    if assembly_enabled and assembly_reports:
        assembly_failed = not bool(assembly_closed)
    # P1 修复：真 M1-M6 被请求但装配失败（assembly_info.error 且未 enabled），
    # 不得静默回退 demo 冒充成功——判 failed（不闭合）。
    if assembly_info and assembly_info.get("error") and not assembly_enabled:
        assembly_failed = True

    # 导出/门禁失败
    # 阶段 8：几何门禁独立于 GLB 导出。skeleton_gate_ok 检查的是模型本身的
    # 拓扑/几何正确性（悬空节点、退化杆件、连通分量），与是否导出 GLB 无关。
    # 因此几何门禁失败必须始终导致 failed，不能因 --no-glb 而被跳过。
    geometry_gate_failed = bool(skeleton_gate is not None and not skeleton_gate.get("ok"))
    export_failed = bool(skeleton_glb_error) or (export_glb and not skeleton_gate_ok)

    # 阶段 6.2 & 6.3: 显式失败与降级传播
    # - 任何必要 sheet 失败 / 几何门禁未通过 / 规则 failed -> 强置 failed (exit code 2)
    # - 存在降级回退 (degraded fallback) / 未匹配投影 / pending 审核 -> review_required (exit code 1)
    has_failed = bool(
        sheet_failures
        or single_failed
        or proj_failed
        or assembly_failed
        or export_failed
        or geometry_gate_failed
        or merged_model is None
        or nodes_solved <= 0
        # 阶段 8.2：assembly GLB 导出失败（含 enabled=True 场景）也必须判 failed
        or bool(assembly_glb_error)
    )
    # 阶段 5.3：未匹配投影（unresolved_projection_refs）不得静默通过——
    # 有跨视图身份未解出时降级为 review_required，供人工复核。
    unresolved_projection_count = 0
    half_width_degraded = False
    if merged_model is not None:
        _df = merged_model.components.get("drawing_file")
        if _df is not None:
            unresolved_projection_count = len(
                (_df.properties or {}).get("unresolved_projection_refs") or []
            )
            # 阶段 3.2：生产路径半宽拟合失败（half_width_degraded=True 且非 GT 注入）
            # 时，四面展开退化到 abs(t) 假深度，必须 review_required，禁止假装闭合。
            half_width_degraded = bool(
                (_df.properties or {}).get("half_width_degraded")
                and (_df.properties or {}).get("half_width_source") != "gt"
            )
    # 阶段 8.5：仍未解出三轴的节点必须参与状态判定（关键空间模型存在未解节点
    # → review_required），不能只写报告。这里提前计算，供 has_pending 使用。
    unsolved_nodes: List[str] = []
    if merged_model is not None:
        for cid, comp in merged_model.components.items():
            if comp.kind != "tower_node":
                continue
            p = comp.properties or {}
            if any(p.get(axis) is None for axis in ("x", "y", "z")):
                unsolved_nodes.append(cid)

    has_pending = bool(
        single_pending
        or proj_pending
        or (merged_model and getattr(merged_model, "degraded", False))
        or unresolved_projection_count > 0
        or half_width_degraded
        # 阶段 8.5：未解三轴节点存在时降级 review_required
        or len(unsolved_nodes) > 0
    )

    if has_failed:
        status = "failed"
        delivery_ok = False
    elif has_pending:
        status = "review_required"
        delivery_ok = False
    else:
        status = "verified"
        delivery_ok = True

    # P0.1（2026-08-31）结构化 failure_reasons：状态三态只回答「多严重」，
    # 不回答「哪里错」。这里把 has_failed / has_pending 的每个布尔分量展开成
    # {code, stage, message}，让「门禁通过但 deliver failed」这种表面矛盾
    # 可解释（几何门禁 OK + 证据校验 FAILED 同时成立是正常组合）。
    failure_reasons: List[Dict[str, str]] = []
    if sheet_failures:
        failure_reasons.append({
            "code": "SHEET_PARSE_FAILED",
            "stage": "intake",
            "message": f"{len(sheet_failures)} 张分册解析失败: "
                       f"{[s.get('sheet_id') or s for s in sheet_failures[:3]]}",
        })
    if geometry_gate_failed:
        failure_reasons.append({
            "code": "GEOMETRY_GATE_FAILED",
            "stage": "geometry_gate",
            "message": "几何门禁未通过（悬空节点/退化杆/连通分量超限）",
        })
    if single_failed:
        _fr = (harness or {}).get("failed") or []
        failure_reasons.append({
            "code": "EVIDENCE_VALIDATION_FAILED",
            "stage": "harness",
            "message": f"单模型规则失败: {_fr}",
        })
    if proj_failed:
        _pf = project_harness.get("failed") or []
        failure_reasons.append({
            "code": "PROJECT_VALIDATION_FAILED",
            "stage": "project_harness",
            "message": f"图册级规则失败: {_pf}",
        })
    if assembly_failed:
        failure_reasons.append({
            "code": "ASSEMBLY_NOT_CLOSED",
            "stage": "assembly",
            "message": "装配接口未闭合（模块间存在超差间隙）",
        })
    if export_failed:
        failure_reasons.append({
            "code": "GLB_EXPORT_FAILED",
            "stage": "export",
            "message": f"GLB 导出失败: {skeleton_glb_error or '门禁未通过'}",
        })
    if merged_model is None:
        failure_reasons.append({
            "code": "NO_3D_MODEL",
            "stage": "solve",
            "message": "空间合并未产出 3D 模型",
        })
    elif nodes_solved <= 0:
        failure_reasons.append({
            "code": "NO_NODES_SOLVED",
            "stage": "solve",
            "message": "3D 合并未解出任何节点",
        })
    if bool(assembly_glb_error):
        failure_reasons.append({
            "code": "ASSEMBLY_GLB_EXPORT_FAILED",
            "stage": "export",
            "message": f"装配 GLB 导出失败: {assembly_glb_error}",
        })
    # review_required 的原因（单独登记，不与 failed 混列）
    review_reasons: List[Dict[str, str]] = []
    if single_pending:
        review_reasons.append({
            "code": "RULES_PENDING",
            "stage": "harness",
            "message": f"待复核规则: {(harness or {}).get('pending') or []}",
        })
    if proj_pending:
        review_reasons.append({
            "code": "PROJECT_RULES_PENDING",
            "stage": "project_harness",
            "message": f"图册级待复核: {project_harness.get('pending') or []}",
        })
    if merged_model is not None and getattr(merged_model, "degraded", False):
        review_reasons.append({
            "code": "DEGRADED_FALLBACK",
            "stage": "solve",
            "message": "空间合并走了降级回退路径",
        })
    if unresolved_projection_count > 0:
        review_reasons.append({
            "code": "UNRESOLVED_PROJECTIONS",
            "stage": "solve",
            "message": f"{unresolved_projection_count} 处跨视图投影未匹配",
        })
    if half_width_degraded:
        review_reasons.append({
            "code": "HALF_WIDTH_DEGRADED",
            "stage": "geometry_fit",
            "message": "生产路径半宽拟合失败，四面展开退化到 abs(t) 假深度",
        })
    if unsolved_nodes:
        review_reasons.append({
            "code": "UNSOLVED_NODES",
            "stage": "solve",
            "message": f"{len(unsolved_nodes)} 个节点三轴坐标未解出",
        })

    # P0-3 报告：unsolved_nodes 已在 has_pending 判定前计算（见上方阶段 8.5），
    # 这里只汇总报告，不重复计算。
    unsolved_summary = {
        "count": len(unsolved_nodes),
        "sample": unsolved_nodes[:50],
    }
    topology_summary: Dict[str, Any] = {}
    if merged_model is not None:
        try:
            topology_summary = inspect_model(merged_model)
        except Exception:
            topology_summary = {}

    # 阶段 8.4：harness_all_passed 必须 failed=0 且 pending=0。
    # 单模型 harness 用 (not failed and not pending)，避免 review_required 却
    # harness_all_passed=True 的矛盾。
    harness_all_passed = bool(
        harness is not None
        and not (harness.get("failed"))
        and not (harness.get("pending"))
    )

    # Phase A3：三种产物分开登记，不再混评。
    products: List[Dict[str, Any]] = [
        {
            "layer": "L0",
            "id": "canonical.glb",
            "name": "权威完整塔（GIM/.NODE）",
            "path": str(canonical_glb_path) if canonical_glb_path and canonical_glb_path.exists() else None,
            "present": bool(canonical_glb_path and canonical_glb_path.exists()),
            "error": canonical_error,
            "source": str(canonical_tower_path) if canonical_tower_path else None,
        },
        {
            "layer": "M3",
            "id": "skeleton.glb",
            "name": "正交视图 3D 骨架（spatial_merge）",
            "path": str(skeleton_glb_path) if skeleton_glb_path and skeleton_glb_path.exists() else None,
            "present": bool(skeleton_glb_path and skeleton_glb_path.exists()),
            "error": skeleton_glb_error,
            "gate": skeleton_gate,
        },
        {
            "layer": "M1",
            "id": "index.json",
            "name": "全册分册索引（角色 + 解析统计）",
            "path": str(index_path),
            "present": index_path.exists(),
            "sheet_count": len(project.sheets),
        },
        {
            "layer": "QA",
            "id": "detail_qa_atlas.glb",
            "name": "二维详图分 Z 层平铺 QA 视图（非真实 3D）",
            "path": detail_qa_atlas_info.get("path"),
            "present": bool(detail_qa_atlas_info.get("present")),
            "error": detail_qa_atlas_info.get("error"),
            "non_structural": True,
        },
    ]
    glb_path_str = (
        str(skeleton_glb_path) if skeleton_glb_path and skeleton_glb_path.exists()
        else None
    )

    delivery = {
        "ok": delivery_ok,
        "status": status,
        # P0.1 结构化状态链：四个子阶段 ok 并列 + 结构化原因清单。
        # 「门禁通过但 status=failed」不再矛盾——几何门禁（gate.ok）与证据
        # 校验（validation.ok）各自独立，failed 必有 failure_reasons 条目。
        "stage_status": {
            "gate": {
                "ok": bool(not geometry_gate_failed),
                "reasons": (skeleton_gate or {}).get("reasons") or [],
            },
            "validation": {
                "ok": bool(not single_failed and not proj_pending and not single_pending and not proj_failed),
                "failed_rules": (harness or {}).get("failed") or [],
                "pending_rules": (harness or {}).get("pending") or [],
            },
            "export": {
                "ok": bool(not export_failed and not assembly_glb_error),
            },
            "evidence": {
                "ok": bool(not sheet_failures and not assembly_failed),
            },
        },
        "failure_reasons": failure_reasons,
        "review_reasons": review_reasons,
        # 阶段 0.2 GT 隔离：manifest 必须标明是否发生过 GT 对齐。
        # gt_aligned=True 的交付只可用于调试/评测对齐，正式评测应拒绝。
        "gt_aligned": gt_aligned,
        "sheet_failures": sheet_failures,
        "harness_all_passed": harness_all_passed,
        "project_harness_all_passed": project_harness.get("all_passed"),
        "project_id": pid,
        "project_path": str(project_path),
        "model_path": str(model_path) if model_path else None,
        # A3：三层产物路径（glb_path 保留兼容，指向 M3 骨架）
        "canonical_glb_path": str(canonical_glb_path) if canonical_glb_path and canonical_glb_path.exists() else None,
        "skeleton_glb_path": str(skeleton_glb_path) if skeleton_glb_path and skeleton_glb_path.exists() else None,
        "index_path": str(index_path),
        "glb_path": glb_path_str,
        "assembly_glb_path": str(assembly_glb_path) if assembly_glb_path and assembly_glb_path.exists() else None,
        "detail_qa_atlas": detail_qa_atlas_info,
        "products": products,
        "glb_error": skeleton_glb_error,
        "assembly_glb_error": assembly_glb_error,
        "canonical_error": canonical_error,
        "glb_geometry_gate": skeleton_gate,
        "unsolved_nodes": unsolved_summary,
        "topology": topology_summary,
        # 阶段 5.3：未匹配投影计数（>0 时降级 review_required）
        "unresolved_projection_refs": unresolved_projection_count,
        # 阶段 3.2：半宽拟合是否退化（true=生产路径走 abs(t) 假深度，已降级 review_required）
        "half_width_degraded": half_width_degraded,
        "mesh_stats": mesh_stats,
        "sheets": [sid for sid in project.sheets],
        "sheet_roles": {
            sid: {
                "role": sh.role,
                "spatial_mergeable": bool(sh.spatial_mergeable),
                "kind": sh.kind,
            }
            for sid, sh in project.sheets.items()
        },
        "spatial_merge_sheets": [
            sid for sid, sh in project.sheets.items() if sh.spatial_mergeable
        ],
        "modules": project.modules,
        "merge_report": mr,
        "harness": harness,
        "assembly": {
            "enabled": bool(assembly_info and assembly_info.get("enabled")),
            "mode": (assembly_info or {}).get("mode"),
            "reports": (assembly_info or {}).get("reports"),
            "model_path": (assembly_info or {}).get("model_path"),
            "module_ids": (assembly_info or {}).get("module_ids"),
            "closed": (assembly_info or {}).get("closed"),
            "max_gap_mm": (assembly_info or {}).get("max_gap_mm"),
        },
        "cross_file_batch_report": (cross_result or {}).get("batch_report"),
        "bar_inventory": bar_inventory,
        "physical_bar_counts": physical_counts,
        "bom_tree_summary": {
            "total_unique_bar_ids": bom_tree.get("total_unique_bar_ids", 0),
            "conflict_count": bom_tree.get("conflict_count", 0),
            "under_identified_count": bom_tree.get("under_identified_count", 0),
            "fittings_skipped": len(bom_tree.get("fittings_skipped") or []),
            "only_in_master": len(bom_tree.get("only_in_master") or []),
            "only_in_model": len(bom_tree.get("only_in_model") or []),
            "master_bom_path": str(bom_path) if bom_path else None,
        },
        "bom_conflicts": (bom_tree.get("conflicts") or [])[:50],
        "bom_under_identified": (bom_tree.get("under_identified") or [])[:50],
        "cross_sheet_bar_id": {
            "duplicate_count": cross_sheet_bar_id.get("duplicate_count", 0),
            "cross_file_groups": (cross_sheet_bar_id.get("cross_file_groups") or [])[:20],
        },
        "project_harness": project_harness,
        "artifact_paths": artifact_paths,
    }

    # ---- 阶段 0.2：run_manifest.json（运行清单，尽力而为，不中断主管线）----
    # 输入哈希 / MLLM 上下文与视觉缓存 / 每 sheet 阶段计数 / 输出文件 / 杆件
    # 变更事件全部集中由 build_run_manifest() 纯函数组装；取不到的字段为 null。
    run_manifest_path: Optional[str] = None
    try:
        steps_by_stem = {
            stem: info.get("steps_path")
            for stem, info in pipelines.items()
            if info.get("steps_path")
        }
        mllm_provider = next(
            (info.get("mllm_provider") for info in pipelines.values()
             if info.get("mllm_provider")),
            None,
        )
        mllm_model = next(
            (info.get("mllm_model") for info in pipelines.values()
             if info.get("mllm_model")),
            None,
        )
        output_candidates: List[Path] = [
            out_dir / "project.json",
            out_dir / "index.json",
            out_dir / "model.json",
            out_dir / "cross_file" / "model.json",
            out_dir / "skeleton.glb",
            out_dir / "assembly_model.json",
            out_dir / "assembly.glb",
            out_dir / "canonical.glb",
            out_dir / "detail_qa_atlas.glb",
            out_dir / "bar_inventory.json",
            out_dir / "bom_tree.json",
            out_dir / "project_harness.json",
            out_dir / "batch" / "batch_report.json",
            out_dir / "batch" / "model.json",
            *sorted((out_dir / "sheets").glob("*/model.json")),
            *sorted((out_dir / "sheets").glob("*/steps.json")),
        ]
        run_manifest = build_run_manifest(
            project_id=pid,
            input_dir=input_dir,
            overlay_path=layer_map_path,
            bom_path=bom_path,
            out_dir=out_dir,
            sheet_ids=list(project.sheets.keys()),
            sheet_stats=sheet_stats,
            merged_model=merged_model,
            steps_by_stem=steps_by_stem,
            merge_report=mr,
            output_candidates=output_candidates,
            mllm_provider=mllm_provider,
            mllm_model=mllm_model,
        )
        # Phase 2c：意图路由审计块（elevation/plan/detail stem 分类结果，
        # run_manifest 可复核「分类驱动管线选择」的判定留痕）。
        try:
            from ..intake.intent_router import registration_report
            run_manifest["sheet_intent_routing"] = registration_report(
                layer_map_path)
        except Exception:
            pass
        # 这两个文件都由本次运行写出（project_delivery.json 紧随其后落盘），
        # 不受 collect_outputs 存在性过滤的影响，如实列入。
        run_manifest["outputs"] = sorted(
            set(run_manifest.get("outputs") or [])
            | {"project_delivery.json", "run_manifest.json"}
        )
        run_manifest_path = write_run_manifest(run_manifest, out_dir)
        delivery["run_id"] = run_manifest["run_id"]
        delivery["run_manifest_path"] = run_manifest_path
    except Exception as exc:
        # manifest 构建/落盘失败只 warning，绝不中断主管线。
        warnings.warn(f"run_manifest 构建失败（不中断主管线）：{exc}")

    manifest_path = out_dir / "project_delivery.json"
    manifest_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8")
    delivery["manifest_path"] = str(manifest_path)
    if skeleton_glb_error:
        delivery["ok"] = False
        delivery["status"] = "failed"
    return delivery
