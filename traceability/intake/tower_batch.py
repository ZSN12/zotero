"""铁塔批量接入（A3 / B7 / F2）。

目录内全部 DWG → DXF → 逐文件 intake，汇总 layer 报告；
可选把同一塔型多文件合并为一个 EngineeringModel。

原则：
    * 每个输入文件都有一条 per-file 结果（F2 steps.json 数据源）
    * 图签/明细类文件标记 drawing_kind，不计入「杆件解析失败」
    * 合并时节点保留来源文件与 placeholder 语义，绝不臆造坐标
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..model import Dimension, EngineeringModel, SourceRef, SourceType
from .dwg import ensure_dxf_batch
from .tower_dxf import classify_drawing_kind, extract_tower_from_dxf, layer_usage_report


def intake_tower_batch(
    input_dir: str | Path,
    out_dir: str | Path,
    layer_map_path: Optional[str | Path] = None,
    merge: bool = False,
) -> Dict[str, Any]:
    """批量接入目录内全部 DWG/DXF。

    返回 {
        "files": [...],         # 每文件 {file, dxf, kind, bars, nodes, layers, error}
        "layer_report": {...},  # 汇总图层使用
        "model_path": Optional[str],   # merge=True 时写出的合并模型
        "ok": bool,
    }
    """
    input_dir = Path(input_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # A3：目录内全部 DWG 转 DXF
    dxf_dir = out_dir / "dxf"
    dxf_dir.mkdir(parents=True, exist_ok=True)
    dxf_paths = ensure_dxf_batch(input_dir, dxf_dir)

    files: List[Dict[str, Any]] = []
    models: List[EngineeringModel] = []
    layer_aggregate: Dict[str, Any] = {"files": {}, "total_entities": 0}
    all_ok = True

    for dxf in sorted(dxf_paths):
        stem = Path(dxf).stem
        kind = classify_drawing_kind(stem)
        entry: Dict[str, Any] = {
            "file": stem,
            "dxf": dxf,
            "kind": kind["kind"],
            "parse_bars": kind["parse_bars"],
            "bars": 0,
            "nodes": 0,
            "labeled": 0,
            "association_rate": 0.0,
            "error": None,
        }
        try:
            usage = layer_usage_report(dxf, layer_map_path=layer_map_path)
            entry["layers"] = usage
            layer_aggregate["files"][stem] = usage
            layer_aggregate["total_entities"] += usage.get("total_entities", 0)

            model = extract_tower_from_dxf(dxf, layer_map_path=layer_map_path)
            bars = [c for c in model.components.values() if c.kind == "tower_bar"]
            nodes = [c for c in model.components.values() if c.kind == "tower_node"]
            labeled = [c for c in bars if not str(c.properties.get("bar_id", "")).startswith("UNLABELED")]
            entry.update({
                "bars": len(bars),
                "nodes": len(nodes),
                "labeled": len(labeled),
                "association_rate": round(len(labeled) / len(bars), 4) if bars else 0.0,
            })
            # 图签/明细不计入解析失败（B2）
            if not kind["parse_bars"]:
                entry["note"] = kind["reason"]
            models.append(model)
        except Exception as exc:  # 单文件失败不中断整批
            entry["error"] = str(exc)
            all_ok = False

        files.append(entry)

    model_path: Optional[str] = None
    if merge and models:
        merged = merge_tower_models(models)
        model_path = (out_dir / "model.json").as_posix()
        from ..io import save_model
        save_model(merged, model_path)

    # per-file 结果写入 batch_report.json（F2 steps.json 数据源）
    (out_dir / "batch_report.json").write_text(
        json.dumps({"ok": all_ok, "files": files, "layer_report": layer_aggregate},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "ok": all_ok,
        "files": files,
        "layer_report": layer_aggregate,
        "model_path": model_path,
        "batch_report": (out_dir / "batch_report.json").as_posix(),
    }


def merge_tower_models(models: List[EngineeringModel]) -> EngineeringModel:
    """合并多个图纸文件模型（B7 单文件无法 --merge 的替代路径）。

    规则：
        * 图纸上下文组件按来源文件各自保留
        * tower_node / tower_bar 加 source_file 前缀避免 ID 冲突，
          并写入 drawing_view = 来源文件 stem
        * 坐标不臆造：已有 x/y 保留，z 缺失保持 None（placeholder）
        * 规则与依赖在调用方 finalize 阶段重新注入
    """
    merged = EngineeringModel(name="tower-batch-merged")

    # 第一遍：组件与尺寸全部前缀重指
    for i, model in enumerate(models, start=1):
        stem = model.name.removeprefix("tower-") if model.name.startswith("tower-") else model.name
        prefix = f"f{i:02d}_"

        for cid, comp in model.components.items():
            new_id = f"{prefix}{cid}"
            props = dict(comp.properties)
            props.setdefault("drawing_view", stem)
            props.setdefault("source_file", stem)
            merged.add_component(type(comp)(
                id=new_id,
                name=f"[{stem}] {comp.name}",
                kind=comp.kind,
                source=comp.source,
                properties=props,
                tags=list(comp.tags),
            ))

        for did, dim in model.dimensions.items():
            merged.add_dimension(Dimension(
                id=f"{prefix}{did}",
                name=dim.name,
                value=dim.value,
                unit=dim.unit,
                origin=dim.origin,
                source=dim.source,
                applies_to=(f"{prefix}{dim.applies_to}" if dim.applies_to else None),
                status=dim.status,
            ))

        for node, ups in model.dependencies.items():
            new_node = f"{prefix}{node}" if node in model.components or node in model.dimensions else node
            new_ups = {f"{prefix}{u}" if u in model.components or u in model.dimensions else u for u in ups}
            merged.dependencies.setdefault(new_node, set()).update(new_ups)

    # 第二遍：杆件 from/to 节点引用按各自来源文件重指
    for model_index, model in enumerate(models, start=1):
        prefix = f"f{model_index:02d}_"
        for cid, comp in model.components.items():
            if comp.kind != "tower_bar":
                continue
            new_bar = merged.components[f"{prefix}{cid}"]
            for end in ("from_node", "to_node"):
                nid = comp.properties.get(end)
                if nid and nid in model.components:
                    new_bar.properties[end] = f"{prefix}{nid}"
    return merged
