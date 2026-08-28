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


def _harness_summary(model: EngineeringModel) -> Dict[str, Any]:
    results = run_harness(model)
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    failed = [r.target_id for r in results if r.status.value == "failed"]
    return {
        "counts": counts,
        "failed": failed,
        "results": [
            {"rule": r.target_id, "status": r.status.value, "message": r.message}
            for r in results
        ],
    }


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


def deliver_project(
    input_dir: str | Path,
    out_dir: str | Path,
    *,
    project_id: Optional[str] = None,
    layer_map_path: Optional[str | Path] = None,
    bom_path: Optional[str | Path] = None,
    export_glb: bool = True,
) -> Dict[str, Any]:
    """图册级交付：ProjectModel + cross_file 合并 + Harness + GLB + manifest。"""
    from ..intake.tower_batch import cross_file_batch, cross_file_bar_id_report, intake_tower_batch
    from ..intake.tower_spec import load_tower_spec, should_use_cross_file_merge
    from ..intake.tower_views import expand_4_face_symmetry_model
    from ..io import load_model, save_model
    from ..solve.tower_solver import export_tower_glb, inspect_model, tower_geometry_gate, SolveError
    from .bar_inventory import aggregate_bar_inventory
    from .bom_tree import aggregate_bom_tree
    from .harness import run_project_harness
    from .module_build import (
        physical_bar_counts,
        resolve_master_bom_path,
        try_assembly_from_merged,
        try_assembly_m1_m6_from_merged,
    )

    input_dir = Path(input_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pid = project_id or input_dir.name
    ov = load_tower_spec(layer_map_path) if layer_map_path else {}

    resolved_bom = resolve_master_bom_path(input_dir, layer_map_path, bom_path)
    if bom_path is None and resolved_bom:
        bom_path = resolved_bom

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

    sheet_models: Dict[str, EngineeringModel] = {}
    sheet_model_list: List[EngineeringModel] = []
    sheet_sources: List[str] = []
    for sid, sheet in project.sheets.items():
        if sheet.model_path and Path(sheet.model_path).exists():
            m = load_model(sheet.model_path)
            m.name = sid
            sheet_models[sid] = m
            sheet_model_list.append(m)
            sheet_sources.append(sid)

    cross_result: Optional[Dict[str, Any]] = None
    model_path: Optional[Path] = None

    if should_use_cross_file_merge(layer_map_path):
        cross_result = cross_file_batch(
            input_dir,
            out_dir / "cross_file",
            layer_map_path=layer_map_path,
            bom_path=bom_path,
        )
        if cross_result.get("model_path"):
            model_path = Path(cross_result["model_path"])
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

    physical_counts: Dict[str, int] = {}
    if merged_model is not None:
        physical_counts = physical_bar_counts(merged_model)

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
    if merged_model is not None:
        # Phase 3：优先 M1–M6 长链条装配；否则回退 Gap 1 的 M1/M2 z 拆分 demo。
        assembly_info = try_assembly_m1_m6_from_merged(merged_model, layer_map_path)
        if assembly_info is None:
            assembly_info = try_assembly_from_merged(merged_model, layer_map_path)
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
    geometry_gate: Optional[Dict[str, Any]] = None
    glb_path: Optional[Path] = None
    assembly_glb_path: Optional[Path] = None
    glb_error: Optional[str] = None
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

        geometry_gate = tower_geometry_gate(merged_model, layer_map_path)
        if prune_before_gate:
            geometry_gate["pruned_bars"] = pruned_bars

        if export_glb:
            if not geometry_gate["ok"]:
                glb_error = "GLB 几何门禁未通过：" + "；".join(geometry_gate["reasons"])
                glb_path = None
            else:
                glb_path = out_dir / "tower.glb"
                try:
                    export_tower_glb(merged_model, glb_path, strict=True)
                    try:
                        import trimesh
                        scene = trimesh.load(str(glb_path), force="scene")
                        mesh_stats["total_meshes"] = len(scene.geometry)
                    except Exception:
                        pass
                    bars = sum(1 for c in merged_model.components.values() if c.kind == "tower_bar")
                    gussets = sum(
                        1 for c in merged_model.components.values()
                        if c.kind == "gusset_plate" and c.properties.get("polygon_global")
                    )
                    bolts = sum(1 for c in merged_model.components.values() if c.kind == "bolt_group")
                    mesh_stats.update({"bars": bars, "gussets": gussets, "bolt_groups": bolts})
                except SolveError as exc:
                    glb_error = str(exc)
                    glb_path = None

        if export_glb and assembly_info and assembly_info.get("model"):
            assembly_glb_path = out_dir / "assembly.glb"
            try:
                export_tower_glb(assembly_info["model"], assembly_glb_path, strict=False)
            except Exception:
                assembly_glb_path = None

    mr = (cross_result or {}).get("merge_report") or {}
    nodes_solved = int(mr.get("nodes_solved") or 0)
    glb_ok = (not export_glb) or (glb_path is not None and glb_path.exists() and not glb_error)
    # P1：delivery.ok 显式绑定 3D 几何门禁；gate 不过即使 nodes_solved>0 也判失败。
    gate_ok = bool(geometry_gate and geometry_gate.get("ok"))
    delivery_ok = merged_model is not None and nodes_solved > 0 and glb_ok and (not export_glb or gate_ok)

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
    delivery = {
        "ok": delivery_ok,
        "harness_all_passed": harness_all_passed,
        "project_harness_all_passed": project_harness.get("all_passed"),
        "project_id": pid,
        "project_path": str(project_path),
        "model_path": str(model_path) if model_path else None,
        "glb_path": str(glb_path) if glb_path and glb_path.exists() else None,
        "assembly_glb_path": str(assembly_glb_path) if assembly_glb_path and assembly_glb_path.exists() else None,
        "glb_error": glb_error,
        "glb_geometry_gate": geometry_gate,
        "unsolved_nodes": unsolved_summary,
        "topology": topology_summary,
        "mesh_stats": mesh_stats,
        "sheets": [sid for sid in project.sheets],
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
    if glb_error:
        delivery["ok"] = False
    return delivery
