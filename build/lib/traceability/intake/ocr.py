"""扫描图尺寸标注提取（可插拔 OCR）。

阶段 1 DRAWING INTAKE 的补充实现：
从扫描图（PNG/PDF 转的图片）中提取尺寸标注。

设计原则：
    * 默认使用「无 OCR 的保守回退」：只做图像基础信息，标注全部
      标记为 origin=placeholder（占位，待补测），置信度 0.0，绝不猜值。
    * 如果环境安装了 pytesseract + tesseract 二进制，自动启用真 OCR，
      提取到的文本会以低置信度进入候选列表，仍标记为 placeholder 等复核。

这样保证：宁可「不知道」，也不「假装知道」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..model import (
    Component,
    Dimension,
    DimensionOrigin,
    EngineeringModel,
    SourceRef,
    SourceType,
)


def _try_import_pytesseract():
    try:
        import pytesseract  # type: ignore
        return pytesseract
    except Exception:
        return None


def _try_ocr_text(image_path: str) -> Optional[str]:
    """尝试 OCR，失败返回 None（绝不抛异常中断流程）。"""
    pyt = _try_import_pytesseract()
    if pyt is None:
        return None
    try:
        from PIL import Image
        img = Image.open(image_path)
        return pyt.image_to_string(img)
    except Exception:
        return None


def extract_dimensions_from_image(
    image_path: str | Path,
    model_name: str = "scan-extract",
) -> EngineeringModel:
    """从扫描图提取尺寸占位项。

    返回的模型里：
        * 文件本身作为一个 Component（来源可追溯）
        * OCR 文本（若有）作为低置信度候选 Component
        * 一条 placeholder 尺寸，明确声明「待补测」
    """
    image_path = str(image_path)
    model = EngineeringModel(name=model_name)

    # 文件上下文
    model.add_component(Component(
        id="scan_file",
        name=Path(image_path).stem,
        kind="scan_file",
        source=SourceRef(SourceType.DRAWING, image_path, detail="扫描图原件", confidence=1.0),
        properties={"path": image_path},
    ))

    # OCR 候选（若有）
    text = _try_ocr_text(image_path)
    if text:
        model.add_component(Component(
            id="ocr_candidates",
            name="OCR 候选文本",
            kind="text_annotation",
            source=SourceRef(SourceType.DRAWING, image_path, detail="OCR 输出", confidence=0.3),
            properties={"raw_text": text},
        ))

    # 占位尺寸：明确表示「尚未实测」
    model.add_dimension(Dimension(
        id="dim_placeholder_scan",
        name="扫描图待补测尺寸",
        value=None,
        unit="",
        origin=DimensionOrigin.PLACEHOLDER,
        source=SourceRef(
            SourceType.UNKNOWN, image_path,
            detail="等待人工测量或真 OCR 标注解析",
            confidence=0.0,
        ),
        applies_to="scan_file",
    ))
    model.depend("dim_placeholder_scan", "scan_file")
    return model


def ocr_available() -> bool:
    """环境是否具备真 OCR 能力。"""
    return _try_ocr_text is not None and _try_import_pytesseract() is not None
