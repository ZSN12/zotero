"""PDF 转图入口（P1-3）。

依赖 pymupdf（优先）或 pdf2image。把 PDF 首页栅格化成 PNG，
供 MLLM `_encode_image` 与扫描图管线使用。

原则：
    * 找不到可用库时给出可读错误，绝不静默失败
    * 生成的 PNG 放系统临时目录（同名 + 页码），可反复使用
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional


class PDFRasterError(RuntimeError):
    """PDF 栅格化失败（无库 / 文件损坏 / 空页）。"""


def _raster_with_pymupdf(pdf_path: str, out_png: Path, dpi: int = 150,
                         page_index: int = 0) -> None:
    try:
        import pymupdf  # 新版包名
    except ImportError:
        import fitz as pymupdf  # 旧版包名

    doc = pymupdf.open(pdf_path)
    try:
        if doc.page_count < 1:
            raise PDFRasterError(f"PDF 没有页面：{pdf_path}")
        if page_index >= doc.page_count:
            raise PDFRasterError(
                f"PDF 页码超出范围：{pdf_path} 共 {doc.page_count} 页，请求第 {page_index + 1} 页")
        page = doc[page_index]
        pix = page.get_pixmap(dpi=dpi)
        pix.save(str(out_png))
    finally:
        doc.close()


def _raster_with_pdf2image(pdf_path: str, out_png: Path, dpi: int = 150,
                           page_index: int = 0) -> None:
    from pdf2image import convert_from_path

    pages = convert_from_path(pdf_path, dpi=dpi, first_page=page_index + 1,
                              last_page=page_index + 1)
    if not pages:
        raise PDFRasterError(f"PDF 无法栅格化：{pdf_path}")
    pages[0].save(str(out_png))


def rasterize_pdf_to_png(
    pdf_path: str | Path,
    out_png: Optional[str | Path] = None,
    dpi: int = 150,
    page: int = 1,
) -> str:
    """把 PDF 指定页（1-based）栅格化为 PNG，返回 PNG 路径。"""
    pdf_path = str(pdf_path)
    if not pdf_path.lower().endswith(".pdf"):
        raise PDFRasterError(f"不是 PDF 文件：{pdf_path}")

    if out_png is None:
        out_png = Path(tempfile.gettempdir()) / (Path(pdf_path).stem + f"_p{page}_raster.png")
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)

    try:
        _raster_with_pymupdf(pdf_path, out_png, dpi, page - 1)
    except (ImportError, ModuleNotFoundError):
        try:
            _raster_with_pdf2image(pdf_path, out_png, dpi, page - 1)
        except ImportError as exc:
            raise PDFRasterError(
                "PDF 转图需要 pymupdf 或 pdf2image：pip install pymupdf"
            ) from exc
    if not out_png.exists() or out_png.stat().st_size == 0:
        raise PDFRasterError(f"PDF 栅格化失败（空输出）：{pdf_path}")
    return str(out_png)


def rasterize_pdf_pages(
    pdf_path: str | Path,
    out_dir: Optional[str | Path] = None,
    dpi: int = 150,
    pages: Optional[list] = None,
) -> List[str]:
    """D2：把 PDF 多页栅格化为 PNG 列表。

    pages：None 表示全部页；可传 [1, 2] 指定页；返回 PNG 路径列表。
    """
    pdf_path = Path(pdf_path)
    if pdf_path.suffix.lower() != ".pdf":
        raise PDFRasterError(f"不是 PDF 文件：{pdf_path}")
    out_dir = Path(out_dir) if out_dir is not None else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        import pymupdf  # noqa: F401
    except ImportError:
        try:
            import fitz  # noqa: F401
        except ImportError:
            raise PDFRasterError("PDF 转图需要 pymupdf：pip install pymupdf")

    import pymupdf as pm
    try:
        doc = pm.open(str(pdf_path))
    except Exception:
        import fitz as pm
        doc = pm.open(str(pdf_path))
    page_count = doc.page_count
    idxs = list(range(page_count)) if pages is None else [int(p) - 1 for p in pages]
    outputs: List[str] = []
    for i in idxs:
        out_png = out_dir / f"{pdf_path.stem}_p{i + 1}.png"
        _raster_with_pymupdf(str(pdf_path), out_png, dpi, i)
        outputs.append(str(out_png))
    doc.close()
    return outputs


def pdf_available() -> bool:
    """检查 PDF 栅格化能力是否可用。"""
    try:
        import pymupdf  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import pdf2image  # noqa: F401
        return True
    except ImportError:
        return False
