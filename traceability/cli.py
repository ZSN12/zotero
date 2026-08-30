"""命令行入口 —— 把「三阶段工程上下文管线」变成可执行命令。

    # 阶段 1：图纸接入
    python -m traceability.cli intake-dxf examples/demo.dxf
    python -m traceability.cli intake-scan scan.png

    # 阶段 2：编译与查询
    python -m traceability.cli validate examples/pipe_network.json
    python -m traceability.cli report examples/pipe_network.json

    # 阶段 3：验证与交付
    python -m traceability.cli harness examples/pipe_network.json
    python -m traceability.cli export examples/pipe_network.json --format cypher
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cli_exit import status_to_exit, EXIT_FAILED, EXIT_REVIEW_REQUIRED
from .graph import invalidate, stale_report
from .io import load_model, render_report, save_model, validate_references, validate_against_schema
from .model import ValidationStatus, Staleness
from .intake.dwg import extract_from_dxf, make_demo_dxf
from .intake.ocr import extract_dimensions_from_image
from .intake.tower_dxf import make_demo_tower_dxf, extract_tower_from_dxf
from .intake.tower_bom import parse_bom_auto, parse_bom_csv, cross_check_bom
from .solve.tower_solver import (
    solve_tower,
    export_tower_obj,
    export_tower_glb,
    compare_to_golden,
    axis_origin_summary,
    SolveError,
)
from .intake.mllm_backend import DrawingInput, choose_backend, MLLMBackend
from .skill.contract import to_engineering_model
from .harness.harness import run_harness, summarize
from .harness.tower_validators import inject_tower_rules
from .intake.tower_pipeline import finalize_tower_model, evaluate_tower_model
from .export.exporters import export_cypher, export_gexf, export_report


def cmd_validate(args):
    model = load_model(args.file)
    problems = validate_references(model)
    if args.schema:
        schema_problems = validate_against_schema(model)
        if schema_problems:
            print("✗ JSON Schema 校验未通过：")
            for p in schema_problems:
                print(f"  - {p}")
            sys.exit(1)
        print("✓ JSON Schema 校验通过")
    if problems:
        print("✗ 引用完整性校验未通过：")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("✓ 引用完整性校验通过")


def cmd_report(args):
    model = load_model(args.file)
    print(render_report(model))


def cmd_invalidate(args):
    model = load_model(args.file)
    changed = set(args.node)
    stale = invalidate(model, changed)
    print(f"改动节点：{', '.join(sorted(changed))}")
    print(f"作废节点（{len(stale)} 个）：{', '.join(sorted(stale))}")
    save_model(model, args.file)
    print(f"已写回 {args.file}")


def cmd_verify(args):
    model = load_model(args.file)
    if args.rule:
        for rid in args.rule:
            if rid in model.rules:
                model.rules[rid].status = ValidationStatus.PASSED
                model.rules[rid].message = "已由工程 Agent Harness 验证"
                print(f"规则 {rid} -> passed")
            else:
                print(f"规则 {rid} 不存在")
    else:
        for conn in model.connections.values():
            conn.validation_status = ValidationStatus.PASSED
        print(f"全部 {len(model.connections)} 条连接 -> passed")

    # 验证通过后，把这些节点恢复为 current
    verified = set(args.rule) if args.rule else set(model.connections)
    model.refresh(verified)
    save_model(model, args.file)
    print(f"已写回 {args.file}")


def cmd_intake_dxf(args):
    from pathlib import Path
    if args.demo:
        src = make_demo_dxf(args.file)
        print(f"已生成演示 DXF：{src}")
    model = extract_from_dxf(args.file)
    save_model(model, args.out)
    print(f"从 {args.file} 抽取 {len(model.components)} 个构件，已保存到 {args.out}")
    print("提示：抽取采用几何启发式，置信度较低，请人工复核。")


def cmd_intake_scan(args):
    if args.tower:
        from .intake.tower_layout import analyze_tower_scan, confirm_tower_scan
        image_path = args.file
        if Path(args.file).suffix.lower() == ".pdf":
            from .intake.pdf_raster import rasterize_pdf_to_png
            image_path = rasterize_pdf_to_png(args.file, page=getattr(args, "page", 1))
            print(f"✓ PDF 已栅格化 -> {image_path}")
        model = analyze_tower_scan(
            image_path,
            model_name=f"scan-{Path(args.file).stem}",
            filter_noise=not args.no_filter,
            scale=args.scale,
            mm_per_px=args.mm_per_px,
            associate_ocr=args.ocr,
        )
        if args.confirm:
            model = confirm_tower_scan(model)
            print("✓ 已人工确认：solve_status=verified（可用 solve-tower --allow-scan 导出）")
        save_model(model, args.out)
        n_bars = sum(1 for c in model.components.values() if c.kind == "tower_bar")
        n_nodes = sum(1 for c in model.components.values() if c.kind == "tower_node")
        unit = "mm" if (args.scale or args.mm_per_px) else "pixel"
        print(f"✓ 扫描图候选：{n_bars} 根候选杆件 / {n_nodes} 个候选节点（{unit} 坐标）")
        print("  置信度 ≤ 0.6，solve_status=pending_review，不进终版 3D，请人工复核。")
        return
    model = extract_dimensions_from_image(args.file)
    save_model(model, args.out)
    print(f"从 {args.file} 建立扫描图上下文，已保存到 {args.out}")
    print("提示：未安装 OCR 时，尺寸为 placeholder（待补测），绝不猜值。")


def cmd_harness(args):
    model = load_model(args.file)
    results = run_harness(model, args.rule)
    print(summarize(results))
    save_model(model, args.file)
    print(f"验证结果已写回 {args.file}")


def cmd_export(args):
    model = load_model(args.file)
    out = args.out or f"{Path(args.file).stem}.export"
    if args.format == "cypher":
        path = f"{out}.cypher"
        export_cypher(model, path)
        print(f"已导出 Neo4j Cypher -> {path}")
    elif args.format == "gexf":
        path = f"{out}.gexf"
        export_gexf(model, path)
        print(f"已导出 GEXF 图 -> {path}")
    elif args.format == "report":
        path = f"{out}.md"
        export_report(model, path)
        print(f"已导出交付报告 -> {path}")
    elif args.format == "obj":
        path = f"{out}.obj"
        export_tower_obj(model, path, strict=True)
        print(f"已导出 3D 线框 -> {path}")
    elif args.format == "glb":
        path = f"{out}.glb"
        export_tower_glb(model, path, strict=True)
        print(f"已导出 GLB 实体 -> {path}")
    elif args.format == "dxf":
        from .export.dxf_write import export_tower_dxf
        path = f"{out}.dxf" if not str(out).lower().endswith(".dxf") else str(out)
        export_tower_dxf(model, path)
        print(f"已导出线框 DXF -> {path}")
    elif args.format == "step":
        from .solve.tower_solver import export_tower_step, SolveError
        path = f"{out}.step" if not str(out).lower().endswith(".step") else str(out)
        try:
            export_tower_step(model, path, strict=True)
        except SolveError as e:
            print(f"✗ {e}")
            sys.exit(1)
        print(f"已导出 STEP -> {path}")


def cmd_intake_tower(args):
    if args.demo:
        src = make_demo_tower_dxf(args.file)
        print(f"已生成演示铁塔 DXF：{src}")
    source = args.file
    if Path(args.file).suffix.lower() == ".dwg":
        from .intake.dwg import ensure_dxf
        source = ensure_dxf(args.file)
        print(f"✓ DWG 已转换 -> {source}")
    model = extract_tower_from_dxf(source, eps=args.eps, layer_map_path=args.layer_map)
    if args.layer_map:
        print(f"✓ 使用 per-project overlay：{args.layer_map}")
    model = finalize_tower_model(model, bom_path=args.bom, merge=args.merge,
                                 allow_scan=args.allow_scan,
                                 layer_map_path=args.layer_map)
    if args.bom:
        print(f"已交叉核验 BOM：{len(parse_bom_auto(args.bom))} 行")
    if args.merge:
        print("已执行跨视图合并（Phase 2）")

    if not args.no_check:
        problems = validate_references(model)
        if problems:
            print("✗ 引用完整性校验未通过：")
            for problem in problems:
                print(f"  - {problem}")
            sys.exit(1)
        print("✓ 引用完整性校验通过")
        print(summarize(run_harness(model)))

    save_model(model, args.out)

    n_bars = sum(1 for c in model.components.values() if c.kind == "tower_bar")
    n_nodes = sum(1 for c in model.components.values() if c.kind == "tower_node")
    labeled = sum(1 for c in model.components.values()
                  if c.kind == "tower_bar" and not c.properties.get("bar_id", "").startswith("UNLABELED"))
    print(f"✓ 抽取完成：{n_bars} 根杆件 / {n_nodes} 个节点 / 编号关联 {labeled}/{n_bars}")
    print(f"  规则已注入：{len(model.rules)} 条")
    print(f"  模型已保存 -> {args.out}")


def cmd_solve_tower(args):
    model = load_model(args.file)
    nodes, problems = solve_tower(
        model, allow_scan=args.allow_scan, allow_derived_y=args.allow_derived_y,
    )
    print(f"节点 {len(nodes)} 个，待补测/拓扑问题 {len(problems)} 项")
    origin_summary = axis_origin_summary(nodes)
    print("坐标来源（measured/derived/placeholder）：")
    for axis in ("x", "y", "z"):
        s = origin_summary[axis]
        print(f"  {axis}: measured={s['measured']} derived={s['derived']} "
              f"placeholder={s['placeholder']}")
    for prob in problems:
        print(f"  - {prob}")
    if problems and not args.force:
        print("✗ 存在 placeholder/拓扑问题，拒绝终版导出（用 --force 可强制导出线框，仅供预览）")
        sys.exit(1)
    fmt = args.format or Path(args.out).suffix.lstrip(".").lower() or "obj"
    try:
        if fmt == "glb":
            export_tower_glb(
                model, args.out, strict=not args.force,
                allow_scan=args.allow_scan, allow_derived_y=args.allow_derived_y,
            )
        else:
            export_tower_obj(
                model, args.out, strict=not args.force,
                allow_scan=args.allow_scan, allow_derived_y=args.allow_derived_y,
            )
    except SolveError as e:
        print(f"✗ {e}")
        sys.exit(1)
    print(f"✓ 3D 已导出 -> {args.out}")
    if args.golden:
        report = compare_to_golden(nodes, args.golden)
        print(f"金标准对齐：{report['matched']}/{report['golden_nodes']} 节点，"
              f"max={report['max_dev_mm']}mm, mean={report['mean_dev_mm']}mm, "
              f"p95={report['p95_dev_mm']}mm, max_rel={report['max_rel']}")
        if not report["passed"]:
            print("✗ 与金标准偏差超限（>2% 或 >50mm）")
            sys.exit(1)
        print("✓ 与金标准偏差在验收限内")


def cmd_compile_drawing(args):
    """一条命令：后端选择 -> 模型分析 -> Skill 契约 -> EngineeringModel。"""
    kind = args.kind or Path(args.file).suffix.lstrip(".").lower()
    if kind == "dwg":
        kind = "dxf"

    # P1-3 PDF 转图入口
    raster_path = args.file
    if kind == "pdf":
        from .intake.pdf_raster import rasterize_pdf_to_png
        raster_path = rasterize_pdf_to_png(args.file, page=getattr(args, "page", 1))
        print(f"✓ PDF 已栅格化 -> {raster_path}")

    # 铁塔扫描图：tower+scan -> MLLM 优先，无 API 降级 rule-based-scan（P1-2）
    if args.tower and kind in ("png", "jpg", "jpeg", "scan", "pdf"):
        drawing = DrawingInput(path=raster_path, kind="scan", original_location=args.location,
                               tower=True)
        mllm = MLLMBackend()
        backend = choose_backend(drawing, mllm=mllm)
        if args.backend:
            if args.backend == "mllm":
                backend = mllm
            else:
                from .intake.mllm_backend import TowerScanBackend
                backend = TowerScanBackend()
        print(f"后端：{backend.name}")
        if getattr(backend, "name", "") == "mllm":
            candidate = backend.analyze(drawing)
            if not candidate.objects:
                print(f"⚠ MLLM 未产出候选：{candidate.raw}")
                save_model(to_engineering_model(candidate, name=f"tower-{Path(args.file).stem}"), args.out)
                return
            model = to_engineering_model(candidate, name=f"tower-{Path(args.file).stem}")
        else:
            from .intake.tower_layout import analyze_tower_scan
            model = analyze_tower_scan(raster_path, model_name=f"tower-{Path(args.file).stem}",
                                       scale=args.scale, mm_per_px=args.mm_per_px)
        model = finalize_tower_model(model, bom_path=args.bom, merge=args.merge,
                                     allow_scan=args.allow_scan,
                                     layer_map_path=args.layer_map)
        print(summarize(run_harness(model)))
        save_model(model, args.out)
        print(f"✓ 扫描图候选模型已保存 -> {args.out}（待人工复核）")
        return

    source_path = args.file
    if Path(args.file).suffix.lower() == ".dwg":
        from .intake.dwg import ensure_dxf
        source_path = ensure_dxf(args.file)
        print(f"✓ DWG 已转换 -> {source_path}")
    drawing = DrawingInput(path=source_path, kind=kind, original_location=args.location,
                           tower=args.tower)
    backend = choose_backend(drawing, mllm=MLLMBackend())
    print(f"后端：{backend.name}")
    candidate = backend.analyze(drawing)
    if not candidate.objects:
        print("⚠ 后端未产出任何候选对象（可能未配置 API 或解析失败）")
        if candidate.raw:
            print(f"  原因：{candidate.raw[:200]}")
    # 铁塔模型沿用 tower-<stem> 命名，保证后续 merge/solve 能定位视图规范
    model_name = f"tower-{Path(args.file).stem}" if args.tower else f"compiled-{Path(args.file).stem}"
    model = to_engineering_model(candidate, name=model_name)

    if args.tower:
        # MLLM/规则输出 -> 铁塔验证链：BOM + 跨视图合并 + 规则 + Harness + 金标准
        model = finalize_tower_model(model, bom_path=args.bom, merge=args.merge,
                                     allow_scan=args.allow_scan,
                                     layer_map_path=args.layer_map)
        print(f"✓ 铁塔规则已注入：{len(model.rules)} 条")
        if args.merge:
            print("✓ 已执行跨视图合并")
        print(summarize(run_harness(model)))
        if args.golden:
            report = evaluate_tower_model(model, golden_path=args.golden)["golden"]
            if report:
                print(f"金标准对齐：{report['matched']}/{report['golden_nodes']} 节点，"
                      f"max={report['max_dev_mm']}mm, max_rel={report['max_rel']}")

    save_model(model, args.out)
    print(f"✓ Skill 契约转换完成：{len(model.components)} 构件 / "
          f"{len(model.dimensions)} 尺寸 / {len(model.connections)} 连接 / "
          f"{len(model.rules)} 规则")
    print(f"  模型已保存 -> {args.out}")


def cmd_run_tower(args):
    """P0-1：一步命令跑完全链，每步状态日志 JSON。"""
    from .harness.tower_harness import run_tower
    result = run_tower(
        source=args.file,
        out_dir=args.out_dir,
        bom_path=args.bom,
        merge=args.merge,
        golden_path=args.golden,
        layer_map_path=args.layer_map,
        backend=args.backend,
        retry=args.retry,
        human_review=args.human_review,
        allow_scan=args.allow_scan,
        allow_derived_y=args.allow_derived_y,
        format=args.format,
        scale=args.scale,
        mm_per_px=args.mm_per_px,
        input_dir=getattr(args, "input_dir", None),
        use_ocr_fallback=not getattr(args, "no_ocr_fallback", False),
        agent_mode=getattr(args, "agent_mode", "ezdxf"),
    )
    if result.get("ok"):
        print("✓ 全链完成")
    else:
        print("✗ 全链存在失败步骤（详见 steps.json）")
    print(f"  模型 -> {result.get('model_path')}")
    print(f"  步骤日志 -> {result.get('steps_path')}")
    print(f"  Harness 摘要 -> {result.get('summary_path')}")
    if result.get("glb_path"):
        print(f"  3D -> {result.get('glb_path')}")
    if not result.get("ok"):
        sys.exit(1)


def cmd_cross_file_batch(args):
    """Phase D：多 DWG 分文件真 3D 视图合并（merge_view_coordinates）。"""
    from .intake.tower_batch import cross_file_batch
    result = cross_file_batch(
        args.input_dir,
        args.out_dir,
        layer_map_path=args.layer_map,
        bom_path=args.bom,
    )
    print(f"✓ cross_file_batch：{len(result['files'])} 个文件，mode={result.get('merge_report', {}).get('mode')}")
    if result.get("model_path"):
        print(f"  合并模型 -> {result['model_path']}")
    print(f"  报告 -> {result['batch_report']}")
    if not result.get("ok"):
        sys.exit(1)


def cmd_build_project(args):
    """Gap 1：构建图册级 ProjectModel 索引。"""
    from .project.model import build_project_from_directory, save_project
    project = build_project_from_directory(
        args.input_dir,
        args.project_id or Path(args.input_dir).name,
        layer_map_path=args.layer_map,
        out_dir=args.out_dir,
    )
    path = save_project(project, Path(args.out_dir) / "project.json")
    print(f"✓ ProjectModel：{len(project.sheets)} sheets -> {path}")


def cmd_deliver_project(args):
    """M6：图册级一键交付（Project + cross_file + Harness + GLB）。"""
    from .project.delivery import deliver_project

    result = deliver_project(
        args.input_dir,
        args.out_dir,
        project_id=args.project_id,
        layer_map_path=args.layer_map,
        bom_path=args.bom,
        export_glb=not args.no_glb,
        agent_mode=getattr(args, "agent_mode", "ezdxf"),
    )
    print(f"✓ deliver-project：ok={result.get('ok')} status={result.get('status')} harness_all_passed={result.get('harness_all_passed')}")
    print(f"  manifest -> {result.get('manifest_path')}")
    if result.get("model_path"):
        print(f"  model    -> {result['model_path']}")
    # Phase A3：L0 / M3 / M1 三种产物分开打印，不再混评
    for prod in result.get("products") or []:
        mark = "✓" if prod.get("present") else "✗"
        path = prod.get("path") or (prod.get("error") or "未产出")
        print(f"  {mark} {prod.get('id')} ({prod.get('layer')}) -> {path}")
    if result.get("glb_error"):
        print(f"  skeleton ✗ -> {result['glb_error']}")
    if result.get("canonical_error"):
        print(f"  canonical ✗ -> {result['canonical_error']}")
    mr = result.get("merge_report") or {}
    print(f"  merge    -> nodes={mr.get('nodes_solved')} bars={mr.get('bars')} "
          f"gussets={mr.get('gussets_anchored')} synthetic_y={mr.get('y_synthetic_side')}")
    ph = result.get("project_harness") or {}
    inv = result.get("bar_inventory") or {}
    if ph:
        print(f"  project  -> harness={ph.get('counts')} sheets={ph.get('sheet_count')}")
    if inv:
        print(f"  bar_inv  -> unique={inv.get('total_unique_bar_ids')} "
              f"cross_sheet={inv.get('cross_sheet_count', 0)}")
    bs = result.get("bom_tree_summary") or {}
    if bs.get("master_bom_path"):
        print(f"  master   -> conflicts={bs.get('conflict_count', 0)} "
              f"physical_ids={len(result.get('physical_bar_counts') or {})}")
    asm = result.get("assembly") or {}
    if asm.get("enabled"):
        print(f"  assembly -> mode={asm.get('mode')} modules={asm.get('module_ids')}")
    # 阶段 8.6：退出码统一走 cli_exit.status_to_exit（verified→0 / failed→1 /
    # review_required→2），禁止在此硬编码，避免与测试/计划 §8.6 分叉。
    status = result.get("status", "failed")
    sys.exit(status_to_exit(status))


def cmd_intake_tower_batch(args):
    """A3/B7：目录内全部 DWG 转 DXF 并逐文件 intake，输出 layer 报告。"""
    from .intake.tower_batch import intake_tower_batch
    result = intake_tower_batch(
        args.input_dir,
        args.out_dir,
        layer_map_path=args.layer_map,
        merge=args.merge,
    )
    print(f"✓ 批量接入：{len(result['files'])} 个文件")
    for f in result["files"]:
        status = "✗" if f.get("error") else "✓"
        print(f"  {status} {f['file']}: kind={f['kind']}, bars={f['bars']}, "
              f"nodes={f['nodes']}, labeled={f['labeled']}, rate={f.get('association_rate') or 0}")
        if f.get("error"):
            print(f"     error: {f['error']}")
    if result.get("model_path"):
        print(f"  合并模型 -> {result['model_path']}")
    print(f"  汇总报告 -> {result['batch_report']}")
    if not result.get("ok"):
        sys.exit(1)


def cmd_parse_report(args):
    """F3：输出 PARSE_RATE_REPORT 同款 JSON（一条命令替代手改 markdown）。"""
    import json
    from pathlib import Path as _Path
    from .intake.tower_dxf import extract_tower_from_dxf, layer_usage_report

    path = _Path(args.file)
    usage = layer_usage_report(path, layer_map_path=args.layer_map)
    model = extract_tower_from_dxf(path, layer_map_path=args.layer_map)
    bars = [c for c in model.components.values() if c.kind == "tower_bar"]
    nodes = [c for c in model.components.values() if c.kind == "tower_node"]
    labeled = [c for c in bars if not str(c.properties.get("bar_id", "")).startswith("UNLABELED")]
    df = model.components.get("drawing_file")
    dup_detail = (df.properties.get("duplicate_bar_id_detail", []) if df else [])
    report = {
        "file": str(path),
        "total_entities": usage.get("total_entities", 0),
        "bars": len(bars),
        "nodes": len(nodes),
        "labeled_bars": len(labeled),
        "association_rate": round(len(labeled) / len(bars), 4) if bars else 0.0,
        "duplicate_bar_id_groups": (df.properties.get("duplicate_bar_id_groups", 0) if df else 0),
        "duplicate_bar_id_detail": dup_detail,
        "recognized_bar_layers": usage.get("recognized_bar_layers", []),
        "recognized_text_layers": usage.get("recognized_text_layers", []),
        "unidentified_layers": usage.get("unidentified_layers", []),
        "entity_count_by_layer": usage.get("entity_count_by_layer", {}),
    }
    if args.out:
        out = _Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✓ 解析率报告 -> {out}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_deliver_tower(args):
    """P0-4：一次产出 model.json + tower.glb + report.md + steps.json + harness_summary.json。"""
    from .harness.tower_harness import run_tower
    from .io import load_model
    from .export.exporters import export_report
    from .solve.tower_solver import export_tower_glb, export_tower_obj, SolveError

    result = run_tower(
        source=args.file,
        out_dir=args.out_dir,
        bom_path=args.bom,
        merge=args.merge,
        golden_path=args.golden,
        layer_map_path=args.layer_map,
        backend=args.backend,
        retry=args.retry,
        human_review=args.human_review,
        allow_scan=args.allow_scan,
        allow_derived_y=args.allow_derived_y,
        format=args.format,
        # 阶段 10：deprecated 包装器全量转发（补齐与 run-tower 一致）
        scale=getattr(args, "scale", None),
        mm_per_px=getattr(args, "mm_per_px", None),
        input_dir=getattr(args, "input_dir", None),
        use_ocr_fallback=not getattr(args, "no_ocr_fallback", False),
        agent_mode=getattr(args, "agent_mode", "ezdxf"),
    )
    if not result.get("ok"):
        print("✗ 交付链存在失败步骤：")
        for st in result["graph"].steps:
            if st.status == "failed":
                print(f"  - {st.id}: {st.error}")
        # 阶段 8.6：失败统一返回 EXIT_FAILED=1（failed→1）。
        sys.exit(EXIT_FAILED)

    # 补交付件（run_tower 已写 model/steps/summary/report/glb）
    out_dir = Path(args.out_dir)
    report_md = out_dir / "report.md"
    if not report_md.exists():
        model = load_model(out_dir / "model.json")
        export_report(model, report_md)
    print("✓ 交付包已生成：")
    for f in ("model.json", "tower.glb" if args.format != "obj" else "tower.obj",
              "report.md", "steps.json", "harness_summary.json"):
        pth = out_dir / f
        if pth.exists():
            print(f"  - {pth}")
        else:
            print(f"  - {pth}（未生成）")


def cmd_confirm_scan(args):
    """P2-5：人工确认扫描候选，solve_status=verified。"""
    from .intake.tower_layout import confirm_tower_scan
    model = load_model(args.file)
    model = confirm_tower_scan(model)
    save_model(model, args.file)
    print(f"✓ 扫描候选已人工确认（solve_status=verified）-> {args.file}")
    print("  现在可用：solve-tower --allow-scan 进行 strict 导出")


def cmd_confirm_derived_y(args):
    """cross_file z-peer 插值 y：人工复核后 y_review=verified。"""
    from .intake.tower_pipeline import confirm_cross_file_derived_y, derived_y_pending_nodes
    model = load_model(args.file)
    pending = derived_y_pending_nodes(model)
    if not pending:
        print(f"✓ 无待复核插值 y 节点 -> {args.file}")
        return
    model = confirm_cross_file_derived_y(model)
    save_model(model, args.file)
    print(f"✓ 插值 y 已人工复核（{len(pending)} 个节点 y_review=verified）-> {args.file}")
    print("  现在可用：solve-tower --allow-derived-y 进行 strict GLB 导出")


def cmd_merge_scans(args):
    """P2-4：front + side 扫描图融合。"""
    from .intake.tower_layout import analyze_tower_scan
    from .intake.tower_scan_merge import merge_scan_views
    front = analyze_tower_scan(args.front, model_name="scan-front")
    side = analyze_tower_scan(args.side, model_name="scan-side")
    merged = merge_scan_views(front, side, scale=args.scale, mm_per_px=args.mm_per_px)
    save_model(merged, args.out)
    n_nodes = sum(1 for c in merged.components.values() if c.kind == "tower_node")
    n_bars = sum(1 for c in merged.components.values() if c.kind == "tower_bar")
    print(f"✓ 扫描融合：{n_bars} 根候选杆件 / {n_nodes} 个候选节点 -> {args.out}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="engineering-trace",
        description="工程图 → 可追溯、可验证、可变更管理的工程上下文",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="校验模型引用完整性")
    p_val.add_argument("file")
    p_val.add_argument("--schema", action="store_true", help="同时按 schema/engineering_model.json 校验结构")
    p_val.set_defaults(func=cmd_validate)

    p_rep = sub.add_parser("report", help="输出追溯报告")
    p_rep.add_argument("file")
    p_rep.set_defaults(func=cmd_report)

    p_inv = sub.add_parser("invalidate", help="改动节点并传播作废")
    p_inv.add_argument("file")
    p_inv.add_argument("--node", action="append", required=True, help="被改动的节点 ID")
    p_inv.set_defaults(func=cmd_invalidate)

    p_ver = sub.add_parser("verify", help="验证规则/连接并恢复 current")
    p_ver.add_argument("file")
    p_ver.add_argument("--rule", action="append", help="要验证的规则 ID（不指定则验证全部连接）")
    p_ver.set_defaults(func=cmd_verify)

    # ---- 阶段 1：图纸接入 ----
    p_dxf = sub.add_parser("intake-dxf", help="从 DXF/DWG 抽取构件")
    p_dxf.add_argument("file")
    p_dxf.add_argument("--out", default="dxf_extract.json")
    p_dxf.add_argument("--demo", action="store_true", help="先生成一个演示 DXF 再抽取")
    p_dxf.set_defaults(func=cmd_intake_dxf)

    p_scan = sub.add_parser("intake-scan", help="从扫描图建立上下文（可插拔 OCR；--tower 走铁塔线检测）")
    p_scan.add_argument("file")
    p_scan.add_argument("--out", default="scan_extract.json")
    p_scan.add_argument("--tower", action="store_true", help="铁塔扫描图：版面分析 + 霍夫线检测 + 端点聚类")
    p_scan.add_argument("--no-filter", action="store_true", help="关闭 P2-1 噪声过滤（回归对照）")
    p_scan.add_argument("--scale", help="图纸比例，如 1:50（P2-2 px→mm 标定）")
    p_scan.add_argument("--mm-per-px", type=float, help="显式 mm/px（P2-2，优先级最高）")
    p_scan.add_argument("--ocr", action="store_true", help="尝试 OCR 件号空间关联（P2-3）")
    p_scan.add_argument("--confirm", action="store_true", help="人工确认：solve_status=verified（P2-5）")
    p_scan.add_argument("--page", type=int, default=1, help="PDF 页码（1-based，D2）")
    p_scan.set_defaults(func=cmd_intake_scan)

    p_tower = sub.add_parser("intake-tower", help="从铁塔 DXF 抽取杆件/节点/编号（Phase 1）")
    p_tower.add_argument("file")
    p_tower.add_argument("--out", default="tower_model.json")
    p_tower.add_argument("--bom", help="BOM CSV 路径（Phase 2 交叉核验）")
    p_tower.add_argument("--eps", type=float, default=50.0, help="节点聚类阈值（图纸单位）")
    p_tower.add_argument("--demo", action="store_true", help="先生成演示 DXF 再抽取")
    p_tower.add_argument("--merge", action="store_true", help="跨视图合并坐标并合并投影杆件（Phase 2）")
    p_tower.add_argument("--no-check", action="store_true", help="跳过自动 validate + harness")
    p_tower.add_argument("--layer-map", help="per-project overlay JSON（P1-5，换图只改配置）")
    p_tower.add_argument("--allow-scan", action="store_true", help="允许扫描候选进入求解链（P2-5）")
    p_tower.set_defaults(func=cmd_intake_tower)

    p_compile = sub.add_parser("compile-drawing", help="MLLM 后端 -> Skill 契约 -> EngineeringModel（一条命令）")
    p_compile.add_argument("file")
    p_compile.add_argument("--kind", help="输入类型（dxf/dwg/pdf/png/jpg/scan），默认按扩展名推断")
    p_compile.add_argument("--location", default="", help="文件原始位置（保留文件/版本/位置）")
    p_compile.add_argument("--out", default="compiled_model.json")
    p_compile.add_argument("--tower", action="store_true", help="按铁塔管线接验证器（注入规则 + Harness）")
    p_compile.add_argument("--bom", help="铁塔 BOM CSV（配合 --tower）")
    p_compile.add_argument("--merge", action="store_true", help="铁塔跨视图合并（配合 --tower）")
    p_compile.add_argument("--golden", help="铁塔金标准 JSON（配合 --tower，验收坐标偏差 2%% 以内）")
    p_compile.add_argument("--backend", choices=["mllm", "rule-based-scan"],
                           help="强制指定后端（P1-2）")
    p_compile.add_argument("--layer-map", help="per-project overlay JSON（P1-5）")
    p_compile.add_argument("--allow-scan", action="store_true", help="允许扫描候选进入求解链（P2-5）")
    p_compile.add_argument("--scale", help="扫描图图纸比例，如 1:50（P2-2）")
    p_compile.add_argument("--mm-per-px", type=float, help="扫描图显式 mm/px（P2-2）")
    p_compile.add_argument("--page", type=int, default=1, help="PDF 页码（1-based，D2）")
    p_compile.set_defaults(func=cmd_compile_drawing)

    p_solve = sub.add_parser("solve-tower", help="从模型求解并导出 3D 线框（Phase 3）")
    p_solve.add_argument("file")
    p_solve.add_argument("--out", default="tower_head.obj")
    p_solve.add_argument("--format", choices=["obj", "glb"], help="导出格式（默认按 --out 后缀）")
    p_solve.add_argument("--force", action="store_true", help="存在缺失轴时仍强制导出（仅供预览）")
    p_solve.add_argument("--golden", help="金标准 JSON 路径（验收：坐标偏差 2%% 以内）")
    p_solve.add_argument("--allow-scan", action="store_true", help="允许已人工确认的扫描候选导出（P2-5）")
    p_solve.add_argument("--allow-derived-y", action="store_true",
                         help="允许已人工复核的 z-peer 插值 y 导出（cross_file）")
    p_solve.set_defaults(func=cmd_solve_tower)

    # ---- P0/P1/P2 编排与交付命令 ----
    p_run = sub.add_parser("run-tower", help="一步跑完铁塔全链（P0-1）")
    p_run.add_argument("file")
    p_run.add_argument("--out-dir", default="out/tower-run")
    p_run.add_argument("--bom", help="BOM CSV")
    p_run.add_argument("--merge", action="store_true", help="跨视图合并")
    p_run.add_argument("--golden", help="金标准 JSON")
    p_run.add_argument("--layer-map", help="per-project overlay JSON（P1-5）")
    p_run.add_argument("--backend", choices=["mllm", "rule-based-scan"], help="强制指定后端")
    p_run.add_argument("--retry", action="store_true", help="失败步骤重试")
    p_run.add_argument("--human-review", action="store_true", help="pending/failed 标记人工复核")
    p_run.add_argument("--allow-scan", action="store_true", help="允许扫描候选进入求解链（P2-5）")
    p_run.add_argument("--allow-derived-y", action="store_true",
                       help="允许已复核的 z-peer 插值 y 进入 GLB 导出")
    p_run.add_argument("--format", choices=["obj", "glb"], default="glb")
    p_run.add_argument("--scale", help="扫描图比例尺")
    p_run.add_argument("--mm-per-px", type=float, help="扫描图 mm/px")
    p_run.add_argument("--no-ocr-fallback", action="store_true",
                       help="扫描图 A1 件号 OCR 不用 Tesseract 兜底（B4，默认启用兜底）")
    p_run.add_argument("--input-dir", help="批量模式：目录内全部 DWG/DXF（A3）")
    p_run.add_argument("--agent-mode", choices=["ezdxf", "hybrid"], default="ezdxf",
                       help="单文件 DXF 几何后端：ezdxf（默认）/ hybrid（MLLM Agent 链）")
    p_run.set_defaults(func=cmd_run_tower)

    p_batch = sub.add_parser("intake-tower-batch", help="目录内全部 DWG 转 DXF 并批量 intake（A3/B7）")
    p_batch.add_argument("input_dir")
    p_batch.add_argument("--out-dir", default="out/tower-batch")
    p_batch.add_argument("--layer-map", help="per-project overlay JSON（P1-5）")
    p_batch.add_argument("--merge", action="store_true", help="多文件合并为单个 EngineeringModel（B7）")
    p_batch.set_defaults(func=cmd_intake_tower_batch)

    p_cross = sub.add_parser("cross-file-batch", help="Phase D：多 DWG 分文件真 3D 视图合并")
    p_cross.add_argument("input_dir")
    p_cross.add_argument("--out-dir", default="out/cross-file")
    p_cross.add_argument("--layer-map", help="per-project overlay JSON")
    p_cross.add_argument("--bom", help="BOM CSV")
    p_cross.set_defaults(func=cmd_cross_file_batch)

    p_proj = sub.add_parser("build-project", help="Gap 1：构建图册级 ProjectModel")
    p_proj.add_argument("input_dir")
    p_proj.add_argument("--out-dir", default="out/project")
    p_proj.add_argument("--project-id", help="项目 ID（默认取目录名）")
    p_proj.add_argument("--layer-map", help="per-project overlay JSON")
    p_proj.set_defaults(func=cmd_build_project)

    p_dproj = sub.add_parser("deliver-project", help="M6：图册级一键交付（Project+cross_file+GLB）")
    p_dproj.add_argument("input_dir")
    p_dproj.add_argument("--out-dir", default="out/project-delivery")
    p_dproj.add_argument("--project-id", help="项目 ID（默认取目录名）")
    p_dproj.add_argument("--layer-map", help="per-project overlay JSON")
    p_dproj.add_argument("--bom", help="BOM CSV")
    p_dproj.add_argument("--no-glb", action="store_true", help="跳过 GLB 导出")
    p_dproj.add_argument("--agent-mode", choices=["ezdxf", "hybrid"], default="ezdxf",
                         help="几何提取后端：ezdxf（默认，纯矢量）/ hybrid（Kimi/MLLM Agent 链）")
    p_dproj.set_defaults(func=cmd_deliver_project)

    p_parse = sub.add_parser("parse-report", help="输出 PARSE_RATE_REPORT 同款 JSON（F3）")
    p_parse.add_argument("file")
    p_parse.add_argument("--layer-map", help="per-project overlay JSON（P1-5）")
    p_parse.add_argument("--out", help="JSON 输出路径（不写则打印到 stdout）")
    p_parse.set_defaults(func=cmd_parse_report)

    p_deliver = sub.add_parser("deliver-tower", help="一键交付包（P0-4）")
    p_deliver.add_argument("file")
    p_deliver.add_argument("--out-dir", default="out/tower-delivery")
    p_deliver.add_argument("--bom", help="BOM CSV")
    p_deliver.add_argument("--merge", action="store_true", help="跨视图合并")
    p_deliver.add_argument("--golden", help="金标准 JSON")
    p_deliver.add_argument("--layer-map", help="per-project overlay JSON（P1-5）")
    p_deliver.add_argument("--backend", choices=["mllm", "rule-based-scan"])
    p_deliver.add_argument("--retry", action="store_true")
    p_deliver.add_argument("--human-review", action="store_true")
    p_deliver.add_argument("--allow-scan", action="store_true")
    p_deliver.add_argument("--allow-derived-y", action="store_true",
                           help="允许已复核的 z-peer 插值 y 进入 GLB 导出")
    p_deliver.add_argument("--format", choices=["obj", "glb"], default="glb")
    # 阶段 10：deprecated 包装器全量转发——补齐与 run-tower 一致的参数，
    # 避免旧命令因缺参而静默丢失 scale/mm_per_px/input_dir/ocr_fallback。
    p_deliver.add_argument("--scale", help="扫描图比例尺")
    p_deliver.add_argument("--mm-per-px", type=float, help="扫描图 mm/px")
    p_deliver.add_argument("--no-ocr-fallback", action="store_true",
                           help="扫描图 A1 件号 OCR 不用 Tesseract 兜底")
    p_deliver.add_argument("--input-dir", help="批量模式：目录内全部 DWG/DXF（A3）")
    p_deliver.add_argument("--agent-mode", choices=["ezdxf", "hybrid"], default="ezdxf",
                           help="单文件 DXF 几何后端：ezdxf（默认）/ hybrid（MLLM Agent 链）")
    p_deliver.set_defaults(func=cmd_deliver_tower)

    p_confirm = sub.add_parser("confirm-scan", help="人工确认扫描候选（P2-5）")
    p_confirm.add_argument("file")
    p_confirm.set_defaults(func=cmd_confirm_scan)

    p_confirm_y = sub.add_parser("confirm-derived-y", help="人工复核 cross_file z-peer 插值 y")
    p_confirm_y.add_argument("file")
    p_confirm_y.set_defaults(func=cmd_confirm_derived_y)

    p_mscan = sub.add_parser("merge-scans", help="front+side 扫描图融合（P2-4）")
    p_mscan.add_argument("--front", required=True)
    p_mscan.add_argument("--side", required=True)
    p_mscan.add_argument("--out", default="scan_merged.json")
    p_mscan.add_argument("--scale", help="图纸比例")
    p_mscan.add_argument("--mm-per-px", type=float)
    p_mscan.set_defaults(func=cmd_merge_scans)

    # ---- 阶段 3：验证与交付 ----
    p_har = sub.add_parser("harness", help="Agent Harness 自动验证规则")
    p_har.add_argument("file")
    p_har.add_argument("--rule", action="append", help="只验证指定规则（不指定则全部）")
    p_har.set_defaults(func=cmd_harness)

    p_exp = sub.add_parser("export", help="导出 Neo4j / GEXF / 报告 / OBJ / GLB")
    p_exp.add_argument("file")
    p_exp.add_argument("--format", choices=["cypher", "gexf", "report", "obj", "glb", "dxf", "step"], required=True)
    p_exp.add_argument("--out", help="输出文件前缀")
    p_exp.set_defaults(func=cmd_export)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
