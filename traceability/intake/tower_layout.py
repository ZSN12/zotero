"""铁塔扫描图管线（Phase 4，最小可用版）。

PDF/PNG → 版面分析 → 杆件线检测 → 端点聚类 → 编号 OCR（可选）
→ 输出 EngineeringModel（pixel 坐标，confidence 全局 ≤ 0.6）

原则：
    * 扫描图产出默认不进终版 3D，只进人工复核队列；
      因此所有 tower_bar / tower_node 都是 pixel 坐标的候选，
      solve_status=pending_review，绝不换算成毫米（换算尺度需要人工标定）。
    * 每个对象必须有 SourceRef（文件 + 位置/端点 + confidence）。
    * 无 OCR 时绝不在杆件上猜编号。

依赖：numpy / opencv-python（cv2）。OCR 可选 pytesseract。
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..model import (
    Component,
    Dimension,
    DimensionOrigin,
    EngineeringModel,
    SourceRef,
    SourceType,
    ValidationStatus,
)

# 扫描图候选对象的置信度上限（原则：模型识别永远 < 1.0，扫描图更低）
SCAN_MAX_CONFIDENCE = 0.6

# 线检测默认参数
CANNY_LOW = 60
CANNY_HIGH = 160
HOUGH_RHO = 1.0
HOUGH_THETA_DEG = 1.0
HOUGH_THRESHOLD = 60
HOUGH_MIN_LINE = 30.0
HOUGH_MAX_GAP = 8.0

# 深色内容阈值（灰度 < 此值才当作杆件/标注，过滤浅色网格线）
INK_THRESHOLD = 160
# 候选杆件最短长度（pixel）：过滤文字笔画、坐标轴刻度、图例短线
MIN_BAR_PX = 80.0

# 共线合并容差
MERGE_ANGLE_DEG = 4.0
MERGE_DIST = 6.0
MERGE_OVERLAP_GAP = 12.0

# 版面分析：空白行/列判定阈值
REGION_GAP = 8
# 版面区域过滤：墨迹占比 / 最小边长 / 最小面积占比（相对整图）
MIN_REGION_INK = 0.003
MIN_REGION_EDGE_PX = 80
MIN_REGION_AREA_FRACTION = 0.01
# 主内容区占整图面积超过此比例时，丢弃更小的碎片 region（避免页边空白条）
DOMINANT_REGION_AREA_FRACTION = 0.15
DOMINANT_REGION_MIN_SUBAREA_FRACTION = 0.05

# P2-2 比例尺标定：扫描/渲染默认 DPI（与生成器导出时一致）
DEFAULT_SCAN_DPI = 150.0

# P2-3 OCR 件号空间关联：文本 bbox 到杆件中点的最大距离（pixel）
OCR_LABEL_SNAP_PX = 250.0

# P2-1 噪声过滤：真实杆件至少一端与其它杆件共点（degree >= 2）
# 孤立线段（两端 degree 都为 1）视为 dim 线 / 图例线 / 边框线候选
NOISE_MAX_ISOLATED_DEG = 1


def _load_image(image_path: str):
    """读取灰度图（cv2 可选安装，缺失时抛可读错误）。"""
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "扫描图管线需要 opencv-python：pip install opencv-python-headless"
        ) from exc
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"无法读取图片：{image_path}")
    return cv2, img


def _bbox_area(bbox: Tuple[int, int, int, int]) -> int:
    x0, y0, x1, y1 = bbox
    return max(0, x1 - x0) * max(0, y1 - y0)


def _split_content_bands(profile, length: int, gap: int = REGION_GAP) -> List[Tuple[int, int]]:
    """把行/列墨迹 profile 切成连续的「有内容」区段（合并间隔 ≤ gap 的碎片）。"""
    runs: List[Tuple[int, int]] = []
    i = 0
    while i < length:
        if profile[i] == 0:
            i += 1
            continue
        start = i
        while i < length and profile[i] > 0:
            i += 1
        runs.append((start, i))
    if not runs:
        return []
    merged = [runs[0]]
    for s, e in runs[1:]:
        ps, pe = merged[-1]
        if s - pe <= gap:
            merged[-1] = (ps, e)
        else:
            merged.append((s, e))
    return [(a, b) for a, b in merged if b - a > 2]


def _content_bbox(binary) -> Optional[Tuple[int, int, int, int]]:
    """全图墨迹紧包围盒；无墨迹时返回 None。"""
    import numpy as np

    ys, xs = np.where(binary > 0)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _filter_layout_regions(
    regions: List[Dict],
    width: int,
    height: int,
) -> List[Dict]:
    """丢弃页边空白条等无效 region，保留可 OCR 的内容块。"""
    img_area = width * height
    min_w = max(MIN_REGION_EDGE_PX, width // 20)
    min_h = max(MIN_REGION_EDGE_PX, height // 20)
    min_area = img_area * MIN_REGION_AREA_FRACTION

    good: List[Dict] = []
    for region in regions:
        x0, y0, x1, y1 = region["bbox"]
        rw, rh = x1 - x0, y1 - y0
        if region["ink_ratio"] < MIN_REGION_INK:
            continue
        if rw < min_w or rh < min_h:
            continue
        if rw * rh < min_area:
            continue
        good.append(region)

    if not good:
        return []

    good.sort(key=lambda r: r["ink_ratio"] * _bbox_area(r["bbox"]), reverse=True)
    largest = good[0]
    largest_area = _bbox_area(largest["bbox"])
    if (
        largest_area >= img_area * DOMINANT_REGION_AREA_FRACTION
        and largest["ink_ratio"] >= MIN_REGION_INK
    ):
        min_sub = img_area * DOMINANT_REGION_MIN_SUBAREA_FRACTION
        trimmed = [r for r in good if _bbox_area(r["bbox"]) >= min_sub]
        if trimmed:
            good = trimmed

    return good


def _detect_regions(cv2, gray) -> List[Dict]:
    """版面分析：按空白行/列把图像切成矩形区域（粗粒度）。

    返回 [{"bbox": (x0, y0, x1, y1), "ink_ratio": float}]。
    这一步不识别区域语义（正立面/平面/BOM），只提供候选版面，
    语义标注留给人/后续 OCR。
    """
    h, w = gray.shape[:2]
    binary = (gray < 128).astype("uint8") * 255
    row_ink = (binary > 0).sum(axis=1)
    col_ink = (binary > 0).sum(axis=0)

    y_bands = _split_content_bands(row_ink, h)
    x_bands = _split_content_bands(col_ink, w)
    if not y_bands or not x_bands:
        cb = _content_bbox(binary)
        if cb is None:
            return []
        x0, y0, x1, y1 = cb
        ink = float((binary[y0:y1, x0:x1] > 0).mean())
        return [{"bbox": cb, "ink_ratio": round(ink, 4)}]

    regions: List[Dict] = []
    for (y0, y1) in y_bands:
        for (x0, x1) in x_bands:
            if y1 - y0 < 4 or x1 - x0 < 4:
                continue
            ink = float((binary[y0:y1, x0:x1] > 0).mean())
            regions.append({
                "bbox": (int(x0), int(y0), int(x1), int(y1)),
                "ink_ratio": round(ink, 4),
            })

    filtered = _filter_layout_regions(regions, w, h)
    if filtered:
        return filtered

    cb = _content_bbox(binary)
    if cb is not None:
        x0, y0, x1, y1 = cb
        ink = float((binary[y0:y1, x0:x1] > 0).mean())
        return [{"bbox": cb, "ink_ratio": round(ink, 4)}]

    regions.sort(key=lambda r: r["ink_ratio"], reverse=True)
    return regions


def layout_views_from_regions(
    regions: List[Dict],
    img_shape: Tuple[int, ...],
    max_views: int = 8,
) -> Tuple[List[Dict], bool]:
    """把版面 region 转成 A0 drawing_view 列表；必要时回退 whole_sheet。

    返回 (views, whole_sheet)。whole_sheet=True 表示整图单 view（无有效切块）。
    """
    h, w = int(img_shape[0]), int(img_shape[1])
    good = _filter_layout_regions(regions, w, h) if regions else []

    if not good:
        return [{
            "view_id": "whole_sheet",
            "bbox": [0, 0, w, h],
            "ink_ratio": 0.0,
            "scale_mm_per_px": None,
            "scale_origin": "placeholder",
        }], True

    views = []
    for i, region in enumerate(good[:max_views], start=1):
        views.append({
            "view_id": f"view_{i:02d}",
            "bbox": [int(v) for v in region["bbox"]],
            "ink_ratio": region["ink_ratio"],
            "scale_mm_per_px": None,
            "scale_origin": "placeholder",
        })
    return views, False


def _detect_line_segments(cv2, gray) -> List[Tuple[float, float, float, float]]:
    """霍夫线检测，返回 [(x1, y1, x2, y2)]（pixel 坐标）。

    先用灰度阈值把浅色网格/底纹滤掉，只保留深色「内容」，
    再对二值图做霍夫检测（比 Canny 更稳，且不受浅色虚线干扰）。
    """
    mask = (gray < INK_THRESHOLD).astype("uint8") * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    lines = cv2.HoughLinesP(
        mask, HOUGH_RHO, HOUGH_THETA_DEG * math.pi / 180.0,
        HOUGH_THRESHOLD, minLineLength=HOUGH_MIN_LINE, maxLineGap=HOUGH_MAX_GAP,
    )
    out: List[Tuple[float, float, float, float]] = []
    if lines is None:
        return out
    for line in lines:
        arr = line[0] if getattr(line, "ndim", 1) == 2 else line
        if len(arr) != 4:
            continue
        x1, y1, x2, y2 = (float(v) for v in arr)
        if math.hypot(x2 - x1, y2 - y1) < 8.0:
            continue
        out.append((x1, y1, x2, y2))
    return out


def _merge_collinear(segments: List[Tuple[float, float, float, float]]):
    """共线合并：把同一条杆件上被霍夫切碎的线段接起来。

    聚类判据：方向角接近 + 原点到直线的距离接近，且沿方向投影有重叠/相邻。
    每个簇沿方向投影取 min/max 得到合并后的线段。
    """
    normals: List[Tuple[float, float]] = []
    for (x1, y1, x2, y2) in segments:
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux
        rho = nx * x1 + ny * y1
        normals.append((nx, ny, rho))

    n = len(segments)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            ni, nj = normals[i], normals[j]
            dot = ni[0] * nj[0] + ni[1] * nj[1]
            angle = math.degrees(math.acos(max(-1.0, min(1.0, abs(dot)))))
            if angle > MERGE_ANGLE_DEG:
                continue
            if abs(ni[2] - nj[2]) > MERGE_DIST:
                continue
            # 方向投影区间（用第 i 条的方向作公共轴）
            ux, uy = -ni[1], ni[0]
            pi = _project(segments[i], ux, uy)
            pj = _project(segments[j], ux, uy)
            if min(pi[1], pj[1]) + MERGE_OVERLAP_GAP < max(pi[0], pj[0]):
                continue
            union(i, j)

    clusters: Dict[int, List[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    merged: List[Tuple[float, float, float, float]] = []
    for members in clusters.values():
        if len(members) == 1:
            merged.append(segments[members[0]])
            continue
        # 用最长线段的单位方向作公共轴
        longest = max(members, key=lambda k: math.hypot(
            segments[k][2] - segments[k][0], segments[k][3] - segments[k][1]))
        dx = segments[longest][2] - segments[longest][0]
        dy = segments[longest][3] - segments[longest][1]
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        bounds = []
        for k in members:
            lo, hi = _project(segments[k], ux, uy)
            p1 = (ux * lo, uy * lo)
            p2 = (ux * hi, uy * hi)
            bounds.append((lo, p1))
            bounds.append((hi, p2))
        lo = min(b[0] for b in bounds)
        hi = max(b[0] for b in bounds)
        p1 = (ux * lo, uy * lo)
        p2 = (ux * hi, uy * hi)
        merged.append((p1[0], p1[1], p2[0], p2[1]))
    return merged


def _project(seg, ux, uy):
    x1, y1, x2, y2 = seg
    p1 = x1 * ux + y1 * uy
    p2 = x2 * ux + y2 * uy
    return (min(p1, p2), max(p1, p2))


def _cluster_endpoints(segments, eps=14.0):
    """端点聚类：相距 < eps 的端点合并为候选节点。"""
    nodes: List[Dict] = []
    for seg in segments:
        for (x, y) in ((seg[0], seg[1]), (seg[2], seg[3])):
            merged = False
            for node in nodes:
                if math.hypot(x - node["x"], y - node["y"]) <= eps:
                    node["handles"].append((round(x, 1), round(y, 1)))
                    n = len(node["handles"])
                    node["x"] = (node["x"] * (n - 1) + x) / n
                    node["y"] = (node["y"] * (n - 1) + y) / n
                    merged = True
                    break
            if not merged:
                nodes.append({"x": x, "y": y, "handles": [(round(x, 1), round(y, 1))]})
    return nodes


def filter_noise_segments(
    segments: List[Tuple[float, float, float, float]],
    eps: float = 14.0,
    keep_longer_than: Optional[float] = None,
) -> Tuple[List[Tuple[float, float, float, float]], List[Dict]]:
    """P2-1 扫描噪声过滤：去掉 dim 线 / 图例线 / 边框线等孤立候选。

    规则：
        * 端点聚类后，若一根线段两个端点都是 degree 1（没有任何其它杆件
          与之共点），则判定为孤立噪声（真实铁塔杆件至少一端接在节点上）。
        * 若给出 keep_longer_than，超长线段即使孤立也保留（主材/大斜材
          可能有一端悬空在截图边界）。

    返回 (保留的线段, 被过滤线段清单)。
    """
    nodes = _cluster_endpoints(segments, eps=eps)

    def node_key(px, py):
        best, best_d = None, float("inf")
        for n in nodes:
            d = math.hypot(px - n["x"], py - n["y"])
            if d < best_d:
                best_d, best = d, (round(n["x"], 1), round(n["y"], 1))
        return best if best_d <= eps else None

    deg: Dict[Tuple, int] = {}
    for seg in segments:
        a = node_key(seg[0], seg[1])
        b = node_key(seg[2], seg[3])
        if a is not None:
            deg[a] = deg.get(a, 0) + 1
        if b is not None:
            deg[b] = deg.get(b, 0) + 1

    def near_hv(seg):
        dx, dy = seg[2] - seg[0], seg[3] - seg[1]
        length = math.hypot(dx, dy) or 1.0
        cos_x = abs(dx) / length
        cos_y = abs(dy) / length
        return cos_x > math.cos(math.radians(10.0)) or cos_y > math.cos(math.radians(10.0))

    keep: List[Tuple[float, float, float, float]] = []
    removed: List[Dict] = []
    for seg in segments:
        a = node_key(seg[0], seg[1])
        b = node_key(seg[2], seg[3])
        da = deg.get(a, 0)
        db = deg.get(b, 0)
        length = math.hypot(seg[2] - seg[0], seg[3] - seg[1])
        isolated = da <= NOISE_MAX_ISOLATED_DEG and db <= NOISE_MAX_ISOLATED_DEG
        # 只过滤「孤立且近水平/竖直的短线」：dim 线 / 图例线 / 刻度线。
        # 真实铁塔杆件至少一端接节点（有非平行杆件），斜材即使检测时
        # 端点孤立也保留——宁可多留候选，不可降低真实杆件召回。
        noise = isolated and near_hv(seg)
        if noise and (keep_longer_than is None or length < keep_longer_than):
            removed.append({"segment": seg, "reason": "孤立 dim/图例/刻度短线"})
            continue
        keep.append(seg)
    return keep, removed


def _detect_scale_text(text: str) -> Optional[float]:
    """从 OCR 文本识别比例尺标注（如 `1:50` / `SCALE 1:100` / `比例 1:100`）。"""
    if not text:
        return None
    m = re.search(r"1\s*[:：]\s*(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def calibrate_scale(
    image_path: str | Path,
    scale: Optional[str | float] = None,
    mm_per_px: Optional[float] = None,
    dpi: float = DEFAULT_SCAN_DPI,
    ocr_text: Optional[str] = None,
) -> Dict:
    """P2-2 比例尺标定：px -> mm。

    输入优先级：
        1. mm_per_px（显式，最可靠）
        2. scale（"1:50" 或 50，图纸比例；配合 dpi 换算 mm/px）
        3. ocr_text / 图片 OCR 中识别出的 `1:N` 比例尺标注

    返回 {"mm_per_px", "source", "method"}；无法标定时 mm_per_px=None，
    source="未标定"，节点保持 px 坐标（人工复核语义不变）。
    """
    if mm_per_px is not None:
        return {"mm_per_px": float(mm_per_px), "source": "用户输入 mm/px", "method": "explicit"}

    ratio = None
    if scale is not None:
        if isinstance(scale, str):
            m = re.match(r"^\s*1\s*[:：]\s*(\d+(?:\.\d+)?)\s*$", scale.strip())
            if m:
                ratio = float(m.group(1))
            else:
                try:
                    ratio = float(scale.strip())
                except ValueError:
                    ratio = None
        else:
            ratio = float(scale)
        if ratio:
            # 图纸比例 1:ratio 表示图纸 1 mm = 实物 ratio mm；
            # 默认扫描/渲染 150 dpi => 1 px = 25.4/150 mm 图纸单位
            mm_per_px = (25.4 / dpi) * ratio
            return {"mm_per_px": round(mm_per_px, 6), "source": f"图纸比例 1:{ratio:g} @ {dpi:.0f}dpi",
                    "method": "scale"}

    if ocr_text is None:
        ocr_text = _try_ocr_text(str(image_path))
    ratio = _detect_scale_text(ocr_text or "")
    if ratio:
        mm_per_px = (25.4 / dpi) * ratio
        return {"mm_per_px": round(mm_per_px, 6), "source": f"OCR 识别比例 1:{ratio:g}",
                "method": "ocr"}
    return {"mm_per_px": None, "source": "未标定（保持 px，待人工复核）", "method": "none"}


def _bar_midpoint_px(model: EngineeringModel, bar_id: str) -> Optional[Tuple[float, float]]:
    bar = model.components.get(bar_id)
    if bar is None:
        return None
    f = bar.properties.get("from_node")
    t = bar.properties.get("to_node")
    nf = model.components.get(f)
    nt = model.components.get(t)
    if not nf or not nt:
        return None
    xf, yf = nf.properties.get("x_px"), nf.properties.get("y_px")
    xt, yt = nt.properties.get("x_px"), nt.properties.get("y_px")
    if None in (xf, yf, xt, yt):
        return None
    return ((float(xf) + float(xt)) / 2, (float(yf) + float(yt)) / 2)


def associate_ocr_labels(
    model: EngineeringModel,
    image_path: Optional[str | Path] = None,
    boxes: Optional[List[Dict]] = None,
    snap_px: float = OCR_LABEL_SNAP_PX,
    id_pattern: str = r"(?:M\d{4}|[GSB]\d{1,4})",
) -> int:
    """P2-3 OCR 件号空间关联。

    boxes: OCR 输出 [{text, bbox:[x0,y0,x1,y1]}]。不传则尝试 pytesseract。
    每个识别出的件号文本，找最近的 tower_bar 中点，距离 < snap_px 则关联，
    写入 bar.properties["bar_id"]，并把 confidence 提升到 0.75（仍 pending_review）。

    返回成功关联的杆件数。绝不猜编号：OCR 没识别到就保持 SCAN_xxxx / UNLABELED。
    """
    if boxes is None and image_path is not None:
        boxes = _ocr_boxes(image_path)
    if not boxes:
        return 0

    pattern = re.compile(r"\b(" + id_pattern + r")\b")
    bars = [c for c in model.components.values() if c.kind == "tower_bar"]
    mid_by_bar = {}
    for bar in bars:
        mid = _bar_midpoint_px(model, bar.id)
        if mid is not None:
            mid_by_bar[bar.id] = mid

    associated = 0
    for box in boxes:
        text = box.get("text", "")
        m = pattern.search(text or "")
        if not m:
            continue
        label = m.group(1)
        bbox = box.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        best_bar, best_d = None, snap_px
        for bar_id, (mx, my) in mid_by_bar.items():
            d = math.hypot(cx - mx, cy - my)
            if d < best_d:
                best_d, best_bar = d, bar_id
        if best_bar is not None:
            bar = model.components[best_bar]
            bar.properties["bar_id"] = label
            bar.properties["ocr_label_conf"] = 0.75
            if bar.source is not None:
                bar.source.confidence = max(bar.source.confidence, 0.75)
            associated += 1
    return associated


def _ocr_boxes(image_path: str | Path) -> List[Dict]:
    """用 pytesseract image_to_data 产出文本框（不可用则空列表）。"""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return []
    try:
        data = pytesseract.image_to_data(Image.open(image_path), output_type=pytesseract.Output.DICT)
    except Exception:
        return []
    boxes: List[Dict] = []
    for i, text in enumerate(data.get("text", []) or []):
        text = (text or "").strip()
        if not text:
            continue
        x, y, w, h = (data["left"][i], data["top"][i], data["width"][i], data["height"][i])
        if w <= 0 or h <= 0:
            continue
        boxes.append({"text": text, "bbox": [float(x), float(y), float(x + w), float(y + h)]})
    return boxes


def _try_ocr_text(image_path: str) -> Optional[str]:
    """可选 OCR：未安装 pytesseract 时返回 None（绝不猜）。"""
    try:
        import pytesseract
        from PIL import Image
        return pytesseract.image_to_string(Image.open(image_path))
    except Exception:
        return None


def analyze_tower_scan(
    image_path: str | Path,
    model_name: Optional[str] = None,
    max_bars: int = 600,
    filter_noise: bool = True,
    scale: Optional[str | float] = None,
    mm_per_px: Optional[float] = None,
    dpi: float = DEFAULT_SCAN_DPI,
    ocr_boxes: Optional[List[Dict]] = None,
    associate_ocr: bool = False,
) -> EngineeringModel:
    """扫描图管线入口（P2 增强版）。

    输出候选模型（confidence ≤ 0.6，默认 solve_status=pending_review）：
        * scan_file / scan_region  版面上下文
        * tower_bar               霍夫线检测 + 共线合并 + 噪声过滤的候选杆件
        * tower_node              端点聚类的候选节点
        * dim_scan_scale          比例尺标定维度（未标定则 placeholder）

    参数：
        * filter_noise=False 可关闭 P2-1 噪声过滤（回归对照用）
        * scale / mm_per_px 触发 P2-2 标定：节点/杆件写入 x_mm/y_mm/length_mm
        * associate_ocr=True 触发 P2-3 件号空间关联（需 OCR 能力）
    """
    image_path = str(image_path)
    stem = Path(image_path).stem
    cv2, gray = _load_image(image_path)
    model = EngineeringModel(name=model_name or f"scan-{stem}")

    # 文件上下文
    model.add_component(Component(
        id="scan_file",
        name=stem,
        kind="scan_file",
        source=SourceRef(SourceType.DRAWING, image_path, confidence=1.0),
        properties={"path": image_path, "width_px": int(gray.shape[1]), "height_px": int(gray.shape[0])},
    ))

    # 版面分析：候选区域（无语义，只给 bbox）
    regions = _detect_regions(cv2, gray)
    for i, region in enumerate(regions[:32], start=1):
        x0, y0, x1, y1 = region["bbox"]
        model.add_component(Component(
            id=f"scan_region_{i:02d}",
            name=f"扫描区域 {i:02d}",
            kind="scan_region",
            source=SourceRef(
                SourceType.DRAWING, image_path,
                detail=f"bbox=({x0},{y0})-({x1},{y1})",
                confidence=0.5,
            ),
            properties={
                "bbox": [x0, y0, x1, y1],
                "ink_ratio": region["ink_ratio"],
                "unit": "px",
            },
        ))

    # 线检测 + 共线合并 + 长度过滤 + P2-1 噪声过滤
    segments = _detect_line_segments(cv2, gray)
    merged = [
        seg for seg in _merge_collinear(segments)
        if math.hypot(seg[2] - seg[0], seg[3] - seg[1]) >= MIN_BAR_PX
    ]
    removed_noise: List[Dict] = []
    if filter_noise:
        merged, removed_noise = filter_noise_segments(merged)
    merged = merged[:max_bars]

    # P2-2 比例尺标定
    scale_info = calibrate_scale(image_path, scale=scale, mm_per_px=mm_per_px, dpi=dpi)
    mm_per_px_value = scale_info.get("mm_per_px")

    # 候选节点
    nodes = _cluster_endpoints(merged)
    node_ids: List[str] = []
    node_coords: List[Tuple[float, float]] = []
    for i, node in enumerate(nodes, start=1):
        nid = f"N{i:03d}"
        node_ids.append(nid)
        node_coords.append((node["x"], node["y"]))
        props = {
            "node_id": nid,
            "x_px": round(node["x"], 2),
            "y_px": round(node["y"], 2),
            "unit": "px",
            "solve_status": "pending_review",
        }
        if mm_per_px_value is not None:
            props["x_mm"] = round(node["x"] * mm_per_px_value, 2)
            props["y_mm"] = round(node["y"] * mm_per_px_value, 2)
            props["unit"] = "mm"
        model.add_component(Component(
            id=f"node_{nid}",
            name=f"候选节点 {nid}",
            kind="tower_node",
            source=SourceRef(
                SourceType.DRAWING, image_path,
                detail=f"端点聚类, handles={len(node['handles'])}",
                confidence=0.5,
            ),
            properties=props,
        ))

    def nearest_node(px, py, eps=20.0):
        best, best_d = None, float("inf")
        for k, (x, y) in enumerate(node_coords):
            d = math.hypot(px - x, py - y)
            if d < best_d:
                best_d, best = d, k
        return node_ids[best] if best is not None and best_d <= eps else None

    # 候选杆件
    for i, seg in enumerate(merged, start=1):
        x1, y1, x2, y2 = seg
        length = math.hypot(x2 - x1, y2 - y1)
        from_nid = nearest_node(x1, y1)
        to_nid = nearest_node(x2, y2)
        props = {
            "bar_id": f"SCAN_{i:04d}",  # 编号待人工确认，绝不猜真实件号
            "length_px": round(length, 2),
            "unit": "px",
            "view_type": None,
            "from_node": f"node_{from_nid}" if from_nid else None,
            "to_node": f"node_{to_nid}" if to_nid else None,
            "solve_status": "pending_review",
        }
        if mm_per_px_value is not None:
            props["length_mm"] = round(length * mm_per_px_value, 2)
            props["unit"] = "mm"
        model.add_component(Component(
            id=f"bar_scan_{i:04d}",
            name=f"候选杆件 {i:04d}",
            kind="tower_bar",
            source=SourceRef(
                SourceType.DRAWING, image_path,
                detail=f"hough merged, endpoints=({x1:.0f},{y1:.0f})-({x2:.0f},{y2:.0f})",
                confidence=0.55,
            ),
            properties=props,
        ))

    # P2-3 OCR 件号空间关联（可选；未识别到则保持 SCAN_xxxx，绝不猜）
    ocr_text = _try_ocr_text(image_path)
    if associate_ocr:
        associated = associate_ocr_labels(model, image_path, boxes=ocr_boxes)
        if associated:
            model.add_dimension(Dimension(
                id="dim_scan_ocr_association",
                name="扫描图 OCR 件号关联数",
                value=associated,
                unit="bars",
                origin=DimensionOrigin.DERIVED,
                source=SourceRef(SourceType.DRAWING, image_path, detail="OCR 空间关联", confidence=0.75),
                applies_to="scan_file",
            ))
    if ocr_text:
        model.add_component(Component(
            id="ocr_candidates",
            name="OCR 候选文本",
            kind="text_annotation",
            source=SourceRef(SourceType.DRAWING, image_path, detail="OCR 输出", confidence=0.3),
            properties={"raw_text": ocr_text[:2000]},
        ))

    # 比例尺维度（P2-2）
    if mm_per_px_value is not None:
        model.add_dimension(Dimension(
            id="dim_scan_scale",
            name="扫描图比例尺 px→mm",
            value=mm_per_px_value,
            unit="mm/px",
            origin=DimensionOrigin.MEASURED,
            source=SourceRef(SourceType.DRAWING, image_path,
                             detail=scale_info.get("source", "比例尺标定"), confidence=0.8),
            applies_to="scan_file",
        ))
    else:
        model.add_dimension(Dimension(
            id="dim_scan_scale",
            name="扫描图比例尺（待人工标定）",
            value=None,
            unit="mm/px",
            origin=DimensionOrigin.PLACEHOLDER,
            source=SourceRef(SourceType.UNKNOWN, image_path,
                             detail="像素坐标到毫米的比例待人工标定", confidence=0.0),
            applies_to="scan_file",
        ))
        # 兼容旧版占位维度 ID
        model.add_dimension(Dimension(
            id="dim_placeholder_scan",
            name="扫描图比例尺（待人工标定）",
            value=None,
            unit="mm/px",
            origin=DimensionOrigin.PLACEHOLDER,
            source=SourceRef(SourceType.UNKNOWN, image_path,
                             detail="像素坐标到毫米的比例待人工标定", confidence=0.0),
            applies_to="scan_file",
        ))
    model.depend("dim_scan_scale", "scan_file")

    # 记录噪声过滤结果，供解析率报告使用（不改变模型对象语义）
    model.add_dimension(Dimension(
        id="dim_scan_noise_removed",
        name="扫描图噪声过滤移除候选数",
        value=len(removed_noise),
        unit="segments",
        origin=DimensionOrigin.DERIVED,
        source=SourceRef(SourceType.DRAWING, image_path, detail="P2-1 孤立线段过滤", confidence=0.9),
        applies_to="scan_file",
    ))
    model.depend("dim_scan_noise_removed", "scan_file")
    return model


def confirm_tower_scan(model: EngineeringModel) -> EngineeringModel:
    """P2-5 人工确认闸门：把扫描候选 solve_status 设为 verified。

    调用后，r_scan_reviewed 规则可通过，strict 导出不再被 pending_review 阻断。
    """
    n = 0
    for comp in model.components.values():
        if comp.kind in ("tower_bar", "tower_node"):
            if comp.properties.get("solve_status") == "pending_review":
                comp.properties["solve_status"] = "verified"
                n += 1
    if "r_scan_reviewed" in model.rules:
        model.rules["r_scan_reviewed"].status = ValidationStatus.PENDING
    return model
