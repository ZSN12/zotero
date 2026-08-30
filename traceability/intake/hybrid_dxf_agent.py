"""DXF hybrid Agent 链（Phase 1）：矢量几何 + 多模态 A1 件号。

A2 几何来自 ezdxf（`extract_tower_from_dxf`），A1 件号来自可插拔
`MLLMBackend`（OpenAI 兼容；提供商由 `MLLM_PROVIDER` / 环境变量决定，
不绑定任何单一厂商）。A3 确定性关联，A4 Skill + Harness。

与纯扫描 Agent 链的区别：杆件拓扑与坐标以 DXF 为准，多模态只负责
读标注/件号（及可选版面语义），不把整塔几何交给 VLM。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..harness.harness import run_harness, summarize
from ..harness.processing_graph import ProcessingGraph
from ..model import Component, EngineeringModel, SourceRef, SourceType, ValidationStatus
from .mllm_backend import MLLMBackend
from .mllm_tower_prompt import (
    LABEL_AGENT_PROMPT,
    LABEL_AGENT_SCHEMA,
    parse_label_agent_output,
)
from .tower_agent_pipeline import (
    LABEL_SNAP_PX,
    MIN_ASSOCIATION_RATE,
    _associate_labels,
    _labels_to_full_image,
    _mllm_detect_geometry,
    _ocr_labels_from_tesseract,
)
from .tower_dxf import extract_tower_from_dxf, resolve_drawing_kind, _compile_bar_id_re, _extract_bar_label
from .pipeline_stages import (
    STAGE_LAYOUT,
    STAGE_LABELS,
    STAGE_LABELS_OCR_FALLBACK,
    STAGE_GEOMETRY,
    STAGE_LINK,
    STAGE_HARNESS,
)


# DXF 图纸坐标下件号→杆件中点的最大距离（mm），与矢量 TEXT_SNAP 同量级
LABEL_SNAP_MM = 500.0
DEFAULT_PREVIEW_DPI = 800

# 管线版本：几何/件号提取逻辑语义变更时递增，使旧缓存自动失效。
PIPELINE_VERSION = "hybrid-dxf-v1"


def build_pipeline_fingerprint(
    provider: str,
    model: str,
    dpi: int,
    geom_method: str,
    layer_map_path: Optional[str | Path],
    prompts: str,
) -> Dict[str, Any]:
    """统一缓存指纹：pipeline 写入与批跑读取必须共用此函数。

    指纹至少包含 provider / model / dpi / geom_method / layer_map_sha /
    prompt_sha / pipeline_version，保证同配置二次执行全部 skip、修改 prompt
    或 overlay 后自动重跑。任何一侧自行拼接字段都会导致缓存无法命中。
    """
    import hashlib

    fp: Dict[str, Any] = {
        "provider": provider,
        "model": model,
        "dpi": int(dpi),
        "geom_method": geom_method,
        "pipeline_version": PIPELINE_VERSION,
        "prompt_sha": hashlib.sha256(prompts.encode("utf-8")).hexdigest()[:16],
    }
    if layer_map_path:
        lp = Path(layer_map_path)
        if lp.exists():
            fp["layer_map_sha"] = hashlib.sha256(lp.read_bytes()).hexdigest()[:16]
    return fp


def render_dxf_preview_with_mapping(
    dxf_path: str | Path,
    png_path: str | Path,
    *,
    dpi: int = 200,
) -> Dict[str, Any]:
    """将 DXF 渲成 PNG，并返回图纸坐标 ↔ 像素仿射所需元数据。"""
    import matplotlib
    matplotlib.use("Agg")
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext, config
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    import matplotlib.pyplot as plt
    from PIL import Image

    dxf_path = Path(dxf_path)
    png_path = Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)

    doc = ezdxf.readfile(str(dxf_path))
    ctx = RenderContext(doc)
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE)
    fig = plt.figure(figsize=(24, 16), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_aspect("equal")
    backend = MatplotlibBackend(ax)
    Frontend(ctx, backend, config=cfg).draw_layout(doc.modelspace())
    ax.autoscale()
    ax.axis("off")
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()

    # 像素↔图纸坐标必须是严格线性仿射。之前直接记 ax.get_xlim()，但
    # savefig(bbox_inches="tight", pad_inches=0.05) 会在内容边界外加 padding，
    # 使图像边缘≠xlim/ylim，MLLM 像素坐标→DXF 图纸坐标出现系统偏差。
    # 这里用 get_tightbbox 求出裁剪后图像边缘对应的真实数据范围，
    # 写入 mapping（px_to_drawing_xy 线性假设随之成立）。
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    tight = ax.get_tightbbox(renderer)
    # tight bbox 是 display 坐标，转回数据坐标得真实数据范围
    (x0_data, y0_data) = ax.transData.inverted().transform((tight.x0, tight.y0))
    (x1_data, y1_data) = ax.transData.inverted().transform((tight.x1, tight.y1))
    xlim_real = (min(x0_data, x1_data), max(x0_data, x1_data))
    ylim_real = (min(y0_data, y1_data), max(y0_data, y1_data))

    fig.savefig(str(png_path), bbox_inches="tight", pad_inches=0)
    plt.close(fig)

    img = Image.open(png_path)
    return {
        "png": str(png_path),
        "width": img.width,
        "height": img.height,
        "xlim": (float(xlim_real[0]), float(xlim_real[1])),
        "ylim": (float(ylim_real[0]), float(ylim_real[1])),
        "dpi": dpi,
    }


# P1 拆分：像素↔图纸坐标变换与 MLLM/Hough 杆件注入已迁到 hybrid_geometry。
# 这里 re-import 保留旧名，避免破坏外部引用。
from .hybrid_geometry import (  # noqa: F401
    px_to_drawing_xy,
    drawing_xy_to_px,
    region_drawing_bbox as _region_drawing_bbox,
    drawing_region_to_pixel_bbox as _drawing_region_to_pixel_bbox,
    bars_px_to_drawing as _bars_px_to_drawing,
    hough_bars_to_drawing as _hough_bars_to_drawing,
    drawing_xy_to_view_xy as _drawing_xy_to_view_xy,
    inject_mllm_bars_into_model as _inject_mllm_bars_into_model,
    stitch_mllm_diagonals as _stitch_mllm_diagonals,
)


def _dxf_model_to_agent_bars(model: EngineeringModel) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把 DXF 模型杆件转为 A3 关联用的 bar 列表（图纸坐标 mm）。

    返回 (bars, bar_component_ids) 平行列表，bar_uid 与组件 id 一一对应。
    """
    nodes = {
        cid: c for cid, c in model.components.items() if c.kind == "tower_node"
    }
    bars: List[Dict[str, Any]] = []
    comp_ids: List[str] = []
    idx = 0
    for cid, comp in model.components.items():
        if comp.kind != "tower_bar":
            continue
        idx += 1
        props = comp.properties
        fn, tn = props.get("from_node"), props.get("to_node")
        nf, nt = nodes.get(fn), nodes.get(tn)
        if nf is None or nt is None:
            continue
        x1, y1 = float(nf.properties["x"]), float(nf.properties["y"])
        x2, y2 = float(nt.properties["x"]), float(nt.properties["y"])
        bars.append({
            "bar_uid": f"bar_{idx:04d}",
            "component_id": cid,
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "view_type": props.get("view_type") or nf.properties.get("view_type"),
        })
        comp_ids.append(cid)
    return bars, comp_ids


