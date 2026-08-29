"""图册级一键交付（M6 / Gap 1）。

build-project → cross_file_batch → Harness → strict GLB → 交付 manifest。
M7：图册级 Project Harness + 件号索引 + BOM 树汇总。
M8：master BOM 物理件号核对 + 模块装配 demo + Web 工作台增强。
"""

from __future__ import annotations

import json
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
) -> tuple[ProjectModel, str, Dict[str, EngineeringModel]]:
    """agent_mode="hybrid"：用 Kimi/MLLM Agent 链跑每张 sheet，构建 Project 索引。

    每张 sheet 用 run_hybrid_dxf_agent_pipeline 产出 model.json（MLLM 几何替换
    ezdxf 垃圾几何、节点带 view_x/view_y + view_type=front），再登记进
    ProjectModel（复用 build_project_from_directory 的索引/模块/证据聚合逻辑）。
    返回 (project, project_path, {sheet_id: EngineeringModel})。
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

    mllm = MLLMBackend()
    if not mllm.available():
        raise RuntimeError(
            "agent_mode=hybrid 需要 MLLM API Key（KIMI_API_KEY / OPENAI_API_KEY 等），"
            "当前未配置；请先 export 或改用 agent_mode=ezdxf"
        )

    project = ProjectModel(project_id=pid, name=pid)
    sheet_models: Dict[str, EngineeringModel] = {}
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
            run_hybrid_dxf_agent_pipeline(
                dxf, sheet_out,
                layer_map_path=str(layer_map_path) if layer_map_path else None,
                mllm=mllm,
                use_ocr_fallback=False,
                geom_method="ezdxf" if not mergeable else "auto",
                skip_mllm=not mergeable,
            )
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
    return project, project_path, sheet_models


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

    if agent_mode == "hybrid":
        project, project_path, sheet_models = _build_hybrid_project(
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
                "nodes_solved": sum(
                    1 for c in merged.components.values()
                    if c.kind == "tower_node" and c.properties.get("solve_status") == "solved"
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
                "nodes_solved": sum(
                    1 for c in merged.components.values()
                    if c.kind == "tower_node" and c.properties.get("solve_status") == "solved"
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
        expand_4_face_symmetry_model(merged_model, layer_map_path)

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
    canonical_error: Optional[str] = None
    mesh_stats: Dict[str, int] = {}

    if merged_model is not None:
        harness = _harness_summary(merged_model)
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

        skeleton_gate = tower_geometry_gate(merged_model, layer_map_path)
        if prune_before_gate:
            skeleton_gate["pruned_bars"] = pruned_bars

        if export_glb:
            if not skeleton_gate["ok"]:
                skeleton_glb_error = "skeleton GLB 几何门禁未通过：" + "；".join(skeleton_gate["reasons"])
            else:
                skeleton_glb_path = out_dir / "skeleton.glb"
                try:
                    export_tower_glb(merged_model, skeleton_glb_path, strict=True)
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
            except Exception:
                assembly_glb_path = None

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
    export_failed = bool(skeleton_glb_error) or (export_glb and not skeleton_gate_ok)

    has_failed = bool(
        sheet_failures
        or single_failed
        or proj_failed
        or assembly_failed
        or export_failed
        or merged_model is None
        or nodes_solved <= 0
    )
    has_pending = bool(single_pending or proj_pending)

    if has_failed:
        status = "failed"
        delivery_ok = False
    elif has_pending:
        status = "review_required"
        delivery_ok = False
    else:
        status = "verified"
        delivery_ok = True

    # P0-3 报告：仍未解出三轴的节点写入 delivery，供人工复核（strict export 的前置卡点）。
    unsolved_nodes: List[str] = []
    if merged_model is not None:
        for cid, comp in merged_model.components.items():
            if comp.kind != "tower_node":
                continue
            p = comp.properties or {}
            if any(p.get(axis) is None for axis in ("x", "y", "z")):
                unsolved_nodes.append(cid)
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

    harness_all_passed = harness is not None and not (harness.get("failed"))

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
        "canonical_error": canonical_error,
        "glb_geometry_gate": skeleton_gate,
        "unsolved_nodes": unsolved_summary,
        "topology": topology_summary,
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
            "only_in_master": len(bom_tree.get("only_in_master") or []),
            "only_in_model": len(bom_tree.get("only_in_model") or []),
            "master_bom_path": str(bom_path) if bom_path else None,
        },
        "bom_conflicts": (bom_tree.get("conflicts") or [])[:50],
        "cross_sheet_bar_id": {
            "duplicate_count": cross_sheet_bar_id.get("duplicate_count", 0),
            "cross_file_groups": (cross_sheet_bar_id.get("cross_file_groups") or [])[:20],
        },
        "project_harness": project_harness,
        "artifact_paths": artifact_paths,
    }
    manifest_path = out_dir / "project_delivery.json"
    manifest_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8")
    delivery["manifest_path"] = str(manifest_path)
    if skeleton_glb_error:
        delivery["ok"] = False
        delivery["status"] = "failed"
    return delivery
