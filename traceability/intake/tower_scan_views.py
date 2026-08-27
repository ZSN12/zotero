"""扫描图 / 多视图文件名语义推断（front / side / plan / section）。

单张 PNG 往往对应一个视图；完整铁塔应分别跑 front+side+plan，
再经 merge-scans 或 intake_scan_batch 合并。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional


def infer_scan_view_meta(path: str | Path) -> Dict[str, Any]:
    """从路径 stem 推断视图类型与是否参与杆件解析。

    返回：
        view_type: front | side | plan | section | elevation | detail | bom | drawing
        z_level: plan 标高（mm），无则 None
        parse_bars: 是否跑霍夫/agent 杆件链
        title: 人类可读标题
    """
    stem = Path(path).stem
    low = stem.lower()

    if any(k in low for k in ("bom", "明细", "材料表")):
        return {
            "view_type": "bom",
            "z_level": None,
            "parse_bars": False,
            "title": "材料表",
        }
    if any(k in low for k in ("node", "节点", "k1", "k2", "detail", "大样")):
        return {
            "view_type": "detail",
            "z_level": None,
            "parse_bars": False,
            "title": "节点大样",
        }
    if "section" in low or "剖" in stem:
        return {
            "view_type": "section",
            "z_level": None,
            "parse_bars": True,
            "title": "剖面",
        }
    if "side" in low or "侧立面" in stem or "侧面" in stem:
        return {
            "view_type": "side",
            "z_level": None,
            "parse_bars": True,
            "title": "侧立面",
        }
    if "front" in low or "正立面" in stem or "正面" in stem:
        return {
            "view_type": "front",
            "z_level": None,
            "parse_bars": True,
            "title": "正立面",
        }
    if "elevation" in low or "立面" in stem:
        return {
            "view_type": "elevation",
            "z_level": None,
            "parse_bars": True,
            "title": "立面",
        }
    if "plan" in low or "平面" in stem:
        z_level: Optional[float] = None
        m = re.search(r"z[_-]?(\d+)", low)
        if m:
            z_level = float(m.group(1))
        title = f"平面 Z={z_level}" if z_level is not None else "平面"
        return {
            "view_type": "plan",
            "z_level": z_level,
            "parse_bars": True,
            "title": title,
        }
    return {
        "view_type": "drawing",
        "z_level": None,
        "parse_bars": True,
        "title": stem,
    }


def apply_scan_view_meta(view: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """把推断的 view_type / z_level 写入 A0 drawing_view dict。"""
    out = dict(view)
    out["view_type"] = meta.get("view_type") or "drawing"
    if meta.get("z_level") is not None:
        out["z_level"] = meta["z_level"]
    out["title"] = meta.get("title")
    return out


def scan_dir_files(input_dir: str | Path) -> Dict[str, Any]:
    """扫描目录里的位图/PDF，按文件名语义分组（P1-1）。

    返回 {
        "front": [paths], "side": [paths], "plan": [paths],
        "section": [paths], "detail": [paths], "bom": [paths],
        "others": [paths],
        "parse_files": [paths],   # parse_bars=True 的文件
        "skip_files": [paths],    # bom/node 大样等 parse_bars=False
    }
    """
    input_dir = Path(input_dir)
    exts = {".png", ".jpg", ".jpeg", ".pdf"}
    files = sorted(p for p in input_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in exts)

    buckets: Dict[str, List[str]] = {
        "front": [], "side": [], "plan": [], "section": [],
        "detail": [], "bom": [], "others": [],
    }
    parse_files: List[str] = []
    skip_files: List[str] = []
    for p in files:
        meta = infer_scan_view_meta(p)
        vt = meta.get("view_type", "drawing")
        key = vt if vt in buckets else "others"
        buckets[key].append(str(p))
        if meta.get("parse_bars", True):
            parse_files.append(str(p))
        else:
            skip_files.append(str(p))

    return {
        "front": buckets["front"], "side": buckets["side"],
        "plan": buckets["plan"], "section": buckets["section"],
        "detail": buckets["detail"], "bom": buckets["bom"],
        "others": buckets["others"],
        "parse_files": parse_files,
        "skip_files": skip_files,
        "all_files": [str(p) for p in files],
    }


def intake_scan_batch(
    input_dir: str | Path,
    out_dir: str | Path,
    mllm=None,
    filter_noise: bool = True,
    label_snap_px: float = 400.0,
) -> Dict[str, Any]:
    """扫描目录批量：front+side 合并 + plan 写 z_level，跳过 bom/node（P1-1）。

    策略：
        * 每个 parse_bars=True 的文件跑一遍 A0→A4 agent 链（无 API 时 A1 跳过）
        * 有 front + side → merge_scan_views 融合为候选 3D
        * 有 plan → 把 z_level 写入对应节点的 properties
        * bom / node 大样 → 跳过（parse_bars=False），记录进报告
        * 输出合并 model.json + steps.json + batch_report.json

    P0-4：models 按 (view_type, z_level) 存，多个 plan_z0/z8100/z16200 不再互相
    覆盖，全部保留；merge 后把各 plan 节点的 (view_x, view_y, z_level) 写入合并模型。

    P1-9：返回完整 ProcessingGraph（每文件一步 + merge_scan 一步 + a4_harness），
    与单文件 agent 结构对齐，供 web 前端按步骤展示。

    返回与 run_tower 兼容的 dict（含 model_path / steps_path / graph / ok）。
    """
    import json as _json
    from ..model import EngineeringModel
    from ..harness.processing_graph import ProcessingGraph
    from .tower_agent_pipeline import run_tower_agent_pipeline

    input_dir = Path(input_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    grouped = scan_dir_files(input_dir)
    parse_files = grouped["parse_files"]

    graph = ProcessingGraph(name=f"tower-scan-batch-{input_dir.stem}")
    if not parse_files:
        graph.fail(f"目录内没有可解析的扫描图（PNG/PDF/JPG）：{input_dir}")
        steps_path = out_dir / "steps.json"
        graph.export_json(steps_path)
        return {"ok": False, "graph": graph, "steps_path": steps_path.as_posix()}

    # P0-4：models 按 (view_type, z_level) 存，避免多个 plan 相互覆盖
    models: Dict[tuple, EngineeringModel] = {}
    per_file: List[Dict[str, Any]] = []

    for path in parse_files:
        stem = Path(path).stem
        meta = infer_scan_view_meta(path)
        vt = meta.get("view_type", "drawing")
        z_level = meta.get("z_level")
        file_out = out_dir / f"run-{stem}"
        graph.start(f"intake:{stem}", f"intake {stem}", input=str(path))
        try:
            result = run_tower_agent_pipeline(
                path, file_out, mllm=mllm, filter_noise=filter_noise,
                label_snap_px=label_snap_px,
            )
            from ..io import load_model
            model = load_model(file_out / "model.json")
            # plan 视图写 z_level 到节点/杆件
            if vt == "plan" and z_level is not None:
                for c in model.components.values():
                    if c.kind in ("tower_node", "tower_bar"):
                        c.properties["z_level"] = z_level
            key = (vt, z_level)
            models[key] = model
            n_bars = sum(1 for c in model.components.values() if c.kind == "tower_bar")
            n_nodes = sum(1 for c in model.components.values() if c.kind == "tower_node")
            per_file.append({
                "file": stem, "view_type": vt, "z_level": z_level, "ok": result.get("ok"),
                "bars": n_bars, "nodes": n_nodes,
            })
            graph.finish(view_type=vt, z_level=z_level, bars=n_bars, nodes=n_nodes,
                         ok=result.get("ok", False))
        except Exception as exc:
            per_file.append({"file": stem, "view_type": vt, "ok": False, "error": str(exc)})
            graph.fail(str(exc))

    # front + side 融合
    merged_model = None
    graph.start("merge_scan", "front+side 视图融合", input=f"{len(models)} 个视图模型")
    try:
        front = next((m for (vt, z), m in models.items() if vt == "front"), None)
        side = next((m for (vt, z), m in models.items() if vt == "side"), None)
        if front is not None and side is not None:
            from .tower_scan_merge import merge_scan_views
            merged_model = merge_scan_views(front, side)
        elif models:
            # 只有单视图：取第一个作为主模型
            merged_model = next(iter(models.values()))

        # P0-4：把各 plan 的节点按 view_x/view_y/z_level 写入合并模型（复用
        # tower_views plan 分支的语义：plan 提供该层的 x/y，z 由 z_level 给出）
        if merged_model is not None:
            _attach_plan_nodes(merged_model, models)
        graph.finish(merged=merged_model is not None,
                     n_nodes=sum(1 for c in merged_model.components.values()
                                 if c.kind == "tower_node") if merged_model else 0)
    except Exception as exc:
        graph.fail(str(exc))

    model_path = None
    if merged_model is not None:
        from ..io import save_model
        model_path = out_dir / "model.json"
        save_model(merged_model, model_path)

    # P1-9：a4_harness 一步（与单文件 agent 结构对齐）
    graph.start("a4_harness", "编译验证（A4）", input="scan batch merge")
    if merged_model is not None:
        try:
            from ..intake.tower_pipeline import finalize_tower_model
            from ..harness.harness import run_harness
            from ..io import validate_references
            merged_model = finalize_tower_model(merged_model, merge=False, allow_scan=False)
            problems = validate_references(merged_model)
            results = run_harness(merged_model)
            status_counts = {}
            for r in results:
                status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1
            failed = [r.target_id for r in results if r.status.value == "failed"]
            pending = [r.target_id for r in results if r.status.value == "pending"]
            if problems:
                graph.fail(f"引用完整性 {len(problems)} 项", problems=problems[:10],
                           summary=status_counts)
            elif failed or pending:
                graph.pending("扫描图待人工复核（pending_review）/ 规则 pending",
                              summary=status_counts, failed_rules=failed,
                              pending_rules=pending)
            else:
                graph.finish(summary=status_counts)
            save_model(merged_model, model_path)
        except Exception as exc:
            graph.fail(str(exc))
    else:
        graph.skip("a4_harness", "编译验证（A4）", "无合并模型")

    steps_path = out_dir / "steps.json"
    graph.export_json(steps_path)

    # 汇总 batch_report.json
    summary = {
        "input_dir": str(input_dir),
        "parse_files": [str(p) for p in parse_files],
        "skip_files": [str(p) for p in grouped["skip_files"]],
        "grouped": {k: [str(p) for p in v] for k, v in grouped.items()
                    if k not in ("parse_files", "skip_files", "all_files")},
        "per_file": per_file,
        "model_path": str(model_path) if model_path else None,
        "merged": merged_model is not None,
    }
    (out_dir / "batch_report.json").write_text(
        _json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = all(s.status != "failed" for s in graph.steps)
    return {
        "ok": ok,
        "graph": graph,
        "model_path": str(model_path) if model_path else None,
        "steps_path": steps_path.as_posix(),
        "batch_report": (out_dir / "batch_report.json").as_posix(),
        "summary": summary,
    }


def _attach_plan_nodes(
    merged_model: EngineeringModel,
    models: Dict[tuple, EngineeringModel],
) -> None:
    """P0-4：把各 plan 视图的节点坐标 (view_x/view_y/z_level) 并入合并模型。

    plan 提供某标高的水平定位 (x, y)，z 由 z_level 给出。这里把每个 plan 模型的
    tower_node 复制进 merged_model（加 plan 前缀避免 ID 冲突），属性写
    x/y/z = (view_x, view_y, z_level)（scan 图中即 x_px/y_px + z_level），
    供后续 solve/报告使用。不臆造：缺轴保持 None。
    """
    from ..model import Component

    for (vt, z_level), model in models.items():
        if vt != "plan":
            continue
        prefix = f"plan_z{z_level}_" if z_level is not None else "plan_"
        for cid, comp in model.components.items():
            if comp.kind != "tower_node":
                continue
            p = comp.properties
            x = p.get("x_px", p.get("view_x"))
            y = p.get("y_px", p.get("view_y"))
            new_id = f"{prefix}{cid}"
            if new_id in merged_model.components:
                continue
            merged_model.add_component(Component(
                id=new_id,
                name=f"[plan z={z_level}] {comp.name}",
                kind="tower_node",
                source=comp.source,
                properties={
                    "node_id": p.get("node_id"),
                    "x_px": x,
                    "y_px": y,
                    "view_x": x,
                    "view_y": y,
                    "z_level": z_level,
                    "view_type": "plan",
                    "unit": "px",
                    "solve_status": "pending_review",
                },
            ))
