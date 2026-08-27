"""TowerHarness 多步编排（P0-1）。

把散落的 CLI 步骤编排成一条命令：
    intake → compile → cross_check → verify → retry → export

每步状态写入 ProcessingGraph（P0-2），最终产出 steps.json。
失败可 --retry 重跑 verify，或 --human-review 把 pending/failed 项标记为
人工复核（写回模型 message 与复核标记维度），便于交付前定位。

复用现有单步能力（不另起炉灶）：
    * intake:  traceability.intake.tower_dxf / tower_layout / mllm_backend
    * compile: traceability.intake.tower_pipeline.finalize_tower_model
    * verify:  traceability.harness.harness.run_harness + io.validate_references
    * solve:   traceability.solve.tower_solver.solve_tower
    * export:  traceability.export.exporters + tower_solver GLB/OBJ
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..io import load_model, save_model, validate_references
from ..model import EngineeringModel, ValidationStatus
from .harness import run_harness, summarize
from .processing_graph import ProcessingGraph


def _n(model: EngineeringModel, kind: str) -> int:
    return sum(1 for c in model.components.values() if c.kind == kind)


def _dir_has_cad_files(d: Path) -> bool:
    """目录里是否含 DWG/DXF（决定批量走矢量管线还是扫描管线）。"""
    cad_exts = {".dwg", ".dxf"}
    for p in d.iterdir():
        if p.is_file() and p.suffix.lower() in cad_exts:
            return True
    return False


def build_intake_model(
    source: str | Path,
    layer_map_path: Optional[str | Path] = None,
    backend: Optional[str] = None,
    merge: bool = False,
    bom_path: Optional[str | Path] = None,
    scale: Optional[str] = None,
    mm_per_px: Optional[float] = None,
) -> EngineeringModel:
    """根据文件类型选择 intake 后端，返回未 finalize 的模型。

    * .dxf/.dwg -> tower_dxf 规则解析（dwg 先转换）
    * .png/.jpg/.jpeg/.pdf -> tower+scan 优先 MLLM（配 API 时），
      无 API 降级 rule-based-scan（霍夫线检测）
    """
    source = str(source)
    suffix = Path(source).suffix.lower()
    if suffix in (".dxf", ".dwg"):
        from ..intake.tower_dxf import extract_tower_from_dxf
        path = source
        if suffix == ".dwg":
            from ..intake.dwg import ensure_dxf
            path = ensure_dxf(source)
        return extract_tower_from_dxf(path, layer_map_path=layer_map_path)

    # 栅格 / PDF
    from ..intake.mllm_backend import DrawingInput, choose_backend, MLLMBackend

    kind = "pdf" if suffix == ".pdf" else ("scan" if suffix in (".png", ".jpg", ".jpeg") else "scan")
    drawing = DrawingInput(path=source, kind=kind, tower=True)
    mllm = MLLMBackend()
    chosen = choose_backend(drawing, mllm=mllm)
    if backend == "mllm":
        chosen = mllm
    elif backend == "rule-based-scan":
        from ..intake.mllm_backend import TowerScanBackend
        chosen = TowerScanBackend()

    if getattr(chosen, "name", "") == "mllm":
        from ..intake.mllm_backend import MLLMAnalysisError
        candidate = chosen.analyze(drawing)
        if not candidate.objects:
            raise MLLMAnalysisError(
                f"MLLM 未产出候选：{candidate.raw}",
                meta=candidate.meta, raw=candidate.raw, warnings=candidate.warnings,
            )
        from ..skill.contract import to_engineering_model
        model = to_engineering_model(candidate, name=f"tower-{Path(source).stem}")
        # 把 MLLM 调用日志挂到模型上，run_tower 会写入 steps.json
        model.mllm_meta = candidate.meta
        model.mllm_warnings = candidate.warnings
        # 铁塔规则注入在 finalize 中完成
        return model

    # rule-based-scan 路径（含 PDF 先栅格化）
    from ..intake.tower_layout import analyze_tower_scan
    if suffix == ".pdf":
        from ..intake.pdf_raster import rasterize_pdf_to_png
        raster = rasterize_pdf_to_png(source)
    else:
        raster = source
    return analyze_tower_scan(raster, scale=scale, mm_per_px=mm_per_px)


def mark_human_review(model: EngineeringModel) -> EngineeringModel:
    """--human-review 标记：pending/failed 的规则/连接标为人工复核。"""
    n = 0
    for rule in model.rules.values():
        if rule.status in (ValidationStatus.PENDING, ValidationStatus.FAILED):
            rule.message = f"HUMAN_REVIEW: {rule.message or '待人工复核'}"
            rule.status = ValidationStatus.PENDING
            n += 1
    for conn in model.connections.values():
        if conn.validation_status in (ValidationStatus.PENDING, ValidationStatus.FAILED):
            conn.validation_status = ValidationStatus.PENDING
            n += 1
    from ..model import Dimension, DimensionOrigin, SourceRef, SourceType
    model.add_dimension(Dimension(
        id="dim_human_review",
        name="待人工复核项数量",
        value=n,
        unit="items",
        origin=DimensionOrigin.DERIVED,
        source=SourceRef(SourceType.UNKNOWN, "tower-harness", detail="--human-review 标记", confidence=1.0),
        applies_to="drawing_file" if "drawing_file" in model.components else None,
    ))
    return model


def run_tower(
    source: str | Path,
    out_dir: str | Path,
    bom_path: Optional[str | Path] = None,
    merge: bool = False,
    golden_path: Optional[str | Path] = None,
    layer_map_path: Optional[str | Path] = None,
    backend: Optional[str] = None,
    retry: bool = False,
    human_review: bool = False,
    allow_scan: bool = False,
    allow_derived_y: bool = False,
    format: str = "glb",
    scale: Optional[str] = None,
    mm_per_px: Optional[float] = None,
    input_dir: Optional[str | Path] = None,
    mllm: Optional[Any] = None,
    use_ocr_fallback: bool = True,
) -> Dict[str, Any]:
    """一步命令跑完全链：intake → compile → cross_check → verify → retry → export。

    支持：
        * 单文件 .dxf/.dwg/.png/.pdf（沿用旧路径）
        * --input-dir / 目录输入：批量转换 + 逐文件 intake（A3/F2），
          可 --merge 合并为一个 EngineeringModel（B7）
        * 图签/明细类文件（drawing_kind=title_block/bom）只 intake + 报告，
          不进入 3D 求解/导出，不计为失败（B2）
        * --retry 对 failed 规则重跑，仍失败则降级 human_review（F1）
        * 无 --golden 时正常结束并写报告，不做金标准对比（C2）

    返回 {"model_path", "steps_path", "summary_path", "glb_path", "graph", "ok"}。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(source)
    batch_source: Optional[Path] = None
    if input_dir is not None:
        batch_source = Path(input_dir)
    elif source_path.is_dir():
        batch_source = source_path

    # ============ 无 DXF 扫描图主路径：A0→A4 多 Agent 链（P1） ============
    # 单文件 PNG/PDF 走 agent 链；目录里全是 PNG/PDF 走扫描批量；
    # 目录里是 DWG 仍走 DWG→DXF 规则管线。DXF/DWG 永远不走 MLLM。
    if batch_source is None:
        suffix = source_path.suffix.lower()
        if suffix in (".png", ".jpg", ".jpeg", ".pdf"):
            from ..intake.tower_agent_pipeline import run_tower_agent_pipeline
            return run_tower_agent_pipeline(
                source, out_dir, mllm=mllm,
                scale=scale, mm_per_px=mm_per_px,
                use_ocr_fallback=use_ocr_fallback,
            )
    else:
        # P1-2：目录识别 —— 全是位图/PDF 走扫描批量，DWG 走现有 batch
        from ..intake.tower_scan_views import scan_dir_files
        grouped = scan_dir_files(batch_source)
        if grouped["all_files"] and not _dir_has_cad_files(batch_source):
            from ..intake.tower_scan_views import intake_scan_batch
            result = intake_scan_batch(batch_source, out_dir, mllm=mllm)
            # P1-9：直接复用 intake_scan_batch 产出的完整 ProcessingGraph
            # （每文件一步 + merge_scan + a4_harness），不再返回空壳 graph。
            return {
                "ok": result.get("ok", False),
                "graph": result.get("graph", ProcessingGraph(name=f"tower-scan-batch-{source_path.stem}")),
                "model_path": result.get("model_path"),
                "steps_path": result.get("steps_path"),
                "summary_path": None,
                "glb_path": None,
            }

    graph = ProcessingGraph(name=f"tower-{source_path.stem}")
    model_path = out_dir / "model.json"
    steps_path = out_dir / "steps.json"
    summary_path = out_dir / "harness_summary.json"
    report_path = out_dir / "report.md"
    glb_path = out_dir / "tower.glb"

    # ============ 批量模式（A3 / B7 / F2） ============
    if batch_source is not None:
        from ..intake.tower_batch import intake_tower_batch

        try:
            batch = intake_tower_batch(batch_source, out_dir,
                                       layer_map_path=layer_map_path, merge=merge)
        except Exception as exc:
            graph.start("batch", "批量接入（DWG→DXF→intake）", input=str(batch_source))
            graph.fail(str(exc))
            graph.export_json(steps_path)
            return {"ok": False, "graph": graph, "steps_path": steps_path.as_posix(),
                    "error": str(exc)}

        # F2：per-file 子步骤（每个输入文件一条记录）
        for f in batch["files"]:
            graph.start(f"intake:{f['file']}", f"intake {f['file']}", input=f.get("dxf"))
            if f.get("error"):
                graph.fail(f["error"], kind=f["kind"])
            else:
                graph.finish(
                    kind=f["kind"],
                    bars=f["bars"],
                    nodes=f["nodes"],
                    labeled=f["labeled"],
                    association_rate=f.get("association_rate"),
                    total_entities=(f.get("layers") or {}).get("total_entities"),
                    unidentified_layers=(f.get("layers") or {}).get("unidentified_layers"),
                )
        graph.start("batch", "批量接入（DWG→DXF→intake）", input=str(batch_source))
        merge_report = batch.get("merge_report") or {}
        cross_dup = batch.get("cross_file_bar_id_dup") or {}
        graph.finish(
            files=len(batch["files"]),
            ok=batch["ok"],
            cross_file_duplicate_count=cross_dup.get("duplicate_count", 0),
            merge_mode=merge_report.get("mode"),
            nodes_solved=merge_report.get("nodes_solved", 0),
            nodes_derived_y=merge_report.get("nodes_derived_y", 0),
        )

        model = None
        if merge:
            if batch.get("model_path") and (out_dir / "model.json").exists():
                from ..io import load_model
                model = load_model(out_dir / "model.json")
                # 合并模型需要重新注入规则（BOM 交叉核验 + Harness）
                try:
                    graph.start("compile", "模型编译（规则注入）")
                    from ..intake.tower_pipeline import finalize_tower_model
                    # cross_file 路径已在 intake_tower_batch 内 finalize(merge=True)；
                    # 此处只做 BOM 注入 + 连接详图 Harness 规则，避免重复 merge。
                    already_merged = (batch.get("merge_report") or {}).get("mode") == "cross_file_view"
                    model = finalize_tower_model(
                        model,
                        bom_path=bom_path,
                        merge=not already_merged,
                        allow_scan=allow_scan,
                        layer_map_path=layer_map_path,
                    )
                    graph.finish(
                        rules=len(model.rules),
                        bars=_n(model, "tower_bar"),
                        nodes_solved=sum(
                            1 for c in model.components.values()
                            if c.kind == "tower_node" and c.properties.get("solve_status") == "solved"
                        ),
                    )
                except Exception as exc:
                    graph.fail(str(exc))
            else:
                graph.skip("compile", "模型编译", "merge 未产出模型")
        else:
            graph.skip("compile", "模型编译", "批量模式未 --merge，不合并模型")

        if model is None:
            graph.skip("verify", "验证", "批量模式未 --merge，无可验证的合并模型")
            graph.skip("solve", "3D 约束求解", "批量模式未 --merge")
            graph.skip("export", "交付导出", "批量模式未 --merge")
            graph.export_json(steps_path)
            ok = all(s.status != "failed" for s in graph.steps)
            return {
                "ok": ok,
                "graph": graph,
                "model_path": batch.get("model_path"),
                "steps_path": steps_path.as_posix(),
                "summary_path": None,
                "glb_path": None,
                "batch_report": batch.get("batch_report"),
            }
    else:
        # ============ 单文件 intake ============
        graph.start("intake", "图纸接入", input=str(source), source=str(source))
        try:
            model = build_intake_model(source, layer_map_path=layer_map_path, backend=backend,
                                       bom_path=bom_path, merge=merge, scale=scale,
                                       mm_per_px=mm_per_px)
            detail: Dict[str, Any] = {
                "output": model_path.as_posix(),
                "components": len(model.components),
                "bars": _n(model, "tower_bar"),
                "nodes": _n(model, "tower_node"),
            }
            mllm_meta = getattr(model, "mllm_meta", None)
            if mllm_meta:
                detail["mllm"] = mllm_meta
            mllm_warnings = getattr(model, "mllm_warnings", None)
            if mllm_warnings:
                detail["mllm_warnings"] = mllm_warnings
            graph.finish(**detail)
        except Exception as exc:
            # P1：MLLM 失败时把 model / raw 长度 / 失败原因写进 steps.json
            from ..intake.mllm_backend import MLLMAnalysisError
            if isinstance(exc, MLLMAnalysisError):
                graph.fail(
                    str(exc),
                    failure_reason=exc.meta.get("failure_reason"),
                    model=exc.meta.get("model"),
                    elapsed_s=exc.meta.get("elapsed_s"),
                    raw_length=exc.meta.get("raw_length"),
                    parse_warnings=exc.meta.get("parse_warnings"),
                )
            else:
                graph.fail(str(exc))
            graph.export_json(steps_path)
            return {"ok": False, "graph": graph, "steps_path": steps_path.as_posix(),
                    "error": str(exc)}

        # compile（finalize：BOM + merge + 规则注入）
        graph.start("compile", "模型编译（规则注入/视图合并）")
        try:
            from ..intake.tower_pipeline import finalize_tower_model
            model = finalize_tower_model(model, bom_path=bom_path, merge=merge,
                                         allow_scan=allow_scan,
                                         layer_map_path=layer_map_path)
            graph.finish(rules=len(model.rules), bars=_n(model, "tower_bar"))
        except Exception as exc:
            graph.fail(str(exc))
            graph.export_json(steps_path)
            return {"ok": False, "graph": graph, "steps_path": steps_path.as_posix(),
                    "error": str(exc)}

    # ============ 通用 compile 后处理（单文件 & merge 后 batch） ============
    # cross_check（BOM 已在 compile 中做，这里以步骤形式记录结果）
    graph.start("cross_check", "BOM 交叉核验")
    try:
        bom_dims = [d for d in model.dimensions.values() if d.id.startswith("dim_bom_")]
        graph.finish(bom_dimensions=len(bom_dims), bom_rows=_n(model, "bom_row"))
    except Exception as exc:
        graph.fail(str(exc))

    # 图签/明细页：不进入 3D 求解/导出（B2）
    drawing_file = model.components.get("drawing_file")
    drawing_kind = drawing_file.properties.get("drawing_kind", "drawing") if drawing_file else "drawing"
    skip_3d = drawing_kind in ("title_block", "bom")

    # verify（引用完整性 + 五条规则 Harness；F1 retry 逻辑）
    def _verify() -> tuple:
        problems = validate_references(model)
        results = run_harness(model)
        return problems, results

    graph.start("verify", "验证（引用完整性 + Harness 规则）")
    retry_downgraded = False
    try:
        problems, results = _verify()
        attempts = 1
        failed = [r.target_id for r in results if r.status == ValidationStatus.FAILED]
        # F1：--retry 对 failed 规则重跑；仍失败则降级 human_review（状态变化）
        while retry and attempts < 3 and (problems or failed):
            problems, results = _verify()
            attempts += 1
            failed = [r.target_id for r in results if r.status == ValidationStatus.FAILED]
        if human_review or (retry and (problems or failed)):
            before = {rid: r.status for rid, r in model.rules.items()}
            model = mark_human_review(model)
            retry_downgraded = True
            # 明确人工门：failed 规则已变为 pending + HUMAN_REVIEW 消息，
            # 不再被 harness 重新覆盖；引用问题仍如实保留（不能靠 retry 凭空修复）
            after = {rid: r.status for rid, r in model.rules.items()}
            changed = {rid for rid in before if before[rid] != after.get(rid)}
            status_counts = {}
            for r in results:
                status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1
            if changed:
                status_counts = {f"{k}→human_review" if k == "failed" else k: v
                                 for k, v in status_counts.items()}
            graph.finish(problems=problems, failed=failed, attempts=attempts,
                         summary=status_counts, retry_downgraded=retry_downgraded,
                         status_changed=sorted(changed))
            graph.steps[-1].status = "passed" if not problems else "failed"
            graph.steps[-1].error = (f"引用问题 {len(problems)} 项（需人工修复）"
                                     if problems else None)
        else:
            ok_verify = not problems and not failed
            status_counts = {}
            for r in results:
                status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1
            graph.finish(problems=problems, failed=failed, attempts=attempts,
                         summary=status_counts, retry_downgraded=False)
            if not ok_verify:
                graph.steps[-1].status = "failed"
                graph.steps[-1].error = f"引用问题 {len(problems)} 项；失败规则 {failed}"
    except Exception as exc:
        graph.fail(str(exc))

    save_model(model, model_path)

    # solve + export（title_block/bom 跳过 3D，但报告照写）
    if skip_3d:
        graph.skip("solve", "3D 约束求解", f"drawing_kind={drawing_kind} 不进入 3D 求解")
        graph.skip("export_3d", "3D 交付导出", f"drawing_kind={drawing_kind} 不导出 3D")
    else:
        graph.start("solve", "3D 约束求解", input=model_path.as_posix())
        try:
            from ..solve.tower_solver import solve_tower, compare_to_golden
            nodes, problems = solve_tower(
                model, allow_scan=allow_scan, allow_derived_y=allow_derived_y,
            )
            golden = None
            if golden_path:
                golden = compare_to_golden(nodes, golden_path)
            graph.finish(nodes=len(nodes), problems=problems, golden=golden)
        except Exception as exc:
            graph.fail(str(exc))

        graph.start("export", "交付导出（model/report/glb）")
        try:
            from ..export.exporters import export_report
            export_report(model, report_path)
            save_model(model, model_path)
            summary_payload = {
                "model": model.name,
                "steps": graph.to_dict(),
                "rules": {rid: {"status": r.status.value, "message": r.message}
                          for rid, r in model.rules.items()},
                "bars": _n(model, "tower_bar"),
                "nodes": _n(model, "tower_node"),
                "drawing_kind": drawing_kind,
            }
            summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            exported_glb = None
            try:
                from ..solve.tower_solver import export_tower_glb, export_tower_obj, SolveError
                if format == "obj":
                    export_tower_obj(
                        model, out_dir / "tower.obj", strict=True,
                        allow_scan=allow_scan, allow_derived_y=allow_derived_y,
                    )
                    exported_glb = (out_dir / "tower.obj").as_posix()
                else:
                    export_tower_glb(
                        model, glb_path, strict=True,
                        allow_scan=allow_scan, allow_derived_y=allow_derived_y,
                    )
                    exported_glb = glb_path.as_posix()
            except SolveError as exc:
                graph.finish(error=str(exc), glb_exported=None)
                graph.steps[-1].status = "failed"
            else:
                graph.finish(model=model_path.as_posix(), report=report_path.as_posix(),
                             glb=exported_glb)
        except Exception as exc:
            graph.fail(str(exc))

    # 报告/摘要对图签页也写（无 3D 导出）
    if skip_3d:
        try:
            from ..export.exporters import export_report
            export_report(model, report_path)
            summary_payload = {
                "model": model.name,
                "steps": graph.to_dict(),
                "rules": {rid: {"status": r.status.value, "message": r.message}
                          for rid, r in model.rules.items()},
                "bars": _n(model, "tower_bar"),
                "nodes": _n(model, "tower_node"),
                "drawing_kind": drawing_kind,
            }
            summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        except Exception as exc:
            graph.fail(str(exc))

    graph.export_json(steps_path)
    ok = all(s.status != "failed" for s in graph.steps)
    return {
        "ok": ok,
        "graph": graph,
        "model_path": model_path.as_posix(),
        "steps_path": steps_path.as_posix(),
        "summary_path": summary_path.as_posix() if summary_path.exists() else None,
        "glb_path": glb_path.as_posix() if glb_path.exists() else None,
    }
