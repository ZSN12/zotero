"""图册级一键交付（M6 / Gap 1）。

build-project → cross_file_batch → Harness → strict GLB → 交付 manifest。
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


def _try_module_assembly(
    project: ProjectModel,
    sheet_models: Dict[str, EngineeringModel],
    overlay: Optional[str | Path | dict],
) -> Optional[Dict[str, Any]]:
    """多 module_id 且 overlay.enable_module_assembly 时尝试 Z 向拼装。"""
    from ..intake.tower_spec import load_tower_spec
    from .assembly import assemble_modules

    ov = load_tower_spec(overlay) if overlay else {}
    if not ov.get("enable_module_assembly"):
        return None
    modules = [
        mid for mid, meta in project.modules.items()
        if len(meta.get("sheets") or []) >= 1
    ]
    if len(modules) < 2:
        return None

    ordered: List[EngineeringModel] = []
    for mid in sorted(modules):
        stems = project.modules[mid].get("sheets") or []
        for stem in stems:
            m = sheet_models.get(stem)
            if m is not None:
                m.name = f"module-{mid}-{stem}"
                ordered.append(m)
                break
    if len(ordered) < 2:
        return None

    solved_counts = [
        sum(
            1 for c in m.components.values()
            if c.kind == "tower_node" and c.properties.get("solve_status") == "solved"
        )
        for m in ordered
    ]
    if any(n == 0 for n in solved_counts):
        return None

    merged, reports = assemble_modules(ordered, tol_mm=float(ov.get("assembly_tol_mm") or 10.0))
    return {
        "model": merged,
        "reports": reports,
        "module_ids": modules,
    }


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
    from ..intake.tower_batch import cross_file_batch, intake_tower_batch
    from ..intake.tower_spec import should_use_cross_file_merge
    from ..io import load_model, save_model
    from ..solve.tower_solver import export_tower_glb, SolveError

    input_dir = Path(input_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pid = project_id or input_dir.name

    project = build_project_from_directory(
        input_dir,
        pid,
        layer_map_path=layer_map_path,
        out_dir=out_dir / "sheets",
    )
    project_path = save_project(project, out_dir / "project.json")

    sheet_models: Dict[str, EngineeringModel] = {}
    for sid, sheet in project.sheets.items():
        if sheet.model_path and Path(sheet.model_path).exists():
            sheet_models[sid] = load_model(sheet.model_path)

    cross_result: Optional[Dict[str, Any]] = None
    model_path: Optional[Path] = None
    assembly_info: Optional[Dict[str, Any]] = None

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

    assembly_info = _try_module_assembly(project, sheet_models, layer_map_path)
    if assembly_info and assembly_info.get("model"):
        asm_path = out_dir / "assembly_model.json"
        save_model(assembly_info["model"], asm_path)
        assembly_info["model_path"] = str(asm_path)

    merged_model: Optional[EngineeringModel] = None
    if model_path and model_path.exists():
        merged_model = load_model(str(model_path))

    harness: Optional[Dict[str, Any]] = None
    glb_path: Optional[Path] = None
    glb_error: Optional[str] = None
    mesh_stats: Dict[str, int] = {}

    if merged_model is not None:
        harness = _harness_summary(merged_model)
        save_model(merged_model, out_dir / "model.json")
        model_path = out_dir / "model.json"

        if export_glb:
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

    mr = (cross_result or {}).get("merge_report") or {}
    nodes_solved = int(mr.get("nodes_solved") or 0)
    glb_ok = (not export_glb) or (glb_path is not None and glb_path.exists() and not glb_error)
    delivery_ok = merged_model is not None and nodes_solved > 0 and glb_ok
    harness_all_passed = harness is not None and not (harness.get("failed"))
    delivery = {
        "ok": delivery_ok,
        "harness_all_passed": harness_all_passed,
        "project_id": pid,
        "project_path": str(project_path),
        "model_path": str(model_path) if model_path else None,
        "glb_path": str(glb_path) if glb_path and glb_path.exists() else None,
        "glb_error": glb_error,
        "mesh_stats": mesh_stats,
        "sheets": [sid for sid in project.sheets],
        "modules": project.modules,
        "merge_report": mr,
        "harness": harness,
        "assembly": {
            "enabled": assembly_info is not None,
            "reports": (assembly_info or {}).get("reports"),
            "model_path": (assembly_info or {}).get("model_path"),
        },
        "cross_file_batch_report": (cross_result or {}).get("batch_report"),
    }
    manifest_path = out_dir / "project_delivery.json"
    manifest_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2), encoding="utf-8")
    delivery["manifest_path"] = str(manifest_path)
    if glb_error:
        delivery["ok"] = False
    return delivery