def _mllm_labels_from_png(
    png_path: str,
    views: List[Dict[str, Any]],
    crops_dir: Path,
    mllm: MLLMBackend,
    mapping: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """A1：调用可插拔 MLLM 读件号，像素坐标转图纸坐标。"""
    from .tower_agent_pipeline import _crop_view

    labels: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {
        "provider": mllm.provider,
        "model": mllm.model,
        "failed_calls": 0,
        "warnings": [],
    }
    last_meta: Dict[str, Any] = {}
    for view in views:
        try:
            crop = _crop_view(png_path, view["bbox"], crops_dir, view["view_id"])
            view["crop"] = crop
            parsed, call_meta = mllm.call_agent_json(
                LABEL_AGENT_PROMPT, crop["path"], LABEL_AGENT_SCHEMA, agent="a1_labels",
            )
            last_meta = call_meta
            if parsed is None:
                meta["failed_calls"] += 1
                meta["warnings"].append(
                    f"{view['view_id']}: {call_meta.get('failure_reason', 'MLLM 调用失败')}"
                )
                continue
            view_labels, problems, warn = parse_label_agent_output(parsed)
            meta["warnings"].extend(warn)
            if problems:
                meta["failed_calls"] += 1
                meta["warnings"].extend(problems)
                continue
            for lab in _labels_to_full_image(view_labels, crop, view["view_id"]):
                px, py = float(lab["x_px"]), float(lab["y_px"])
                dx, dy = px_to_drawing_xy(px, py, mapping)
                lab = dict(lab)
                # P3-8 单位规范：转成图纸 mm 后键名改为 drawing_x/drawing_y，
                # 严禁用 x_px/y_px 存 mm 值（键名带 _px 后缀但值是 mm 属单位混写）。
                lab["drawing_x"] = round(dx, 2)
                lab["drawing_y"] = round(dy, 2)
                lab["coord_space"] = "drawing_mm"
                labels.append(lab)
        except Exception as exc:
            meta["failed_calls"] += 1
            meta["warnings"].append(f"{view['view_id']}: {exc}")
    meta["mllm_duration_ms"] = last_meta.get("duration_ms")
    meta["mllm_elapsed_s"] = last_meta.get("elapsed_s")
    return labels, meta


def _apply_assignments_to_dxf_model(
    model: EngineeringModel,
    bars: List[Dict[str, Any]],
    assignments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """把 A3 关联结果写回 DXF EngineeringModel 的 tower_bar。"""
    by_uid = {a["bar_uid"]: a for a in assignments}
    updated = 0
    vector_prelabeled = sum(
        1 for bar in bars
        if (comp := model.components.get(bar["component_id"])) is not None
        and (bid := str(comp.properties.get("bar_id") or ""))
        and not bid.startswith("UNLABELED")
    )
    mllm_labeled = 0
    for bar in bars:
        uid = bar["bar_uid"]
        assign = by_uid.get(uid)
        if not assign:
            continue
        cid = bar["component_id"]
        comp = model.components.get(cid)
        if comp is None:
            continue
        new_id = str(assign["bar_id"])
        if new_id.startswith("UNLABELED"):
            continue
        old_id = comp.properties.get("bar_id")
        if old_id and not str(old_id).startswith("UNLABELED"):
            continue  # 保留矢量贴号，MLLM 只补空白
        comp.properties["bar_id"] = new_id
        comp.properties["label_origin"] = "mllm_a3_hybrid"
        # 阶段 6.4 单位规范：只接受毫米距离（coord_space="mm"），严禁把
        # 像素距离 label_distance_px 冒充 label_distance_mm。
        if assign.get("label_distance_mm") is not None:
            comp.properties["label_distance_mm"] = round(assign["label_distance_mm"], 2)
        comp.properties["association_confidence"] = assign.get("confidence", 0.75)
        updated += 1
        mllm_labeled += 1
    return {
        "bars_updated": updated,
        "vector_prelabeled": vector_prelabeled,
        "mllm_labeled": mllm_labeled,
    }


def _layout_views_for_overlay(
    stem: str,
    mapping: Dict[str, Any],
    layer_map_path: Optional[str | Path],
    png_path: str,
) -> List[Dict[str, Any]]:
    """A0：优先用 overlay view_regions 切视图（立面/大样局部放大给 MLLM）。"""
    from .tower_spec import view_regions

    from PIL import Image

    img = Image.open(png_path)
    w, h = img.size
    from .tower_spec import canonical_view_type, is_ortho_view_type
    kind = resolve_drawing_kind(stem, overlay=layer_map_path)
    raw_vt = canonical_view_type(str(kind.get("kind") or ""))
    default_vt = raw_vt if raw_vt in ("front", "plan", "side", "detail") else "front"

    # 子区域切分白名单：只对斜腹杆密集、单区漏检的段切分
    # （02/04/07/40 单区检测已够好，切分反而过度检测）
    subdivide_by_stem: Dict[str, bool] = {}
    if layer_map_path:
        try:
            _ov = json.loads(Path(layer_map_path).read_text(encoding="utf-8"))
            subdivide_by_stem = {
                str(k): bool(v) for k, v in _ov.get("subdivide_views_by_stem", {}).items()
            }
        except Exception:
            subdivide_by_stem = {}

    views: List[Dict[str, Any]] = []
    for i, reg in enumerate(view_regions(stem, overlay=layer_map_path)):
        region = list(reg.get("region") or [])
        if len(region) < 4:
            continue
        bbox = _drawing_region_to_pixel_bbox(region, mapping)
        if bbox[2] - bbox[0] < 20 or bbox[3] - bbox[1] < 20:
            continue
        vk = canonical_view_type(str(reg.get("kind") or default_vt))
        views.append({
            "view_id": f"{stem}_{vk}_{i}",
            "view_type": vk if vk in ("front", "plan", "side", "detail") else default_vt,
            "title": reg.get("title") or f"{stem}-{vk}",
            "bbox": bbox,
            "overlay_region": region,
            "subdivide": bool(subdivide_by_stem.get(stem)),
        })
    if not views:
        views = [{
            "view_id": "whole_sheet",
            "view_type": default_vt,
            "title": stem,
            "bbox": [0, 0, w, h],
        }]
    return views


def _extract_dxf_text_labels(
    dxf_path: Path,
    layer_map_path: Optional[str | Path],
) -> List[Dict[str, Any]]:
    """从 DXF TEXT/MTEXT 提取件号候选（图纸坐标），供 A3 与 MLLM 合并。"""
    import ezdxf
    from .tower_dxf import _flatten_modelspace_entities, _layer_hit, DEFAULT_LAYER_MAP
    from .tower_spec import layer_names

    text_layers = layer_names(
        "text_layers", DEFAULT_LAYER_MAP["text_layers"], overlay=layer_map_path,
    )
    bar_id_re = _compile_bar_id_re()
    doc = ezdxf.readfile(str(dxf_path))
    labels: List[Dict[str, Any]] = []
    for e in _flatten_modelspace_entities(doc.modelspace()):
        layer = getattr(e.dxf, "layer", "0")
        if not _layer_hit(layer, text_layers):
            continue
        if e.dxftype() == "TEXT":
            text = e.dxf.text
            ins = e.dxf.insert
        elif e.dxftype() == "MTEXT":
            text = e.text
            ins = e.dxf.insert
        else:
            continue
        bar_id = _extract_bar_label(str(text or ""), bar_id_re)
        if not bar_id:
            continue
        labels.append({
            "text": str(text).strip(),
            "bar_id": bar_id,
            "x_px": round(float(ins.x), 2),
            "y_px": round(float(ins.y), 2),
            "coord_space": "drawing_mm",
            "label_source": "dxf_text",
        })
    return labels


def _vector_labeled_count(model: EngineeringModel, bars: List[Dict[str, Any]]) -> int:
    n = 0
    for bar in bars:
        comp = model.components.get(bar["component_id"])
        if comp is None:
            continue
        bid = str(comp.properties.get("bar_id") or "")
        if bid and not bid.startswith("UNLABELED"):
            n += 1
    return n


def _strip_vector_geometry(model: EngineeringModel) -> int:
    """清除 ezdxf/hough 产生的杆件与节点，供 MLLM 几何「替换」而非「追加」。

    P0-1 后续隐患：extract_tower_from_dxf 对 04-07 双线角钢图常产出 layer-0
    垃圾几何（数百根碎杆）。若 MLLM 检测成功却只「追加」到 ezdxf 几何旁，
    merge 后仍混入冗余杆件。这里在 MLLM 注入前清空所有 tower_node/tower_bar，
    使 MLLM 成为唯一几何来源（MLLM 失败时才回退 ezdxf/hough）。

    除删除 components 外，同步清理引用这些组件 id 的：
      * connections（from/to 指向被删 node/bar 的连接）
      * rules（applies_to 非空且指向被删 bar/node 的规则；空 applies_to 的
        全局规则保留，避免误删）
      * dimensions（applies_to 指向被删杆件/节点的尺寸）
      * dependencies（upstream/downstream 指向被删组件）
      * staleness（被删组件 id 的陈旧标记）
    否则留下悬空引用，后续 validate_references / Harness 会误报。

    返回清除的组件数（node + bar）。
    """
    removed_ids = [
        cid for cid, comp in model.components.items()
        if comp.kind in ("tower_node", "tower_bar")
    ]
    removed = set(removed_ids)

    for cid in removed_ids:
        del model.components[cid]
        model.staleness.pop(cid, None)

    # connections：from/to 任一落在被删组件上，一并删除
    if model.connections:
        drop_conns = [
            cid for cid, conn in model.connections.items()
            if conn.from_component in removed or conn.to_component in removed
        ]
        for cid in drop_conns:
            del model.connections[cid]
            model.staleness.pop(cid, None)

    # rules：只删「applies_to 非空且指向被删组件」的规则。
    # 空 applies_to 表示全局规则，不因删除几何而移除（避免范围过大误删）。
    if model.rules:
        drop_rules = [
            rid for rid, rule in model.rules.items()
            if rule.applies_to and any(a in removed for a in rule.applies_to)
        ]
        for rid in drop_rules:
            del model.rules[rid]
            model.staleness.pop(rid, None)

    # dimensions：applies_to 指向被删杆件/节点的尺寸（如某杆件的实测长度）
    # 一并删除，否则留下悬空引用。
    if model.dimensions:
        drop_dims = [
            did for did, dim in model.dimensions.items()
            if dim.applies_to in removed
        ]
        for did in drop_dims:
            del model.dimensions[did]
            model.staleness.pop(did, None)

    # dependencies：upstream/downstream 指向被删组件
    if model.dependencies:
        for dep_id in list(model.dependencies):
            if dep_id in removed:
                del model.dependencies[dep_id]
                continue
            upstream = {u for u in model.dependencies[dep_id] if u not in removed}
            if upstream:
                model.dependencies[dep_id] = upstream
            else:
                del model.dependencies[dep_id]

    return len(removed_ids)


def _merge_label_lists(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并多源件号，按 (bar_id, round x, round y) 去重。"""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for group in groups:
        for lab in group:
            key = (
                str(lab.get("bar_id") or ""),
                round(float(lab.get("x_px", 0)), 0),
                round(float(lab.get("y_px", 0)), 0),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(lab)
    return out


def run_hybrid_dxf_agent_pipeline(
    dxf_path: str | Path,
    out_dir: str | Path,
    *,
    layer_map_path: Optional[str | Path] = None,
    mllm: Optional[MLLMBackend] = None,
    dpi: int = DEFAULT_PREVIEW_DPI,
    label_snap_mm: float = LABEL_SNAP_MM,
    min_association_rate: float = MIN_ASSOCIATION_RATE,
    use_ocr_fallback: bool = True,
    finalize_merge: bool = False,
    bom_path: Optional[str | Path] = None,
    geom_method: str = "auto",
    skip_mllm: bool = False,
) -> Dict[str, Any]:
    """DXF hybrid：A2 矢量 + A1 多模态件号 + A3 关联 + A4 Harness。

    返回与 ``run_tower_agent_pipeline`` 兼容的 dict（含 steps_path）。
    """
    from ..io import save_model, validate_references
    from ..intake.tower_pipeline import finalize_tower_model
    from ..harness.tower_validators import inject_tower_rules
    from .tower_spec import canonical_view_type, is_ortho_view_type

    dxf_path = Path(dxf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = dxf_path.stem

    graph = ProcessingGraph(name=f"hybrid-dxf-{stem}")
    model_path = out_dir / "model.json"
    steps_path = out_dir / "steps.json"
    summary_path = out_dir / "harness_summary.json"
    png_path = out_dir / f"{stem}_preview.png"
    crops_dir = out_dir / "agent_crops"
    mapping_path = out_dir / "render_mapping.json"

    mllm_backend = mllm or MLLMBackend()
    mllm_labels: List[Dict[str, Any]] = []
    labels: List[Dict[str, Any]] = []
    bars: List[Dict[str, Any]] = []
    link_meta: Dict[str, Any] = {}
    model: Optional[EngineeringModel] = None
    mapping: Dict[str, Any] = {}
    views: List[Dict[str, Any]] = []
    a2_method = "ezdxf"

    kind = resolve_drawing_kind(stem, overlay=layer_map_path)
    parse_bars = bool(kind.get("parse_bars", True))

    # ---------------- A0 版面（整页预览） ----------------
    graph.start(STAGE_LAYOUT, "版面分析（A0·DXF hybrid）", input=str(dxf_path))
    try:
        mapping = render_dxf_preview_with_mapping(dxf_path, png_path, dpi=dpi)
        mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        views = _layout_views_for_overlay(stem, mapping, layer_map_path, str(png_path))
        graph.finish(
            views=len(views),
            preview_png=str(png_path),
            drawing_kind=kind.get("kind"),
            parse_bars=parse_bars,
            mllm_provider=mllm_backend.provider,
            mllm_model=mllm_backend.model,
        )
    except Exception as exc:
        graph.fail(str(exc))
        views = []

    # ---------------- A2 几何（MLLM 优先 → ezdxf → 霍夫） ----------------
    mllm_geom_meta: Dict[str, Any] = {}
    if not parse_bars:
        graph.skip(STAGE_GEOMETRY, "几何检测（A2）", "parse_bars=False")
    else:
        graph.start(STAGE_GEOMETRY, "几何检测（A2）", input=str(dxf_path))
        try:
            model = extract_tower_from_dxf(str(dxf_path), layer_map_path=layer_map_path)
            ezdxf_bars, _ = _dxf_model_to_agent_bars(model)
            bar_count = sum(1 for c in model.components.values() if c.kind == "tower_bar")
            node_count = sum(1 for c in model.components.values() if c.kind == "tower_node")
            # view_type 优先取 overlay view_regions 的正交视图 kind（04-07 的
            # region.kind 是 "front"，而非文件名规则判出的 node_detail）；文件名
            # 规则只作兜底。这是 P0 根因：文件名 -0[3-9] 规则把 04-07 打成
            # node_detail，导致 MLLM 杆件 view_type 被压成 detail 进不了 M3。
            overlay_view_types = [
                v.get("view_type") for v in views
                if v.get("view_type") and is_ortho_view_type(v.get("view_type"))
            ]
            view_type = overlay_view_types[0] if overlay_view_types else (
                canonical_view_type(str(kind.get("kind") or "")) or "detail")
            if not is_ortho_view_type(view_type) and view_type != "detail":
                view_type = "detail"

            mllm_bars_px: List[Dict[str, Any]] = []
            if (not skip_mllm and geom_method in ("auto", "mllm")
                    and mllm_backend.available() and mapping and views):
                mllm_bars_px, _, mllm_geom_meta = _mllm_detect_geometry(
                    str(png_path), views, crops_dir, mllm_backend,
                )

            if mllm_bars_px and geom_method in ("auto", "mllm"):
                bars = _bars_px_to_drawing(mllm_bars_px, mapping, view_type)
                # 斜材共线拼接：把 MLLM 碎片化的通长斜材拼回整根（降低 FP、提升斜材长度）
                bars, stitched = _stitch_mllm_diagonals(bars)
                if stitched:
                    mllm_geom_meta["stitched_fragments"] = stitched
                a2_method = "mllm_geom"
                # MLLM 有杆时始终注入/替换：ezdxf 对 04-07 这类双线角钢图
                # 常产出 layer-0 垃圾几何，MLLM 优先于 ezdxf（而非仅 0 杆时注入）。
                stripped = _strip_vector_geometry(model)
                injected = _inject_mllm_bars_into_model(
                    model, bars, view_type=view_type,
                    stem=stem, layer_map_path=layer_map_path,
                )
                bars, _ = _dxf_model_to_agent_bars(model)
                mllm_geom_meta["injected_bars"] = injected
                mllm_geom_meta["stripped_vector_components"] = stripped
                mllm_geom_meta["ezdxf_bars"] = bar_count
                graph.finish(
                    bars=len(bars), nodes=node_count,
                    **{k: v for k, v in mllm_geom_meta.items()
                       if k not in ("method", "bars", "nodes", "ezdxf_bars")},
                    method=a2_method,
                )
            elif geom_method == "ezdxf" and ezdxf_bars:
                # 显式 ezdxf：只在这种模式下才用 ezdxf 几何（默认 auto 不优先 ezdxf，
                # 因为 04-07 双线角钢图常产 layer-0 垃圾碎杆）。
                bars = ezdxf_bars
                graph.finish(bars=bar_count, nodes=node_count, method="ezdxf",
                             association_ready=len(bars))
            elif mapping and geom_method != "ezdxf":
                # MLLM 空/失败时优先 hough（干净的栅格回退），而非 ezdxf 垃圾。
                hough_bars, hough_meta = _hough_bars_to_drawing(
                    str(png_path), mapping, view_type=view_type,
                )
                if hough_bars:
                    bars = hough_bars
                    a2_method = "hough_fallback"
                    stripped = _strip_vector_geometry(model)
                    injected = _inject_mllm_bars_into_model(
                        model, bars, view_type=view_type,
                        stem=stem, layer_map_path=layer_map_path,
                    )
                    bars, _ = _dxf_model_to_agent_bars(model)
                    hough_meta["injected_bars"] = injected
                    hough_meta["stripped_vector_components"] = stripped
                    hough_meta["mllm_degraded"] = bool(mllm_backend.available()) and not mllm_bars_px
                    graph.finish(
                        bars=len(bars), nodes=hough_meta.get("nodes_px", 0),
                        vector_bars=bar_count,
                        **{k: v for k, v in hough_meta.items()
                           if k not in ("nodes_px", "method", "bars", "nodes")},
                        method=a2_method,
                    )
                elif ezdxf_bars:
                    # 最后兜底：hough 也空但 ezdxf 有杆，标记 degraded 使用。
                    bars = ezdxf_bars
                    graph.finish(bars=bar_count, nodes=node_count, method="ezdxf",
                                 association_ready=len(bars), degraded=True,
                                 note="MLLM/hough 均无结果，回退 ezdxf")
                else:
                    graph.fail("MLLM/ezdxf/霍夫均无杆件", bars=bar_count, nodes=node_count,
                               **{k: v for k, v in mllm_geom_meta.items()
                                  if k not in ("method", "bars", "nodes")})
            else:
                graph.fail("几何检测 0 杆", bars=bar_count, nodes=node_count,
                           **{k: v for k, v in mllm_geom_meta.items()
                              if k not in ("method", "bars", "nodes")})
        except Exception as exc:
            graph.fail(str(exc))

    dxf_text_labels: List[Dict[str, Any]] = []
    if parse_bars:
        try:
            dxf_text_labels = _extract_dxf_text_labels(dxf_path, layer_map_path)
        except Exception:
            dxf_text_labels = []

    # ---------------- A1 多模态件号（可插拔 MLLM） ----------------
    if not parse_bars:
        graph.skip(STAGE_LABELS, "件号 OCR（A1·MLLM）", "parse_bars=False")
    elif skip_mllm:
        # 降级模式（detail 详图等非空间段）：只保留 DXF TEXT 件号，不调用 MLLM OCR。
        labels = list(dxf_text_labels)
        mllm_labels = []
        if labels:
            graph.start(STAGE_LABELS, "件号（DXF TEXT 降级）", input=str(dxf_path))
            graph.finish(labels=len(labels), mllm_labels=0,
                         dxf_text_labels=len(dxf_text_labels), method="dxf_text_only",
                         note="skip_mllm：仅 DXF TEXT 件号，不调用 MLLM")
        else:
            graph.skip(STAGE_LABELS, "件号 OCR（A1·MLLM）", "skip_mllm 且无 DXF TEXT")
    elif not mllm_backend.available():
        if use_ocr_fallback:
            ocr_labels: List[Dict[str, Any]] = []
            try:
                ocr_labels = _ocr_labels_from_tesseract(str(png_path), views)
                for lab in ocr_labels:
                    px, py = float(lab["x_px"]), float(lab["y_px"])
                    dx, dy = px_to_drawing_xy(px, py, mapping)
                    lab["x_px"], lab["y_px"] = round(dx, 2), round(dy, 2)
                    lab["coord_space"] = "drawing_mm"
            except Exception:
                ocr_labels = []
            labels = _merge_label_lists(dxf_text_labels, ocr_labels)
            mllm_labels = list(ocr_labels)
            if labels:
                graph.start(STAGE_LABELS, "件号 OCR（A1·Tesseract 兜底）", input=str(png_path))
                graph.finish(
                    labels=len(labels), mllm_labels=len(mllm_labels),
                    dxf_text_labels=len(dxf_text_labels), method="tesseract",
                    note="无 MLLM API，Tesseract 兜底",
                )
            else:
                graph.skip(STAGE_LABELS, "件号 OCR（A1·MLLM）",
                           "无 MLLM API 且 Tesseract 无结果")
        else:
            graph.skip(STAGE_LABELS, "件号 OCR（A1·MLLM）", "无 MLLM API")
    else:
        graph.start(STAGE_LABELS, "件号 OCR（A1·MLLM）", input=str(png_path))
        mllm_labels, a1_meta = _mllm_labels_from_png(
            str(png_path), views, crops_dir, mllm_backend, mapping,
        )
        labels = _merge_label_lists(dxf_text_labels, mllm_labels)
        if labels:
            graph.finish(
                labels=len(labels),
                mllm_labels=len(mllm_labels),
                dxf_text_labels=len(dxf_text_labels),
                **{k: v for k, v in a1_meta.items() if k != "warnings"},
                warnings=a1_meta.get("warnings", [])[:20],
            )
        elif a1_meta.get("failed_calls", 0) == 0:
            graph.finish(
                labels=0, mllm_labels=0, dxf_text_labels=len(dxf_text_labels),
                note="无文字", warnings=a1_meta.get("warnings", [])[:20],
            )
        else:
            graph.pending(
                "MLLM 件号 0 字或调用失败，待复核",
                labels=len(labels), mllm_labels=0, dxf_text_labels=len(dxf_text_labels),
                warnings=a1_meta.get("warnings", [])[:20],
            )

        if use_ocr_fallback and not mllm_labels:
            try:
                ocr_labels = _ocr_labels_from_tesseract(str(png_path), views)
                for lab in ocr_labels:
                    px, py = float(lab["x_px"]), float(lab["y_px"])
                    dx, dy = px_to_drawing_xy(px, py, mapping)
                    lab["x_px"], lab["y_px"] = round(dx, 2), round(dy, 2)
                    lab["coord_space"] = "drawing_mm"
                if ocr_labels:
                    mllm_labels = ocr_labels
                    labels = _merge_label_lists(dxf_text_labels, mllm_labels)
                    graph.start(STAGE_LABELS_OCR_FALLBACK, "件号 OCR 兜底（Tesseract）",
                                input=str(png_path))
                    graph.finish(labels=len(labels), mllm_labels=len(mllm_labels),
                                 method="tesseract")
            except Exception as exc:
                # P4：禁止静默吞异常——OCR 兜底失败记录到 graph，不影响主 A1 结果。
                graph.start(STAGE_LABELS_OCR_FALLBACK, "件号 OCR 兜底（Tesseract）",
                            input=str(png_path))
                graph.skip(STAGE_LABELS_OCR_FALLBACK, "件号 OCR 兜底（Tesseract）",
                           f"Tesseract 兜底失败：{exc}")

    if parse_bars and dxf_text_labels:
        labels = _merge_label_lists(dxf_text_labels, labels)

    # ---------------- A3 关联（图纸坐标 mm） ----------------
    graph.start(STAGE_LINK, "关联匹配（A3）", input=f"labels={len(labels)}, bars={len(bars)}")
    try:
        if not parse_bars:
            graph.finish(note="parse_bars=False")
            link_meta = {"assignments": [], "association_rate": 0.0, "label_hit_rate": 0.0}
        else:
            vector_n = _vector_labeled_count(model, bars) if model and a2_method == "ezdxf" else 0
            vector_rate = vector_n / len(bars) if bars else 0.0
            link_meta = _associate_labels(bars, labels, snap_distance=label_snap_mm, coord_space="mm")
            rate = link_meta["association_rate"]
            hit = link_meta["label_hit_rate"]
            noisy = link_meta["ocr_labels"] > 0 and len(bars) > link_meta["ocr_labels"] * 3
            passed = vector_rate >= min_association_rate or \
                     ((not noisy and rate >= min_association_rate) or
                      (noisy and hit >= min_association_rate))
            detail = {k: v for k, v in link_meta.items() if k != "assignments"}
            detail["vector_labeled"] = vector_n
            detail["vector_label_rate"] = round(vector_rate, 4)
            if passed:
                note = "矢量已贴号达标" if vector_rate >= min_association_rate else None
                graph.finish(**detail, coord_space="drawing_mm", note=note)
            else:
                gate = "label_hit_rate" if noisy else "association_rate"
                val = hit if noisy else rate
                graph.pending(f"{gate}={val} < {min_association_rate}", **detail)
    except Exception as exc:
        graph.fail(str(exc))

    # ---------------- A4 编译 + Harness ----------------
    graph.start(STAGE_HARNESS, "编译验证（A4·DXF hybrid）", input=str(dxf_path))
    harness_summary: Dict[str, Any] = {}
    try:
        if model is None:
            raise RuntimeError("A2 矢量模型缺失")
        apply_stats = _apply_assignments_to_dxf_model(
            model, bars, link_meta.get("assignments") or [],
        )
        df = model.components.get("drawing_file")
        if df is not None:
            df.properties["compile_mode"] = "hybrid_dxf_agent"
            df.properties["mllm_provider"] = mllm_backend.provider
            df.properties["mllm_model"] = mllm_backend.model
            df.properties["label_origin_hybrid"] = "mllm_a3"
        model = finalize_tower_model(
            model, bom_path=bom_path, merge=finalize_merge,
            layer_map_path=layer_map_path,
        )
        inject_tower_rules(model)
        problems = validate_references(model)
        results = run_harness(model)
        harness_summary = {"summary": summarize(results)}
        status_counts: Dict[str, int] = {}
        for r in results:
            status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1
        failed_rules = [r.target_id for r in results if r.status == ValidationStatus.FAILED]

        if problems:
            graph.fail(f"引用完整性 {len(problems)} 项", problems=problems[:10])
        elif failed_rules:
            graph.pending("Harness 有 failed 规则", failed_rules=failed_rules[:10],
                          summary=status_counts)
        else:
            graph.finish(summary=status_counts, **apply_stats)

        save_model(model, model_path)
        labeled_vector = sum(
            1 for c in model.components.values()
            if c.kind == "tower_bar"
            and c.properties.get("bar_id")
            and not str(c.properties["bar_id"]).startswith("UNLABELED")
        )
        # P0/P1：缓存指纹。统一走 build_pipeline_fingerprint，保证与批跑
        # --skip-existing 的读取键完全一致（含 prompt_sha / pipeline_version）。
        from .mllm_tower_prompt import GEOM_AGENT_PROMPT, LABEL_AGENT_PROMPT
        fingerprint = build_pipeline_fingerprint(
            provider=mllm_backend.provider,
            model=mllm_backend.model,
            dpi=dpi,
            geom_method=geom_method,
            layer_map_path=layer_map_path,
            prompts=GEOM_AGENT_PROMPT + "\n" + LABEL_AGENT_PROMPT,
        )
        summary_path.write_text(json.dumps({
            "mode": "hybrid_dxf_agent",
            "dxf": str(dxf_path),
            "mllm_provider": mllm_backend.provider,
            "mllm_model": mllm_backend.model,
            "dpi": dpi,
            "geom_method": geom_method,
            "fingerprint": fingerprint,
            "mllm_labels": len(mllm_labels),
            "dxf_text_labels": len(dxf_text_labels),
            "total_labels": len(labels),
            "a2_method": a2_method,
            "vector_labeled_bars": labeled_vector,
            "bars": len(bars),
            "association_rate": link_meta.get("association_rate"),
            "label_hit_rate": link_meta.get("label_hit_rate"),
            "apply": apply_stats,
            "harness": harness_summary,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        graph.fail(str(exc))

    graph.export_json(steps_path)
    # P1 修复：ok 判定收紧。之前只排除 failed，导致 Harness 有 failed 规则
    # / association_rate 不达标 / MLLM 件号 0 字等 pending 状态也被算作成功，
    # 批跑/CI 会把不合格单页当通过。现在 failed 与 pending 均视为未达标。
    ok = all(s.status not in ("failed", "pending") for s in graph.steps)
    return {
        "ok": ok,
        "graph": graph,
        "model_path": model_path.as_posix() if model_path.exists() else None,
        "steps_path": steps_path.as_posix(),
        "summary_path": summary_path.as_posix() if summary_path.exists() else None,
        "preview_png": png_path.as_posix() if png_path.exists() else None,
        "mapping_path": mapping_path.as_posix() if mapping_path.exists() else None,
        "glb_path": None,
        "mllm_provider": mllm_backend.provider,
        "mllm_model": mllm_backend.model,
    }
