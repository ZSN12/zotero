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

    返回与 run_tower 兼容的 dict（含 model_path / steps_path / ok）。
    """
    import json as _json
    from ..model import EngineeringModel
    from .tower_agent_pipeline import run_tower_agent_pipeline

    input_dir = Path(input_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    grouped = scan_dir_files(input_dir)
    parse_files = grouped["parse_files"]

    if not parse_files:
        raise ValueError(f"目录内没有可解析的扫描图（PNG/PDF/JPG）：{input_dir}")

    models: Dict[str, EngineeringModel] = {}
    per_file: List[Dict[str, Any]] = []
    steps_all: List[Dict[str, Any]] = []

    for path in parse_files:
        stem = Path(path).stem
        meta = infer_scan_view_meta(path)
        vt = meta.get("view_type", "drawing")
        file_out = out_dir / f"run-{stem}"
        try:
            result = run_tower_agent_pipeline(
                path, file_out, mllm=mllm, filter_noise=filter_noise,
                label_snap_px=label_snap_px,
            )
            from ..io import load_model
            model = load_model(file_out / "model.json")
            # plan 视图写 z_level 到节点
            if vt == "plan" and meta.get("z_level") is not None:
                for c in model.components.values():
                    if c.kind in ("tower_node", "tower_bar"):
                        c.properties["z_level"] = meta["z_level"]
            models[vt] = model
            per_file.append({
                "file": stem, "view_type": vt, "ok": result.get("ok"),
                "bars": sum(1 for c in model.components.values() if c.kind == "tower_bar"),
                "nodes": sum(1 for c in model.components.values() if c.kind == "tower_node"),
            })
            if result.get("steps_path"):
                try:
                    steps_all.append(_json.loads(Path(result["steps_path"]).read_text(encoding="utf-8")))
                except Exception:
                    pass
        except Exception as exc:
            per_file.append({"file": stem, "view_type": vt, "ok": False, "error": str(exc)})

    # front + side 融合
    merged_model = None
    if "front" in models and "side" in models:
        from .tower_scan_merge import merge_scan_views
        merged_model = merge_scan_views(models["front"], models["side"])
    elif models:
        # 只有单视图：取第一个作为主模型
        merged_model = next(iter(models.values()))

    model_path = None
    if merged_model is not None:
        from ..io import save_model
        model_path = out_dir / "model.json"
        save_model(merged_model, model_path)

    # 汇总 steps.json
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
    if steps_all:
        (out_dir / "steps.json").write_text(
            _json.dumps({"steps": [s for grp in steps_all for s in grp.get("steps", [])]},
                        ensure_ascii=False, indent=2), encoding="utf-8")

    ok = all(p.get("ok", False) for p in per_file)
    return {
        "ok": ok,
        "model_path": model_path,
        "steps_path": (out_dir / "steps.json").as_posix() if (out_dir / "steps.json").exists() else None,
        "batch_report": (out_dir / "batch_report.json").as_posix(),
        "summary": summary,
    }
