"""铁塔扫描图多 Agent 编排（P1）。

无 DXF 主路径（PNG/PDF 扫描图）不再用单轮 MLLM 识别整塔，而是拆成五步：
    A0 版面分析（规则）  -> drawing_view + bbox
    A1 件号 OCR（VLM）   -> labels 数组（无 API 跳过，A3 全 UNLABELED）
    A2 几何检测（霍夫为主）-> bars / nodes（无 VLM 也能跑）
    A3 关联匹配（确定性规则）-> bar -> 最近合法件号，一对一贪心
    A4 编译验证（contract + Harness）

每一步都有 Harness 闸门（passed / pending / failed），写入 steps.json 可审计。
A3 必须是确定性规则，不把「对一下」再扔给模型。
扫描图默认 solve_status=pending_review，无坐标不 export strict GLB。
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..harness.harness import run_harness, summarize
from ..harness.processing_graph import ProcessingGraph
from ..model import ValidationStatus
from .mllm_backend import CandidateObject, DrawingInput, MLLMBackend, ModelCandidate
from .mllm_tower_prompt import (
    LABEL_AGENT_PROMPT,
    LABEL_AGENT_SCHEMA,
    GEOM_AGENT_PROMPT,
    GEOM_AGENT_SCHEMA,
    parse_label_agent_output,
    parse_geom_agent_output,
)
from .tower_dxf import _compile_bar_id_re, _extract_bar_label
from .pipeline_stages import (
    STAGE_LAYOUT,
    STAGE_LABELS,
    STAGE_LABELS_OCR_FALLBACK,
    STAGE_GEOMETRY,
    STAGE_LINK,
    STAGE_HARNESS,
)
from .tower_layout import (
    INK_THRESHOLD,
    MIN_BAR_PX,
    _cluster_endpoints,
    _detect_line_segments,
    _detect_regions,
    _load_image,
    _merge_collinear,
    filter_noise_segments,
    filter_frame_and_edge_segments,
    layout_views_from_regions,
)

# A3 件号文字中心到杆件中点的最大像素距离（与 DXF TEXT_SNAP 对应，扫描图用 px）
LABEL_SNAP_PX = 400.0
# A3 关联率闸门：低于阈值 -> pending（不 failed 凑数）
MIN_ASSOCIATION_RATE = 0.20
# 每个 view 裁剪图最长边（与 MLLM_MAX_IMAGE_EDGE 对齐）。Kimi 视觉推荐
# 单图上限 4096×2160；2048 会让塔身段（6600mm 高）细斜材糊掉，实测召回不足。
MAX_VIEW_EDGE_PX = int(os.environ.get("MLLM_MAX_IMAGE_EDGE") or "4096")


def _rasterize_if_pdf(source: str | Path, out_dir: Path) -> str:
    """PDF 先栅格化为 PNG，A0 后续统一按位图处理。"""
    source = str(source)
    if Path(source).suffix.lower() != ".pdf":
        return source
    from .pdf_raster import rasterize_pdf_to_png
    raster = rasterize_pdf_to_png(source)
    # 放进 out_dir，保证交付目录自包含
    target = out_dir / "agent_source.png"
    target.write_bytes(Path(raster).read_bytes())
    return str(target)


def _crop_view(image_path: str, bbox: List[int], out_dir: Path, view_id: str) -> Dict[str, Any]:
    """裁出一个视图；长边 ≤ MAX_VIEW_EDGE_PX，记录 scale_mm_per_px（无则 placeholder）。"""
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    x0, y0, x1, y1 = [int(v) for v in bbox]
    img = Image.open(image_path)
    if x0 < 0 or y0 < 0 or x1 > img.width or y1 > img.height:
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(img.width, x1), min(img.height, y1)
    if x1 <= x0 or y1 <= y0:
        # 空裁剪回退整图，避免 A1 无输入
        x0, y0, x1, y1 = 0, 0, img.width, img.height
    crop = img.crop((x0, y0, x1, y1))
    source_w, source_h = crop.width, crop.height
    longest = max(source_w, source_h)
    ratio = 1.0
    if longest > MAX_VIEW_EDGE_PX:
        ratio = MAX_VIEW_EDGE_PX / longest
        crop = crop.resize((max(1, round(source_w * ratio)), max(1, round(source_h * ratio))))
    out_path = out_dir / f"{view_id}.png"
    crop.convert("RGB").save(out_path, format="PNG")
    return {
        "path": str(out_path),
        "bbox": [x0, y0, x1, y1],
        "crop_size": [crop.width, crop.height],
        "source_crop_size": [source_w, source_h],
        "scale_mm_per_px": None,  # 未人工标定 -> placeholder
        "scale_origin": "placeholder",
    }


def _assign_view_by_bbox(
    bars: List[Dict[str, Any]],
    nodes: List[Dict[str, Any]],
    views: List[Dict[str, Any]],
    default_view_type: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """P1-6：A2 后按 A0 视图 bbox 给杆/节点打 view_type。

    单图多 region 时，A2 的霍夫检测跑在整图上，产出无视图归属；
    这里按杆件中点落在哪个 view 的 bbox 内来归属（节点同理）。
    落在所有 bbox 之外（或只有 whole_sheet 单视图）时回退到文件名级
    default_view_type，保持单视图旧行为。

    归属后的 view_type 让 A3 的「label 与 bar 同 view」过滤真正生效，
    避免跨视图（如 front 文字贴到 side 杆件）误配。
    """
    def _mid(obj: Dict[str, Any]) -> Tuple[float, float]:
        if "x1" in obj:
            return ((float(obj["x1"]) + float(obj["x2"])) / 2.0,
                    (float(obj["y1"]) + float(obj["y2"])) / 2.0)
        return (float(obj["x_px"]), float(obj["y_px"]))

    # 只取有真实语义 bbox 的视图；whole_sheet 表示整图无有效切块，不参与归属
    bbox_views = [v for v in views
                  if v.get("bbox") and v.get("view_id") != "whole_sheet"]

    def _assign(obj: Dict[str, Any]) -> str:
        mx, my = _mid(obj)
        for v in bbox_views:
            x0, y0, x1, y1 = [int(c) for c in v["bbox"]]
            if x0 <= mx <= x1 and y0 <= my <= y1:
                return v.get("view_type") or default_view_type
        return default_view_type

    new_bars = [dict(b, view_type=_assign(b)) for b in bars]
    new_nodes = [dict(n, view_type=_assign(n)) for n in nodes]
    return new_bars, new_nodes


def _detect_geometry(
    image_path: str,
    filter_noise: bool = True,
    use_preprocess: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """A2 规则几何检测（霍夫线 + 共线合并 + 端点聚类 + 噪声过滤）。

    use_preprocess=True 时先走 Phase C 线重绘栅格预处理，提升弱中心线召回。
    返回 (bars, nodes, meta)。bars 元素：bar_uid + x1/y1/x2/y2；
    nodes 元素：node_id + x_px/y_px。meta 记录 raw/merged/kept 数量。
    """
    cv2, gray = _load_image(image_path)
    preprocess_meta: Dict[str, Any] = {}
    if use_preprocess:
        from .tower_preprocess import preprocess_for_scan
        import numpy as np
        gray, preprocess_meta = preprocess_for_scan(gray, cv2=cv2, np=np)
    raw = _detect_line_segments(cv2, gray)
    merged = [
        seg for seg in _merge_collinear(raw)
        if math.hypot(seg[2] - seg[0], seg[3] - seg[1]) >= MIN_BAR_PX
    ]
    removed: List[Dict] = []
    # B1：先过滤图框长线 / 贴边线段（几何判定，不接节点 degree），再过滤孤立短噪声。
    h, w = gray.shape[:2]
    merged, frame_removed = filter_frame_and_edge_segments(merged, w, h)
    removed.extend(frame_removed)
    if filter_noise:
        merged, noise_removed = filter_noise_segments(merged)
        removed.extend(noise_removed)

    bars: List[Dict[str, Any]] = []
    for i, seg in enumerate(merged, start=1):
        x1, y1, x2, y2 = seg
        bars.append({
            "bar_uid": f"bar_{i:04d}",
            "x1": round(float(x1), 2),
            "y1": round(float(y1), 2),
            "x2": round(float(x2), 2),
            "y2": round(float(y2), 2),
        })
    nodes: List[Dict[str, Any]] = []
    for i, node in enumerate(_cluster_endpoints(merged), start=1):
        nodes.append({
            "node_id": f"N{i:03d}",
            "x_px": round(float(node["x"]), 2),
            "y_px": round(float(node["y"]), 2),
        })
    meta = {
        "method": "hough",
        "raw_segments": len(raw),
        "merged_segments": len(merged),
        "noise_removed": len(removed),
    }
    if preprocess_meta:
        meta["preprocess"] = preprocess_meta
        meta["preprocessed"] = True
    return bars, nodes, meta


def _label_point(label: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    # P3-8 单位规范：label 坐标可能是像素（x_px/y_px）或图纸 mm（drawing_x/drawing_y）。
    # 优先读 drawing_x/drawing_y（coord_space="drawing_mm"），回退 x_px/y_px。
    try:
        if "drawing_x" in label or "drawing_y" in label:
            return (float(label.get("drawing_x")), float(label.get("drawing_y")))
        return (float(label["x_px"]), float(label["y_px"]))
    except (KeyError, TypeError, ValueError):
        return None


def _labels_to_full_image(
    view_labels: List[Dict[str, Any]],
    crop: Dict[str, Any],
    view_id: str,
) -> List[Dict[str, Any]]:
    """VLM 在缩放后 crop 上的坐标还原到整图 pixel 坐标。

    坐标方向：
        * VLM 看到的图是 ``crop_size``（_crop_view 已按最长边缩放过）
        * 裁图但未缩放时的原始尺寸是 ``source_crop_size``
        * 因此从 VLM 坐标放大回原始 crop 坐标的倍率必须是
          ``source_crop_size / crop_size``（> 1，而不是取反的 < 1）
        * 最后加上 crop 在整图中的左上角偏移 ``(x0, y0)``
    """
    x0, y0 = float(crop["bbox"][0]), float(crop["bbox"][1])

    # VLM 实际看到的图（缩放后）
    scaled_w, scaled_h = crop.get("crop_size") or (None, None)
    # 裁图后、缩放前的原始像素尺寸
    source_w, source_h = crop.get("source_crop_size") or (None, None)

    # 回退：拿不到 source 尺寸时，用 bbox 宽高作为原始裁图尺寸
    if source_w is None or source_h is None or source_w <= 0 or source_h <= 0:
        source_w = float(crop["bbox"][2]) - x0
        source_h = float(crop["bbox"][3]) - y0
    if scaled_w is None or scaled_h is None or scaled_w <= 0 or scaled_h <= 0:
        scaled_w, scaled_h = float(source_w), float(source_h)

    # VLM 坐标 -> 原始 crop 坐标的放大倍率（缩放图更小，因此 sx/sy >= 1）
    sx = float(source_w) / float(scaled_w)
    sy = float(source_h) / float(scaled_h)

    out: List[Dict[str, Any]] = []
    for lab in view_labels:
        lab = dict(lab)
        lab["x_px"] = round(x0 + float(lab["x_px"]) * sx, 2)
        lab["y_px"] = round(y0 + float(lab["y_px"]) * sy, 2)
        lab["view"] = lab.get("view") or view_id
        out.append(lab)
    return out


def _subdivide_view_bbox(
    view: Dict[str, Any],
    png_path: str,
    *,
    max_sub_height_px: int = 1400,
    overlap_ratio: float = 0.12,
) -> List[List[int]]:
    """把视图 bbox 沿高度(Z)切成多个带重叠的子 bbox，降低单区斜腹杆密度。

    只对 front/elevation/side 等正交视图切分（detail 不切）；高度不超过
    max_sub_height_px 时不切。子区域带 overlap_ratio 重叠，便于跨子区边界的
    斜腹杆在 stitch 阶段拼回通长。

    返回子 bbox 列表；不切时返回 [原 bbox]。
    """
    bbox = view.get("bbox") or []
    if len(bbox) < 4:
        return [list(bbox)] if bbox else [[]]
    # 只在 overlay 显式标记 subdivide=True 的段切分（白名单控制），
    # 避免对单区已够好的段（02/07）过度检测
    if not view.get("subdivide"):
        return [list(bbox)]
    vt = view.get("view_type") or ""
    if vt not in ("front", "elevation", "side"):
        return [list(bbox)]

    x0, y0, x1, y1 = [int(v) for v in bbox[:4]]
    height = y1 - y0
    if height <= max_sub_height_px:
        return [list(bbox)]

    # 计算子区域数量（向上取整）
    n = max(2, -(-height // max_sub_height_px))
    overlap = int(height * overlap_ratio / max(1, n - 1))
    sub_bboxes: List[List[int]] = []
    step = (height - overlap) / n
    for i in range(n):
        sy0 = int(y0 + round(i * step))
        sy1 = y1 if i == n - 1 else int(min(y1, sy0 + max_sub_height_px))
        sub_bboxes.append([x0, sy0, x1, sy1])
    return sub_bboxes


def _crop_scale_factors(crop: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """VLM crop 坐标 → 整图像素坐标的 (x0, y0, sx, sy)。"""
    x0, y0 = float(crop["bbox"][0]), float(crop["bbox"][1])
    scaled_w, scaled_h = crop.get("crop_size") or (None, None)
    source_w, source_h = crop.get("source_crop_size") or (None, None)
    if source_w is None or source_h is None or source_w <= 0 or source_h <= 0:
        source_w = float(crop["bbox"][2]) - x0
        source_h = float(crop["bbox"][3]) - y0
    if scaled_w is None or scaled_h is None or scaled_w <= 0 or scaled_h <= 0:
        scaled_w, scaled_h = float(source_w), float(source_h)
    sx = float(source_w) / float(scaled_w)
    sy = float(source_h) / float(scaled_h)
    return x0, y0, sx, sy


def _geom_to_full_image(
    bars: List[Dict[str, Any]],
    nodes: List[Dict[str, Any]],
    crop: Dict[str, Any],
    view_id: str,
    view_type: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """A2 MLLM 在 crop 上的几何坐标还原到整图像素。"""
    x0, y0, sx, sy = _crop_scale_factors(crop)
    vt = view_type or view_id
    out_bars: List[Dict[str, Any]] = []
    for bar in bars:
        out_bars.append({
            **bar,
            "x1": round(x0 + float(bar["x1"]) * sx, 2),
            "y1": round(y0 + float(bar["y1"]) * sy, 2),
            "x2": round(x0 + float(bar["x2"]) * sx, 2),
            "y2": round(y0 + float(bar["y2"]) * sy, 2),
            "view_type": bar.get("view_type") or vt,
        })
    out_nodes: List[Dict[str, Any]] = []
    for node in nodes:
        out_nodes.append({
            **node,
            "x_px": round(x0 + float(node["x_px"]) * sx, 2),
            "y_px": round(y0 + float(node["y_px"]) * sy, 2),
            "view_type": node.get("view_type") or vt,
        })
    return out_bars, out_nodes


def _deduplicate_overlapping_bars(
    bars: List[Dict[str, Any]],
    *,
    endpoint_tol_px: float = 6.0,
    angle_tol_deg: float = 8.0,
    overlap_ratio_threshold: float = 0.5,
) -> Tuple[List[Dict[str, Any]], List[List[str]]]:
    """P3-7：MLLM 重叠 crop 杆件去重（几何级，禁止只按 node ID）。

    相邻子区域带 overlap，跨边界杆件会被重复检测。去重条件（全部满足才判重）：
        * 同一 view_type
        * 两端点允许正反顺序（(a,b) 与 (b,a) 视为同杆）
        * 端点距离在 endpoint_tol_px 内
        * 方向夹角在 angle_tol_deg 内（共线）
        * 共线重叠达到 overlap_ratio_threshold（去重短碎片）

    返回 (deduplicated_bars, duplicate_groups)。duplicate_groups 每组是
    被判重的 bar_uid 列表（首个保留）。
    """
    import math

    def _endpoints(b: Dict[str, Any]) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        return ((float(b.get("x1", 0.0)), float(b.get("y1", 0.0))),
                (float(b.get("x2", 0.0)), float(b.get("y2", 0.0))))

    def _dist(p: Tuple[float, float], q: Tuple[float, float]) -> float:
        return math.hypot(p[0] - q[0], p[1] - q[1])

    def _dir(b: Dict[str, Any]) -> Tuple[float, float]:
        (x1, y1), (x2, y2) = _endpoints(b)
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        if norm < 1e-9:
            return (0.0, 0.0)
        return (dx / norm, dy / norm)

    def _angle_diff(d1: Tuple[float, float], d2: Tuple[float, float]) -> float:
        dot = max(-1.0, min(1.0, d1[0] * d2[0] + d1[1] * d2[1]))
        return math.degrees(math.acos(dot))

    def _overlap_ratio(
        p: Tuple[float, float], q: Tuple[float, float],
        r: Tuple[float, float], s: Tuple[float, float],
        d: Tuple[float, float],
    ) -> float:
        """两条共线线段在方向 d 上的投影重叠比例（相对较短者）。"""
        # 把四个端点投影到方向 d 上
        proj = lambda pt: pt[0] * d[0] + pt[1] * d[1]  # noqa: E731
        a1, a2 = sorted((proj(p), proj(q)))
        b1, b2 = sorted((proj(r), proj(s)))
        overlap = max(0.0, min(a2, b2) - max(a1, b1))
        shorter = min(a2 - a1, b2 - b1)
        if shorter < 1e-9:
            return 0.0
        return overlap / shorter

    n = len(bars)
    keep = [True] * n
    groups: List[List[str]] = []
    for i in range(n):
        if not keep[i]:
            continue
        bi = bars[i]
        (i1, i2) = _endpoints(bi)
        di = _dir(bi)
        vti = bi.get("view_type")
        group = [bi.get("bar_uid") or f"bar_{i}"]
        for j in range(i + 1, n):
            if not keep[j]:
                continue
            bj = bars[j]
            if bj.get("view_type") != vti:
                continue
            (j1, j2) = _endpoints(bj)
            # 端点正反顺序都试：同向 (i1~j1, i2~j2) 或反向 (i1~j2, i2~j1)
            same_dir = (_dist(i1, j1) <= endpoint_tol_px and _dist(i2, j2) <= endpoint_tol_px)
            rev_dir = (_dist(i1, j2) <= endpoint_tol_px and _dist(i2, j1) <= endpoint_tol_px)
            # 方向夹角（反向杆用反向单位向量比较）
            dj = _dir(bj)
            if rev_dir:
                dj = (-dj[0], -dj[1])
            if _angle_diff(di, dj) > angle_tol_deg:
                continue
            # 阶段 6.1/7.3：端点几乎重合（same_dir/rev_dir）时直接判重；
            # 端点不重合但共线重叠达阈值的短碎片也判重（去重短碎片）。
            if not (same_dir or rev_dir):
                ov = _overlap_ratio(i1, i2, j1, j2, di)
                if ov < overlap_ratio_threshold:
                    continue
            keep[j] = False
            group.append(bj.get("bar_uid") or f"bar_{j}")
        if len(group) > 1:
            groups.append(group)

    deduped = [b for b, k in zip(bars, keep) if k]
    return deduped, groups


def _mllm_detect_geometry(
    png_path: str,
    views: List[Dict[str, Any]],
    crops_dir: Path,
    mllm: MLLMBackend,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """A2 MLLM 几何检测：按 overlay/A0 视图裁剪后逐块调用 GEOM_AGENT。"""
    bars_all: List[Dict[str, Any]] = []
    nodes_all: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {
        "method": "mllm_geom",
        "provider": mllm.provider,
        "model": mllm.model,
        "failed_calls": 0,
        "warnings": [],
        "views_processed": 0,
    }
    bar_idx = 0
    # P0 修复：不同 crop/view 的 MLLM 各自返回 N001/N002...，仅按 node_id 去重
    # 会把后续 view 的同名节点误删。改为按 (view_id, node_id) 去重。
    seen_nodes: set = set()
    last_meta: Dict[str, Any] = {}

    for view in views:
        vt = view.get("view_type") or "drawing"
        # 子区域切分：对高斜腹杆密度的 front/elevation 视图，沿高度(Z)切成
        # 多个带重叠的子区域分别检测，降低单区斜腹杆密度，避免 MLLM 漏检
        # （06 段 112 根斜腹杆单次检测只出 43 根）。子区域坐标经
        # _geom_to_full_image 还原到整图后合并，重叠区不做去重（由 stitch 拼接）。
        sub_bboxes = _subdivide_view_bbox(view, png_path)
        for si, sub_bbox in enumerate(sub_bboxes):
            sub_view_id = f"{view['view_id']}" if len(sub_bboxes) == 1 else f"{view['view_id']}_s{si}"
            try:
                crop = _crop_view(png_path, sub_bbox, crops_dir, f"geom_{sub_view_id}")
                parsed, call_meta = mllm.call_agent_json(
                    GEOM_AGENT_PROMPT, crop["path"], GEOM_AGENT_SCHEMA, agent="a2_geom",
                )
                last_meta = call_meta
                if parsed is None:
                    meta["failed_calls"] += 1
                    meta["warnings"].append(
                        f"{sub_view_id}: {call_meta.get('failure_reason', 'MLLM 几何调用失败')}"
                    )
                    continue
                view_bars, view_nodes, problems, warnings = parse_geom_agent_output(parsed)
                meta["warnings"].extend(warnings)
                if problems:
                    meta["failed_calls"] += 1
                    meta["warnings"].extend(problems)
                    continue
                full_bars, full_nodes = _geom_to_full_image(
                    view_bars, view_nodes, crop, sub_view_id, view_type=vt,
                )
                for bar in full_bars:
                    bar_idx += 1
                    bars_all.append({**bar, "bar_uid": f"bar_{bar_idx:04d}", "geometry_origin": "mllm_geom"})
                for node in full_nodes:
                    nid = str(node.get("node_id") or "")
                    key = (sub_view_id, nid)
                    if key in seen_nodes:
                        continue
                    seen_nodes.add(key)
                    nodes_all.append(node)
                meta["views_processed"] += 1
            except Exception as exc:
                meta["failed_calls"] += 1
                meta["warnings"].append(f"{sub_view_id}: {exc}")
        if len(sub_bboxes) > 1:
            meta.setdefault("subdivided_views", 0)
            meta["subdivided_views"] += 1

    meta["mllm_duration_ms"] = last_meta.get("duration_ms")
    meta["mllm_elapsed_s"] = last_meta.get("elapsed_s")
    # P3-7：重叠 crop 杆件几何去重（非 node ID）。输出 raw/deduplicated/duplicate_groups。
    meta["raw_bars"] = len(bars_all)
    deduped_bars, dup_groups = _deduplicate_overlapping_bars(bars_all)
    bars_all = deduped_bars
    meta["deduplicated_bars"] = len(bars_all)
    meta["duplicate_groups"] = dup_groups
    meta["duplicate_group_count"] = len(dup_groups)
    meta["bars"] = len(bars_all)
    meta["nodes"] = len(nodes_all)
    return bars_all, nodes_all, meta


def _ocr_labels_from_tesseract(
    image_path: str,
    views: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """B4：A1 无 API / 0 字时的 Tesseract OCR 确定性兜底。

    用 pytesseract image_to_data 在整图上产文本框，把命中件号正则的文本
    转成 A3 期望的 label 格式（text / bar_id / x_px / y_px / view）。

    与 VLM 坐标不同：Tesseract 直接跑在整图上，bbox 即整图像素坐标，
    无需 _labels_to_full_image 的缩放/偏移还原。因此只取文本框中心作为
    (x_px, y_px)，并按中心落在哪个 view bbox 内打 view_type（供 A3 同
    view 过滤）。

    未安装 pytesseract / tesseract 时返回空列表（绝不猜编号）。
    """
    from .tower_layout import _ocr_boxes

    boxes = _ocr_boxes(image_path)
    if not boxes:
        return []

    bar_id_re = _compile_bar_id_re()

    # 有真实语义 bbox 的视图（whole_sheet 不参与归属）
    bbox_views = [v for v in views
                  if v.get("bbox") and v.get("view_id") != "whole_sheet"]

    def _view_of(x: float, y: float) -> Optional[str]:
        for v in bbox_views:
            x0, y0, x1, y1 = [int(c) for c in v["bbox"]]
            if x0 <= x <= x1 and y0 <= y <= y1:
                return v.get("view_type")
        return None

    labels: List[Dict[str, Any]] = []
    for box in boxes:
        text = (box.get("text") or "").strip()
        if not text:
            continue
        bar_id = _extract_bar_label(text, bar_id_re)
        if not bar_id:
            continue
        bbox = box.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        cx = (float(bbox[0]) + float(bbox[2])) / 2.0
        cy = (float(bbox[1]) + float(bbox[3])) / 2.0
        labels.append({
            "text": text,
            "bar_id": bar_id,
            "x_px": round(cx, 2),
            "y_px": round(cy, 2),
            "view": _view_of(cx, cy),
            "ocr_source": "tesseract",
        })
    return labels


def _associate_labels(
    bars: List[Dict[str, Any]],
    labels: List[Dict[str, Any]],
    snap_distance: float = LABEL_SNAP_PX,
    coord_space: str = "px",
    snap_px: Optional[float] = None,
) -> Dict[str, Any]:
    """A3 确定性关联：bar -> 同视图最近合法件号，一对一贪心。

    P3-8 单位规范：snap_distance 是吸附距离，coord_space ∈ {"px", "mm"} 表明
    其单位。输出距离字段名随 coord_space 变化：
        * coord_space="px" → label_distance_px
        * coord_space="mm" → label_distance_mm
    snap_px 为兼容旧调用的别名（等于 snap_distance + coord_space="px"）。

    与 DXF 逻辑同源：先生成 (距离, bar, label) 候选对，按距离升序贪心；
    每个文字只贴一根杆、每根杆只收一个文字。同一件号可出现在多个文字位置
    （重复件号组留给 A3 报告与 r_no_duplicate_bar_id）。
    """
    # 兼容旧参数名：snap_px 优先（旧调用），否则用 snap_distance + coord_space。
    if snap_px is not None:
        snap = snap_px
        dist_field = "label_distance_px"
    else:
        snap = snap_distance
        dist_field = "label_distance_mm" if coord_space == "mm" else "label_distance_px"
    bar_id_re = _compile_bar_id_re()

    def _view_of(obj: Dict[str, Any]) -> Optional[str]:
        """统一读取 view 字段：优先 view_type，其次 view。"""
        v = obj.get("view_type") or obj.get("view")
        return str(v) if v else None

    # 先筛出合法件号（材质/截面/螺栓排除在 _extract_bar_label 内）
    legal: List[Tuple[int, str, float, float, Optional[str]]] = []
    for li, label in enumerate(labels):
        pt = _label_point(label)
        if pt is None:
            continue
        bar_id = _extract_bar_label(str(label.get("text") or ""), bar_id_re)
        if not bar_id:
            continue
        legal.append((li, bar_id, pt[0], pt[1], _view_of(label)))

    pairs: List[Tuple[float, int, int, str]] = []
    for bi, bar in enumerate(bars):
        x1, y1 = float(bar["x1"]), float(bar["y1"])
        x2, y2 = float(bar["x2"]), float(bar["y2"])
        # 中点距离（参考 tower_dxf._point_mid_dist）：件号文字在杆件中点附近，
        # 而不是沿整条线段任意位置；点-线段距离会被交叉杆件抢走编号。
        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        bar_view = _view_of(bar)
        for (li, bar_id, lx, ly, label_view) in legal:
            # 同 view 内配对：bar 与 label 都带视图时，视图不一致直接跳过；
            # 任一侧缺 view（旧数据 / whole_sheet 单视图）保持全局配对兼容。
            if bar_view is not None and label_view is not None and bar_view != label_view:
                continue
            d = math.hypot(lx - mx, ly - my)
            if d < snap:
                pairs.append((d, bi, li, bar_id))
    pairs.sort(key=lambda x: x[0])

    bar_label: Dict[int, str] = {}
    bar_dist: Dict[int, float] = {}
    used_labels: set = set()
    for d, bi, li, bar_id in pairs:
        if bi in bar_label or li in used_labels:
            continue
        bar_label[bi] = bar_id
        bar_dist[bi] = d
        used_labels.add(li)

    assignments: List[Dict[str, Any]] = []
    for bi, bar in enumerate(bars):
        if bi in bar_label:
            assignments.append({
                "bar_uid": bar["bar_uid"],
                "bar_id": bar_label[bi],
                "confidence": 0.75,
                dist_field: round(bar_dist[bi], 2),
            })
        else:
            assignments.append({
                "bar_uid": bar["bar_uid"],
                "bar_id": f"UNLABELED_{bar['bar_uid']}",
                "confidence": 0.3,
                dist_field: None,
            })

    labeled = [a for a in assignments if not str(a["bar_id"]).startswith("UNLABELED")]
    rate = round(len(labeled) / len(assignments), 4) if assignments else 0.0

    # B2 双指标：
    #   * labeled/bars           = 已贴号杆件 / 全部候选杆（受霍夫噪声影响，基数虚高时偏低）
    #   * labeled_labels/ocr_labels = 命中杆件的件号 / 合法件号总数（OCR 视角的命中率，
    #     霍夫噪声多时更能反映「件号有没有成功贴对杆」）
    ocr_labels = len(legal)
    labeled_labels = len(used_labels)
    label_hit_rate = round(labeled_labels / ocr_labels, 4) if ocr_labels else 0.0

    # 重复件号组数（同一 bar_id 贴了多根杆）
    from collections import defaultdict
    by_id: Dict[str, List[str]] = defaultdict(list)
    for a in labeled:
        by_id[str(a["bar_id"])].append(a["bar_uid"])
    duplicate_groups = {k: v for k, v in by_id.items() if len(v) > 1}

    return {
        "assignments": assignments,
        "labeled": len(labeled),
        "bars": len(assignments),
        "association_rate": rate,
        # B2 双指标
        "labeled_labels": labeled_labels,
        "ocr_labels": ocr_labels,
        "label_hit_rate": label_hit_rate,
        "duplicate_bar_id_groups": len(duplicate_groups),
        "duplicate_bar_id_detail": [
            {"bar_id": k, "count": len(v), "bar_uids": v}
            for k, v in sorted(duplicate_groups.items())
        ][:200],
    }


def _build_model_candidate(
    source: str,
    views: List[Dict[str, Any]],
    bars: List[Dict[str, Any]],
    nodes: List[Dict[str, Any]],
    assignments: List[Dict[str, Any]],
    link_meta: Dict[str, Any],
) -> ModelCandidate:
    """把 A0/A2/A3 结果组装成 ModelCandidate，交 skill/contract.py 强制编译。"""
    objects: List[CandidateObject] = []
    # P0-2：主视图类型（单文件扫描图通常只有一个视图），杆件/节点继承它
    default_view_type = views[0].get("view_type") if views else "drawing"

    for view in views:
        objects.append(CandidateObject(
            obj_type="component",
            data={
                "id": view["view_id"],
                "kind": "drawing_view",
                "name": view.get("title") or f"视图 {view['view_id']}",
                "properties": {
                    "view_type": view.get("view_type") or "drawing",
                    "bbox": view.get("bbox"),
                    "scale_mm_per_px": view.get("scale_mm_per_px"),
                    "scale_origin": view.get("scale_origin", "placeholder"),
                    **({"z_level": view["z_level"]} if view.get("z_level") is not None else {}),
                },
            },
            source={"source_type": "drawing", "reference": source,
                    "detail": "A0 版面分析", "confidence": 0.55},
            confidence=0.55,
        ))

    for node in nodes:
        objects.append(CandidateObject(
            obj_type="component",
            data={
                "id": f"node_{node['node_id']}",
                "kind": "tower_node",
                "name": f"候选节点 {node['node_id']}",
                "properties": {
                    "node_id": node["node_id"],
                    "x_px": node["x_px"],
                    "y_px": node["y_px"],
                    "unit": "px",
                    "view_type": node.get("view_type") or default_view_type,
                    "solve_status": "pending_review",
                },
            },
            source={"source_type": "drawing", "reference": source,
                    "detail": "A2 端点聚类", "confidence": 0.5},
            confidence=0.5,
        ))

    # 最近节点绑定（与 DXF _find_node 同思路：扫描候选也保证拓扑引用闭合）
    node_points: List[Tuple[str, float, float]] = [
        (node["node_id"], float(node["x_px"]), float(node["y_px"])) for node in nodes
    ]

    def nearest_node(px: float, py: float) -> Optional[str]:
        best, best_d = None, float("inf")
        for nid, nx, ny in node_points:
            d = math.hypot(px - nx, py - ny)
            if d < best_d:
                best_d, best = d, nid
        return best

    assign_by_uid = {a["bar_uid"]: a for a in assignments}
    for bar in bars:
        uid = bar["bar_uid"]
        assign = assign_by_uid.get(uid, {
            "bar_uid": uid, "bar_id": f"UNLABELED_{uid}", "confidence": 0.3,
        })
        from_nid = nearest_node(float(bar["x1"]), float(bar["y1"]))
        to_nid = nearest_node(float(bar["x2"]), float(bar["y2"]))
        props = {
            "bar_id": assign["bar_id"],
            "view_type": bar.get("view_type") or default_view_type,
            "unit": "px",
            "length_px": round(math.hypot(float(bar["x2"]) - float(bar["x1"]),
                                          float(bar["y2"]) - float(bar["y1"])), 2),
            "x1_px": float(bar["x1"]),
            "y1_px": float(bar["y1"]),
            "x2_px": float(bar["x2"]),
            "y2_px": float(bar["y2"]),
            "from_node": f"node_{from_nid}" if from_nid else None,
            "to_node": f"node_{to_nid}" if to_nid else None,
            "solve_status": "pending_review",
            "association_confidence": assign.get("confidence", 0.3),
        }
        if assign.get("label_distance_px") is not None:
            props["label_distance_px"] = assign["label_distance_px"]
        # 阶段 7.4：label_distance_mm 不得被丢弃——若 MLLM 提供了物理距离，
        # 显式写回（区别于 px 距离），避免下游误把 px 距离当 mm 用。
        if assign.get("label_distance_mm") is not None:
            props["label_distance_mm"] = round(float(assign["label_distance_mm"]), 2)
        objects.append(CandidateObject(
            obj_type="component",
            data={
                "id": f"bar_{uid}",
                "kind": "tower_bar",
                "name": f"候选杆件 {assign.get('bar_id')}",
                "properties": props,
            },
            source={"source_type": "drawing", "reference": source,
                    "detail": "A2 霍夫线检测 + A3 关联", "confidence": 0.55},
            confidence=0.55,
        ))

    # 比例尺占位（无人工标定 -> placeholder，配合 strict GLB 闸门）
    objects.append(CandidateObject(
        obj_type="dimension",
        data={
            "id": "dim_scan_scale",
            "name": "扫描图比例尺 px→mm",
            "value": None,
            "unit": "mm/px",
            "origin": "placeholder",
        },
        source={"source_type": "unknown", "reference": source,
                "detail": "像素坐标到毫米比例待人工标定", "confidence": 0.0},
        confidence=0.0,
    ))

    return ModelCandidate(
        input=DrawingInput(path=source, kind="scan", tower=True),
        objects=objects,
        raw="A0→A4 多 Agent 链候选",
        backend="tower-agent-pipeline",
        meta={"link": link_meta},
    )


def run_tower_agent_pipeline(
    source: str | Path,
    out_dir: str | Path,
    mllm: Optional[MLLMBackend] = None,
    filter_noise: bool = True,
    label_snap_px: float = LABEL_SNAP_PX,
    min_association_rate: float = MIN_ASSOCIATION_RATE,
    scale: Optional[str | float] = None,
    mm_per_px: Optional[float] = None,
    use_ocr_fallback: bool = True,
    use_preprocess: bool = True,
) -> Dict[str, Any]:
    """无 DXF 扫描图主路径：A0 版面 → A1 件号 → A2 几何 → A3 关联 → A4 编译验证。

    返回与 run_tower 兼容的 dict：
        {"ok", "graph", "model_path", "steps_path", "summary_path",
         "glb_path", "solve_status"}
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = _rasterize_if_pdf(source, out_dir)
    source_path = Path(source)
    stem = source_path.stem

    graph = ProcessingGraph(name=f"tower-agent-{stem}")
    model_path = out_dir / "model.json"
    steps_path = out_dir / "steps.json"
    summary_path = out_dir / "harness_summary.json"
    crops_dir = out_dir / "agent_crops"

    views: List[Dict[str, Any]] = []
    labels: List[Dict[str, Any]] = []
    bars: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []
    assignments: List[Dict[str, Any]] = []
    link_meta: Dict[str, Any] = {}

    # ---------------- A0 版面分析（规则） ----------------
    # P0-2：按源文件名推断 view_type / z_level（front/side/plan/bom/node），
    # 覆盖到 A0 产出的 drawing_view；bom/node 不 parse 杆件（parse_bars=False）。
    from .tower_scan_views import infer_scan_view_meta
    view_meta = infer_scan_view_meta(str(source_path))
    graph.start(STAGE_LAYOUT, "版面分析（A0）", input=str(source))
    try:
        cv2, gray = _load_image(str(source))
        regions = _detect_regions(cv2, gray)
        views, whole_sheet = layout_views_from_regions(regions, gray.shape)
        # 文件名语义覆盖到每个 view（单文件扫描图通常是单一视图）
        for v in views:
            v["view_type"] = view_meta.get("view_type") or v.get("view_type") or "drawing"
            if view_meta.get("z_level") is not None:
                v["z_level"] = view_meta["z_level"]
            v["title"] = view_meta.get("title")
        # B3 比例尺标定：--scale / OCR 比例尺 / --mm-per-px → scale_mm_per_px
        # 写入每个 view（进而写入 model 的 drawing_view），未标定保持 placeholder。
        if scale is not None or mm_per_px is not None:
            from .tower_layout import calibrate_scale
            cal = calibrate_scale(str(source), scale=scale, mm_per_px=mm_per_px)
            scale_mm = cal.get("mm_per_px")
            # B3：标定成功（mm_per_px 非 None）→ derived；未标定 → placeholder
            scale_origin = "derived" if scale_mm is not None else "placeholder"
            for v in views:
                v["scale_mm_per_px"] = scale_mm
                v["scale_origin"] = scale_origin
        graph.finish(drawing_views=len(views), whole_sheet=whole_sheet,
                     regions=len(regions), method="tower_layout",
                     view_type=view_meta.get("view_type"),
                     parse_bars=view_meta.get("parse_bars", True),
                     scale_mm_per_px=(views[0].get("scale_mm_per_px") if views else None))
    except Exception as exc:
        graph.fail(str(exc))

    # P1-7：parse_bars=False（bom/node/大样）短路整条杆件链——A1 件号 OCR 与
    # A2 几何检测都不该跑（明细表/节点大样没有杆件中心线），A3 直接空跑，
    # A4 只记 metadata。否则单文件 run-tower tower_bom_hd.png 仍会误跑 A2。
    parse_bars = bool(view_meta.get("parse_bars", True))

    # ---------------- A1 件号 OCR（VLM/MLLM，B4：Tesseract 兜底） ----------------
    mllm_backend = mllm or MLLMBackend()
    if not parse_bars:
        graph.skip(STAGE_LABELS, "件号 OCR（A1）", "parse_bars=False（bom/节点大样），跳过件号 OCR")
    elif not mllm_backend.available():
        # B4：无 MLLM API 时不再直接跳过——先尝试 Tesseract 确定性 OCR 兜底，
        # 拿到件号就进 A3 关联，拿不到才标 skip（A3 只依赖 A2，全 UNLABELED）。
        if use_ocr_fallback:
            try:
                labels = _ocr_labels_from_tesseract(str(source), views)
            except Exception as exc:
                labels = []
                warnings = [f"Tesseract 兜底失败：{exc}"]
            if labels:
                graph.start(STAGE_LABELS, "件号 OCR（A1·Tesseract 兜底）", input=str(source))
                graph.finish(labels=len(labels), method="tesseract",
                             note="无 MLLM API，Tesseract 确定性 OCR 兜底")
            else:
                graph.skip(STAGE_LABELS, "件号 OCR（A1）",
                           "无 MLLM API 且 Tesseract 未识别到件号（A3 只依赖 A2，全 UNLABELED）")
        else:
            graph.skip(STAGE_LABELS, "件号 OCR（A1）",
                       "无 MLLM API，跳过件号 OCR（A3 只依赖 A2，全 UNLABELED）")
    else:
        graph.start(STAGE_LABELS, "件号 OCR（A1）", input=str(source))
        failed_calls = 0
        no_text_views = 0
        warnings: List[str] = []
        last_meta: Dict[str, Any] = {}
        for view in views:
            try:
                crop = _crop_view(str(source), view["bbox"], crops_dir, view["view_id"])
                view["crop"] = crop
                parsed, meta = mllm_backend.call_agent_json(
                    LABEL_AGENT_PROMPT, crop["path"], LABEL_AGENT_SCHEMA, agent="a1_labels")
                if parsed is None:
                    failed_calls += 1
                    last_meta = meta
                    graph_detail_hint = meta.get("failure_reason", "MLLM 调用失败")
                    warnings.append(f"{view['view_id']}: {graph_detail_hint}")
                    continue
                view_labels, problems, warn = parse_label_agent_output(parsed)
                warnings.extend(warn)
                if problems:
                    failed_calls += 1
                    warnings.extend(problems)
                    continue
                # crop 坐标（VLM 在缩放图上）-> 整图坐标
                for lab in _labels_to_full_image(view_labels, crop, view["view_id"]):
                    labels.append(lab)
                if not view_labels and str(parsed.get("note", "")).strip():
                    no_text_views += 1
            except Exception as exc:  # 单个 view 失败不影响其它 view
                failed_calls += 1
                warnings.append(f"{view['view_id']}: {exc}")

        mllm_detail: Dict[str, Any] = {}
        if last_meta:
            mllm_detail = {
                "mllm_model": last_meta.get("model"),
                "mllm_failure_reason": last_meta.get("failure_reason"),
                "mllm_raw_length": last_meta.get("raw_length"),
                "mllm_duration_ms": last_meta.get("duration_ms"),
                "mllm_elapsed_s": last_meta.get("elapsed_s"),
            }
        if labels:
            graph.finish(labels=len(labels), views=len(views),
                         failed_calls=failed_calls, warnings=warnings[:20],
                         **mllm_detail)
        elif no_text_views or (failed_calls == 0 and not labels):
            # 明确「无文字」或调用成功但 0 条 -> 视为通过（图无字）
            graph.finish(labels=0, note="无文字", views=len(views),
                         no_text_views=no_text_views, warnings=warnings[:20],
                         **mllm_detail)
        else:
            # 0 字且非图签（整图有内容却读不到字） -> pending，不级联猜值
            graph.pending("件号 OCR 0 字（非图签），待复核",
                          labels=0, failed_calls=failed_calls,
                          warnings=warnings[:20], **mllm_detail)

    # B4：MLLM 可用但 A1 没读回任何件号（0 字 / 全失败 / 全 UNLABELED）时，
    # 用 Tesseract 确定性 OCR 再兜底一次——不覆盖已有 MLLM 结果，只在空时补。
    if parse_bars and mllm_backend.available() and use_ocr_fallback and not labels:
        try:
            ocr_labels = _ocr_labels_from_tesseract(str(source), views)
        except Exception as exc:
            ocr_labels = []
        if ocr_labels:
            labels = ocr_labels
            # a1_labels 已结束（pending/failed），兜底结果记为新步骤，不影响闸门判定。
            graph.start(STAGE_LABELS_OCR_FALLBACK, "件号 OCR 兜底（A1·Tesseract）",
                        input=str(source))
            graph.finish(labels=len(labels), method="tesseract",
                         note="MLLM 0 字，Tesseract 确定性 OCR 兜底")


    # ---------------- A2 几何检测（MLLM 优先，霍夫兜底） ----------------
    if not parse_bars:
        graph.skip(STAGE_GEOMETRY, "几何检测（A2）", "parse_bars=False（bom/节点大样），跳过杆件几何检测")
    else:
        graph.start(STAGE_GEOMETRY, "几何检测（A2）", input=str(source))
        try:
            geom_input = str(source)
            pre_meta: Dict[str, Any] = {}
            if use_preprocess:
                from .tower_preprocess import preprocess_image_file
                geom_input, pre_meta = preprocess_image_file(
                    str(source), out_dir / "a2_preprocessed.png",
                )
            bars, nodes, geom_meta = [], [], {"method": "none"}
            if mllm_backend.available():
                bars, nodes, geom_meta = _mllm_detect_geometry(
                    geom_input, views, crops_dir, mllm_backend,
                )
            if not bars:
                bars, nodes, geom_meta = _detect_geometry(
                    geom_input, filter_noise=filter_noise, use_preprocess=False,
                )
                geom_meta["mllm_fallback"] = mllm_backend.available()
            if use_preprocess and pre_meta:
                geom_meta["preprocess"] = pre_meta
                geom_meta["preprocessed"] = True
            # P1-6：给 A2 产出的 bar/node 注入 view_type。单文件扫描图可能含多个
            # A0 视图（多 region），按杆件中点落在哪个 view bbox 内归属；无有效
            # 切块（whole_sheet）时回退到文件名级主视图类型。
            vt = view_meta.get("view_type") or "drawing"
            bars, nodes = _assign_view_by_bbox(bars, nodes, views, vt)
            if bars:
                graph.finish(bars=len(bars), nodes=len(nodes), **geom_meta)
            else:
                # 非图签页 0 杆 -> failed（几何检测失败，不能靠 A3 凑）
                graph.fail("几何检测 0 杆（非图签页）", **geom_meta)
        except Exception as exc:
            graph.fail(str(exc))

    # ---------------- A3 关联匹配（确定性规则） ----------------
    graph.start(STAGE_LINK, "关联匹配（A3）", input=f"labels={len(labels)}, bars={len(bars)}")
    try:
        link_meta = _associate_labels(bars, labels, snap_px=label_snap_px)
        assignments = link_meta["assignments"]
        detail = {k: v for k, v in link_meta.items() if k != "assignments"}
        if not parse_bars:
            # P1-7：bom/节点大样无杆件，A3 空跑但视为通过（不是失败），
            # A4 只记 metadata。
            graph.finish(**detail, note="parse_bars=False，无杆件可关联")
        else:
            # B2 双指标闸门：霍夫噪声高（候选杆远多于件号）时，labeled/bars 被
            # 噪声基数拉低无法反映真实关联质量，改以 label_hit_rate（命中件号/总件号）
            # 作闸门。两者都不过才 pending。
            rate = link_meta["association_rate"]
            hit = link_meta["label_hit_rate"]
            ocr_labels = link_meta["ocr_labels"]
            noisy = ocr_labels > 0 and len(bars) > ocr_labels * 3
            passed = (not noisy and rate >= min_association_rate) or \
                     (noisy and hit >= min_association_rate)
            if passed:
                graph.finish(**detail)
            else:
                gate = "label_hit_rate" if noisy else "labeled/bars"
                val = hit if noisy else rate
                graph.pending(
                    f"{gate}={val} < {min_association_rate}" +
                    (f"（霍夫噪声高，用件号命中率作闸门）" if noisy else ""),
                    **detail)
    except Exception as exc:
        graph.fail(str(exc))

    # ---------------- A4 编译验证（contract + Harness） ----------------
    graph.start(STAGE_HARNESS, "编译验证（A4）", input="A0+A1+A2+A3")
    model = None
    try:
        from ..io import validate_references
        from ..skill.contract import to_engineering_model
        from ..intake.tower_pipeline import finalize_tower_model

        candidate = _build_model_candidate(str(source), views, bars, nodes, assignments, link_meta)
        model = to_engineering_model(candidate, name=f"tower-{stem}")
        model = finalize_tower_model(model, merge=False, allow_scan=False)
        problems = validate_references(model)
        results = run_harness(model)

        status_counts: Dict[str, int] = {}
        for r in results:
            status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1
        failed_rules = [r.target_id for r in results if r.status == ValidationStatus.FAILED]
        pending_rules = [r.target_id for r in results if r.status == ValidationStatus.PENDING]

        # 扫描图默认 pending_review：r_scan_reviewed 预期 failed，因此 A4 不判
        # failed 而标 pending，等待人工确认（confirm_tower_scan）。
        if problems:
            graph.fail(f"引用完整性 {len(problems)} 项", problems=problems[:10],
                       summary=status_counts, rules=len(model.rules))
        elif failed_rules or pending_rules:
            graph.pending(
                "扫描图待人工复核（pending_review）/ 规则 pending",
                summary=status_counts, failed_rules=failed_rules,
                pending_rules=pending_rules, rules=len(model.rules))
        else:
            graph.finish(summary=status_counts, rules=len(model.rules))
    except Exception as exc:
        graph.fail(str(exc))

    # ---------------- 保存模型与日志 ----------------
    from ..io import save_model
    if model is not None:
        save_model(model, model_path)
        payload = {
            "model": model.name,
            "steps": graph.to_dict(),
            "rules": {rid: {"status": r.status.value, "message": r.message}
                      for rid, r in model.rules.items()},
            "bars": len(bars),
            "nodes": len(nodes),
            "labels": len(labels),
            "association_rate": link_meta.get("association_rate"),
            "solve_status": "pending_review",
        }
        summary_path.write_text(
            __import__("json").dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    graph.export_json(steps_path)
    ok = all(s.status != "failed" for s in graph.steps)
    return {
        "ok": ok,
        "graph": graph,
        "model_path": model_path.as_posix() if model_path.exists() else None,
        "steps_path": steps_path.as_posix(),
        "summary_path": summary_path.as_posix() if summary_path.exists() else None,
        "glb_path": None,
        "solve_status": "pending_review",
    }
