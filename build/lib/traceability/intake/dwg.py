"""DWG/DXF 构件抽取（基于 ezdxf）。

阶段 1 DRAWING INTAKE 的落地实现：
从 DXF 文件中读取 LINE / LWPOLYLINE / CIRCLE / TEXT / INSERT 等实体，
按简单启发式规则归类为工程构件，并保留「原始位置」作为来源追溯。

限制说明：
    真正的符号识别（阀门符号、泵符号）需要符号库匹配，这里先做
    几何实体归类（线=管道候选、圆=法兰/孔候选、文本=标注候选），
    把「识别不准」的置信度如实写低，交给人工复核。
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, List, Optional

from ..model import (
    Component,
    Dimension,
    DimensionOrigin,
    EngineeringModel,
    SourceRef,
    SourceType,
)


def _source(path: str, entity_type: str, handle: str, confidence: float) -> SourceRef:
    return SourceRef(
        source_type=SourceType.DRAWING,
        reference=path,
        detail=f"entity={entity_type}, handle={handle}",
        confidence=confidence,
        extracted_by="ezdxf-intake",
    )


def extract_from_dxf(dxf_path: str | Path, model_name: str = "dxf-extract") -> EngineeringModel:
    """从 DXF 抽取构件/尺寸，返回一个 EngineeringModel。"""
    import ezdxf

    dxf_path = str(dxf_path)
    doc = ezdxf.readfile(dxf_path)
    model = EngineeringModel(name=model_name)
    model = model.add_component(Component(
        id="file_ctx", name=Path(dxf_path).stem, kind="drawing_file",
        source=_source(dxf_path, "FILE", "-", 1.0),
        properties={"path": dxf_path},
    ))

    msp = doc.modelspace()
    line_count = 0
    circle_count = 0
    text_count = 0

    # 1) 直线 → 管道/线候选
    for e in msp.query("LINE"):
        line_count += 1
        length = math.dist((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))
        comp = Component(
            id=f"line_{e.dxf.handle}",
            name=f"线段 #{e.dxf.handle}",
            kind="line_segment",
            source=_source(dxf_path, "LINE", e.dxf.handle, 0.7),  # 未做符号识别，置信度低
            properties={
                "start": (round(e.dxf.start.x, 2), round(e.dxf.start.y, 2)),
                "end": (round(e.dxf.end.x, 2), round(e.dxf.end.y, 2)),
                "length": round(length, 2),
            },
        )
        model.add_component(comp)
        model.add_dimension(Dimension(
            id=f"dim_len_{e.dxf.handle}",
            name=f"线段 #{e.dxf.handle} 长度",
            value=round(length, 2),
            unit="unit",
            origin=DimensionOrigin.DERIVED,  # 由端点坐标算出，可复核
            source=_source(dxf_path, "LINE", e.dxf.handle, 0.7),
            applies_to=comp.id,
        ))
        model.depend(f"dim_len_{e.dxf.handle}", comp.id)

    # 2) 圆 → 孔/法兰候选
    for e in msp.query("CIRCLE"):
        circle_count += 1
        comp = Component(
            id=f"circle_{e.dxf.handle}",
            name=f"圆 #{e.dxf.handle}",
            kind="circle",
            source=_source(dxf_path, "CIRCLE", e.dxf.handle, 0.65),
            properties={
                "center": (round(e.dxf.center.x, 2), round(e.dxf.center.y, 2)),
                "radius": round(e.dxf.radius, 2),
            },
        )
        model.add_component(comp)
        model.add_dimension(Dimension(
            id=f"dim_rad_{e.dxf.handle}",
            name=f"圆 #{e.dxf.handle} 半径",
            value=round(e.dxf.radius, 2),
            unit="unit",
            origin=DimensionOrigin.MEASURED,  # 直接从图元几何读取，视为实测
            source=_source(dxf_path, "CIRCLE", e.dxf.handle, 0.9),
            applies_to=comp.id,
        ))

    # 3) 文本 → 标注候选（不猜内容，只记录位置，等 OCR/人工确认）
    for e in msp.query("TEXT"):
        text_count += 1
        comp = Component(
            id=f"text_{e.dxf.handle}",
            name=f"文本 #{e.dxf.handle}",
            kind="text_annotation",
            source=_source(dxf_path, "TEXT", e.dxf.handle, 0.5),
            properties={
                "text": e.dxf.text,
                "insert": (round(e.dxf.insert.x, 2), round(e.dxf.insert.y, 2)),
            },
        )
        model.add_component(comp)

    return model


ODA_CONVERTER_ENV = "ODA_CONVERTER"


def _repo_tools_root() -> Optional[Path]:
    """返回仓库根目录下的 tools 目录（<repo>/../tools，即 zotore/tools）。

    dwg.py 位于 <repo>/traceability/intake/，parents[2] = <repo>。
    ODA 约定安装位置为 <repo>/../tools/oda-file-converter/。
    """
    repo = Path(__file__).resolve().parents[2]
    tools = repo.parent / "tools"
    return tools if tools.exists() else None


def _oda_binary_candidates() -> List[str]:
    """探测 ODA File Converter 可执行文件的候选路径。

    顺序：
        1. 环境变量 ODA_CONVERTER（显式指定，优先级最高）
        2. 系统 PATH
        3. zotore/tools/oda-file-converter/ODAFileConverter.app/...（本地安装）
    """
    candidates: List[str] = []
    env = os.environ.get(ODA_CONVERTER_ENV)
    if env:
        candidates.append(env)

    for name in ("ODAFileConverter", "oda-file-converter", "dwg2dxf", "teigha"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    tools = _repo_tools_root()
    if tools is not None:
        oda_dir = tools / "oda-file-converter"
        for rel in (
            "ODAFileConverter.app/Contents/MacOS/ODAFileConverter",  # macOS
            "ODAFileConverter",                                        # Linux
            "ODAFileConverter.exe",                                    # Windows
        ):
            bin_path = oda_dir / rel
            if bin_path.exists():
                candidates.append(str(bin_path))
    return candidates


def find_oda_converter() -> Optional[str]:
    """返回第一个可用的 ODA File Converter 可执行路径（没有则 None）。"""
    for c in _oda_binary_candidates():
        if Path(c).exists() or shutil.which(c):
            return c
    return None


def _oda_version() -> str:
    return os.environ.get("ODA_VERSION", "ACAD2018")


def _oda_audit() -> str:
    return os.environ.get("ODA_AUDIT", "1")


def _convert_dwg_dir(input_dir: Path, out_dir: Path, oda_bin: str) -> None:
    """调用 ODA File Converter：ODAFileConverter <in_dir> <out_dir> <ver> DXF 0 <audit>。

    ODA 的接口是「输入目录 → 输出目录」，不是单文件路径；单文件调用会被
    ODA 当作目录处理，这正是 ensure_dxf() 以前调用方式错误的根因。
    """
    input_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [oda_bin, str(input_dir), str(out_dir), _oda_version(), "DXF", "0", _oda_audit()]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"DWG 转换失败（{oda_bin}）：{proc.stderr or proc.stdout or '退出码 ' + str(proc.returncode)}"
        )


def ensure_dxf(path: str | Path, out_dir: Optional[str | Path] = None) -> str:
    """P1-8 DWG 原生支持：把 DWG 转成 DXF（或返回原 DXF 路径）。

    调用方式已修正为 ODA 的「输入目录 → 输出目录」语义：
        * 单文件会先复制到临时输入目录，再让 ODA 批量转换；
        * 转换器自动探测：环境变量 ODA_CONVERTER > 系统 PATH >
          <repo>/../tools/oda-file-converter/...（无需手动 dwg2dxf.sh）。

    out_dir：输出目录（默认临时目录）；返回转换后的 DXF 路径。
    """
    path = str(path)
    p = Path(path)
    if p.suffix.lower() == ".dxf":
        return path
    if p.suffix.lower() != ".dwg":
        raise ValueError(f"不支持的文件类型：{path}（仅支持 dxf/dwg）")

    if not p.exists():
        oda = find_oda_converter()
        if oda is None:
            raise RuntimeError(
                "DWG 原生支持需要 ODA File Converter 或 dwg2dxf 转换层。\n"
                f"  文件：{path}\n"
                "  做法 1：设置 ODA_CONVERTER 指向 ODAFileConverter 可执行文件；\n"
                "  做法 2：安装到 zotore/tools/oda-file-converter/；\n"
                "  做法 3：先手动转换：ODAFileConverter <in_dir> <out_dir> ACAD2018 DXF 0 1；\n"
                "  做法 4：本项目也支持直接喂转换后的 DXF（intake-tower foo.dxf）。"
            )
        # 文件不存在但检测到转换器：让 ODA 报出明确错误
        raise RuntimeError(f"DWG 文件不存在，无法转换：{path}")

    oda_bin = find_oda_converter()
    if oda_bin is None:
        raise RuntimeError(
            "DWG 原生支持需要 ODA File Converter 或 dwg2dxf 转换层。\n"
            f"  文件：{path}\n"
            "  做法 1：设置 ODA_CONVERTER 指向 ODAFileConverter 可执行文件；\n"
            "  做法 2：安装到 zotore/tools/oda-file-converter/；\n"
            "  做法 3：先手动转换：ODAFileConverter <in_dir> <out_dir> ACAD2018 DXF 0 1；\n"
            "  做法 4：本项目也支持直接喂转换后的 DXF（intake-tower foo.dxf）。"
        )

    out_dir = Path(out_dir) if out_dir is not None else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="oda-input-") as tmp_in:
        in_dir = Path(tmp_in)
        shutil.copy2(p, in_dir / p.name)
        _convert_dwg_dir(in_dir, out_dir, oda_bin)

    expected = out_dir / (p.stem + ".dxf")
    if not expected.exists():
        # ODA 偶尔保留原样大小写
        for f in out_dir.glob("*.dxf"):
            if f.stem.lower() == p.stem.lower():
                return str(f)
        raise RuntimeError(f"DWG 转换失败（{oda_bin}）：未找到输出 DXF {expected}")
    return str(expected)


def ensure_dxf_batch(
    input_path: str | Path,
    out_dir: str | Path,
) -> List[str]:
    """批量 DWG → DXF：接受单个 DWG 或目录，返回转换后的 DXF 路径列表。

    目录内所有 .dwg（大小写不敏感）都会被转换；单个文件则只转该文件。
    非 DWG 文件（如 .dxf）按原路径原样返回。
    """
    input_path = Path(input_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dwg_files: List[Path] = []
    passthrough: List[str] = []
    if input_path.is_dir():
        for f in sorted(input_path.iterdir()):
            if f.suffix.lower() == ".dwg":
                dwg_files.append(f)
            elif f.suffix.lower() == ".dxf":
                passthrough.append(str(f))
    elif input_path.suffix.lower() == ".dwg":
        dwg_files = [input_path]
    elif input_path.suffix.lower() == ".dxf":
        return [str(input_path)]

    if not dwg_files and not passthrough:
        raise ValueError(f"输入路径没有 DWG/DXF 文件：{input_path}")

    if dwg_files:
        oda_bin = find_oda_converter()
        if oda_bin is None:
            raise RuntimeError(
                "DWG 原生支持需要 ODA File Converter 或 dwg2dxf 转换层。\n"
                f"  输入：{input_path}\n"
                "  做法 1：设置 ODA_CONVERTER 指向 ODAFileConverter 可执行文件；\n"
                "  做法 2：安装到 zotore/tools/oda-file-converter/。"
            )
        with tempfile.TemporaryDirectory(prefix="oda-input-") as tmp_in:
            in_dir = Path(tmp_in)
            for f in dwg_files:
                shutil.copy2(f, in_dir / f.name)
            _convert_dwg_dir(in_dir, out_dir, oda_bin)

    results = list(passthrough)
    for f in dwg_files:
        expected = out_dir / (f.stem + ".dxf")
        if expected.exists():
            results.append(str(expected))
        else:
            for g in out_dir.glob("*.dxf"):
                if g.stem.lower() == f.stem.lower():
                    results.append(str(g))
                    break
            else:
                raise RuntimeError(f"DWG 转换失败：未找到 {f.name} 对应的 DXF 输出")
    return sorted(set(results))


def make_demo_dxf(path: str | Path) -> str:
    """生成一个演示用 DXF 文件（一条管线 + 两个圆法兰 + 两个标注文本）。

    方便在没有真实 DWG 时演示完整抽取流程。
    """
    import ezdxf

    path = str(path)
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    # 一条代表管道的直线
    msp.add_line((0, 0), (100, 0))
    # 两个法兰圆
    msp.add_circle((0, 0), radius=10)
    msp.add_circle((100, 0), radius=10)
    # 标注文本
    msp.add_text("P-101", dxfattribs={"height": 5}).set_placement((50, 20))
    msp.add_text("DN100", dxfattribs={"height": 5}).set_placement((50, -15))

    doc.saveas(path)
    return path
