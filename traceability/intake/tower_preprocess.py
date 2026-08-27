"""扫描/渲染图线重绘栅格预处理（Phase C）。

在 A2 霍夫线检测前增强弱中心线、抑制底纹噪声：
    1. 对比度拉伸（百分位 clip + CLAHE）
    2. 去斑（连通域面积过滤）
    3. 线重绘（形态学骨架化，强调中心线）

原则：只做确定性图像处理，不引入随机性；meta 记录各步参数供基准评测。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _require_cv2():
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "扫描图预处理需要 opencv-python：pip install opencv-python-headless"
        ) from exc
    return cv2, np


def contrast_stretch(gray, cv2, np, low_pct: float = 2.0, high_pct: float = 98.0):
    """百分位对比度拉伸 + CLAHE，提升弱线对比度。"""
    lo = float(np.percentile(gray, low_pct))
    hi = float(np.percentile(gray, high_pct))
    if hi <= lo + 1:
        stretched = gray.copy()
    else:
        stretched = np.clip((gray.astype("float32") - lo) * 255.0 / (hi - lo), 0, 255).astype("uint8")
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(stretched)


def denoise_spots(binary, cv2, np, min_area: int = 12):
    """去除小连通域（文字笔画碎点、扫描噪点）。"""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    out = np.zeros_like(binary)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            out[labels == i] = 255
    return out


def skeletonize(binary, cv2, np):
    """形态学骨架化（Zhang-Suen 近似：迭代腐蚀+开运算）。"""
    skel = np.zeros(binary.shape, dtype="uint8")
    element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    img = binary.copy()
    while True:
        eroded = cv2.erode(img, element)
        opened = cv2.dilate(eroded, element)
        temp = cv2.subtract(img, opened)
        skel = cv2.bitwise_or(skel, temp)
        img = eroded.copy()
        if cv2.countNonZero(img) == 0:
            break
    return skel


def preprocess_for_scan(
    gray,
    *,
    ink_threshold: int = 160,
    min_spot_area: int = 12,
    skeletonize_lines: bool = True,
    cv2=None,
    np=None,
) -> Tuple[Any, Dict[str, Any]]:
    """对灰度图做线重绘预处理，返回 (preprocessed_gray, meta)。

    preprocessed_gray 可直接传给霍夫线检测（深色内容为白）。
    """
    if cv2 is None or np is None:
        cv2, np = _require_cv2()

    meta: Dict[str, Any] = {
        "method": "line_repaint",
        "ink_threshold": ink_threshold,
        "min_spot_area": min_spot_area,
        "skeletonize": skeletonize_lines,
    }

    stretched = contrast_stretch(gray, cv2, np)
    meta["contrast"] = "percentile_2_98+clahe"

    # 深色墨迹 -> 白，浅色底 -> 黑（与 tower_layout 霍夫输入一致）
    binary = (stretched < ink_threshold).astype("uint8") * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    cleaned = denoise_spots(opened, cv2, np, min_area=min_spot_area)
    meta["spots_removed"] = int(cv2.countNonZero(opened) - cv2.countNonZero(cleaned))

    if skeletonize_lines:
        skel = skeletonize(cleaned, cv2, np)
        # 轻微膨胀恢复霍夫可检线宽（骨架过细会漏检）
        dilated = cv2.dilate(skel, kernel, iterations=1)
        work = dilated
        meta["skeleton_pixels"] = int(cv2.countNonZero(skel))
    else:
        work = cleaned

    # 转回灰度：白线黑底 -> 深色线浅色底（与 _detect_line_segments 输入一致）
    out = np.where(work > 0, 0, 255).astype("uint8")
    meta["ink_pixels"] = int(cv2.countNonZero(work))
    meta["shape"] = [int(out.shape[0]), int(out.shape[1])]
    return out, meta


def preprocess_image_file(
    image_path: str | Path,
    out_path: Optional[str | Path] = None,
    **kwargs,
) -> Tuple[str, Dict[str, Any]]:
    """读取位图 → 预处理 → 可选写出 PNG。返回 (path, meta)。"""
    from .tower_layout import _load_image

    cv2, gray = _load_image(str(image_path))
    _, np = _require_cv2()
    processed, meta = preprocess_for_scan(gray, cv2=cv2, np=np, **kwargs)
    target = Path(out_path) if out_path else Path(image_path).with_name(
        Path(image_path).stem + "_preprocessed.png"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(target), processed)
    meta["source"] = str(image_path)
    meta["output"] = str(target)
    return str(target), meta
