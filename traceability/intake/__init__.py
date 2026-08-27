"""INTAKE 层：多源图纸接入。

    * dwg.py   DWG/DXF 构件抽取（ezdxf）
    * ocr.py   扫描图尺寸标注提取（可插拔 OCR）
"""

from .dwg import extract_from_dxf
from .ocr import extract_dimensions_from_image

__all__ = ["extract_from_dxf", "extract_dimensions_from_image"]
