"""生成演示用输电铁塔结构施工图 DXF（立面 + 平面 + BOM）。

用于 DSH 开发 intake-tower 解析器的标准测试图纸。
单位：mm；坐标系 Z 向上；杆件为角钢中心线。
"""

from __future__ import annotations

import math

from pathlib import Path
from typing import Dict, List, Tuple

from .tower_spec import layer_names as spec_layer_names, view_origin

# 三维节点 (mm)
NODES: Dict[str, Tuple[float, float, float]] = {
    "N01": (-2000.0, -2000.0, 0.0),
    "N02": (2000.0, -2000.0, 0.0),
    "N03": (2000.0, 2000.0, 0.0),
    "N04": (-2000.0, 2000.0, 0.0),
    "N05": (-1200.0, -1200.0, 5000.0),
    "N06": (1200.0, -1200.0, 5000.0),
    "N07": (1200.0, 1200.0, 5000.0),
    "N08": (-1200.0, 1200.0, 5000.0),
    "N09": (-4000.0, 0.0, 7000.0),
    "N10": (4000.0, 0.0, 7000.0),
    "N11": (-800.0, -800.0, 7000.0),
    "N12": (800.0, -800.0, 7000.0),
    "N13": (800.0, 800.0, 7000.0),
    "N14": (-800.0, 800.0, 7000.0),
    "N15": (-1500.0, 0.0, 12000.0),
    "N16": (1500.0, 0.0, 12000.0),
}

# 杆件拓扑 + BOM 规格
BARS: List[Tuple[str, str, str, str]] = [
    # (bar_id, from, to, section)
    ("G01", "N01", "N05", "L100x8"),
    ("G02", "N02", "N06", "L100x8"),
    ("G03", "N03", "N07", "L100x8"),
    ("G04", "N04", "N08", "L100x8"),
    ("G05", "N01", "N06", "L80x6"),
    ("G06", "N02", "N05", "L80x6"),
    ("G07", "N02", "N07", "L80x6"),
    ("G08", "N03", "N06", "L80x6"),
    ("G09", "N03", "N08", "L80x6"),
    ("G10", "N04", "N07", "L80x6"),
    ("G11", "N04", "N05", "L80x6"),
    ("G12", "N01", "N08", "L80x6"),
    ("G13", "N05", "N11", "L100x8"),
    ("G14", "N06", "N12", "L100x8"),
    ("G15", "N07", "N13", "L100x8"),
    ("G16", "N08", "N14", "L100x8"),
    ("G17", "N11", "N09", "L100x10"),
    ("G18", "N14", "N09", "L100x10"),
    ("G19", "N12", "N10", "L100x10"),
    ("G20", "N13", "N10", "L100x10"),
    ("G21", "N11", "N15", "L80x6"),
    ("G22", "N14", "N15", "L80x6"),
    ("G23", "N09", "N15", "L80x6"),
    ("G24", "N12", "N16", "L80x6"),
    ("G25", "N13", "N16", "L80x6"),
    ("G26", "N10", "N16", "L80x6"),
]


def _bar_length(from_id: str, to_id: str) -> float:
    x1, y1, z1 = NODES[from_id]
    x2, y2, z2 = NODES[to_id]
    return ((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2) ** 0.5


BOM_ROWS: List[Tuple[str, str, int, int]] = [
    # (bar_id, section, length_mm, qty) — 由三维节点坐标计算
    *((bar_id, section, int(round(_bar_length(n1, n2))), 1) for bar_id, n1, n2, section in BARS),
]

# 图纸布局偏移 (mm)
ELEV_ORIGIN = (8000.0, 6000.0)   # 立面图左下角
PLAN_ORIGIN = (22000.0, 6000.0)  # 平面图左下角


def _midpoint(a: Tuple[float, float], b: Tuple[float, float]) -> Tuple[float, float]:
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def make_demo_tower_dxf(path: str | Path) -> str:
    """生成铁塔结构演示 DXF：立面图 + 平面图 + 尺寸标注 + BOM 表。"""
    import ezdxf
    from ezdxf.enums import TextEntityAlignment

    path = str(path)
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM

    # 图层（行业惯例命名，杆件/节点/标注/文字层与 schema 规范共用）
    layers = {
        "FRAME": 7,
        "TRUSS_MAIN": 1,
        "TRUSS_NODE": 3,
        "DIM": 5,
        "TEXT": 2,
        "BOM": 4,
        "TITLE": 7,
    }
    for name, color in layers.items():
        doc.layers.add(name, color=color)
    for group in ("bar_layers", "node_layers", "dim_layers", "text_layers"):
        for name in spec_layer_names(group, []):
            if name not in doc.layers:
                doc.layers.add(name, color=7)

    msp = doc.modelspace()

    # 图框
    frame_x, frame_y = 5000.0, 4000.0
    frame_w, frame_h = 32000.0, 20000.0
    msp.add_lwpolyline(
        [(frame_x, frame_y), (frame_x + frame_w, frame_y),
         (frame_x + frame_w, frame_y + frame_h), (frame_x, frame_y + frame_h)],
        close=True,
        dxfattribs={"layer": "FRAME"},
    )

    # ---- 立面图 (正视，投影 X-Z，Y 朝里) ----
    stem = Path(path).stem
    ex, ey = view_origin(stem, "elevation", ELEV_ORIGIN)

    def elev_pt(node_id: str) -> Tuple[float, float]:
        x, _, z = NODES[node_id]
        return (ex + x, ey + z)

    # 视图标题
    msp.add_text(
        "立面图 ELEVATION A-A",
        dxfattribs={"layer": "TEXT", "height": 350.0},
    ).set_placement((ex, ey - 1200.0), align=TextEntityAlignment.LEFT)

    # 正立面图只画 Y<=0 侧的杆件（真实图纸惯例：正立面与侧立面分开）
    for bar_id, n1, n2, _ in BARS:
        if NODES[n1][1] > 0 or NODES[n2][1] > 0:
            continue  # 侧立面杆件不在正立面图上画
        p1, p2 = elev_pt(n1), elev_pt(n2)
        msp.add_line(p1, p2, dxfattribs={"layer": "TRUSS_MAIN"})
        mid = _midpoint(p1, p2)
        msp.add_text(
            bar_id,
            dxfattribs={"layer": "TEXT", "height": 280.0},
        ).set_placement(
            (mid[0], mid[1] + 180.0),
            align=TextEntityAlignment.MIDDLE_CENTER,
        )

    # 节点
    for nid in NODES:
        p = elev_pt(nid)
        msp.add_circle(p, radius=120.0, dxfattribs={"layer": "TRUSS_NODE"})
        msp.add_text(
            nid,
            dxfattribs={"layer": "TEXT", "height": 200.0},
        ).set_placement((p[0], p[1] - 280.0), align=TextEntityAlignment.MIDDLE_CENTER)

    # 尺寸标注（引线 + 文字）
    def dim_horizontal(x1: float, x2: float, z: float, label: str, offset: float = -800.0):
        y_dim = ey + z + offset
        msp.add_line((ex + x1, ey + z), (ex + x1, y_dim), dxfattribs={"layer": "DIM"})
        msp.add_line((ex + x2, ey + z), (ex + x2, y_dim), dxfattribs={"layer": "DIM"})
        msp.add_line((ex + x1, y_dim), (ex + x2, y_dim), dxfattribs={"layer": "DIM"})
        msp.add_text(label, dxfattribs={"layer": "DIM", "height": 250.0}).set_placement(
            ((ex + x1 + ex + x2) / 2, y_dim - 200.0), align=TextEntityAlignment.MIDDLE_CENTER
        )

    def dim_vertical(x: float, z1: float, z2: float, label: str, offset: float = -1200.0):
        x_dim = ex + x + offset
        msp.add_line((ex + x, ey + z1), (x_dim, ey + z1), dxfattribs={"layer": "DIM"})
        msp.add_line((ex + x, ey + z2), (x_dim, ey + z2), dxfattribs={"layer": "DIM"})
        msp.add_line((x_dim, ey + z1), (x_dim, ey + z2), dxfattribs={"layer": "DIM"})
        msp.add_text(label, dxfattribs={"layer": "DIM", "height": 250.0}).set_placement(
            (x_dim - 350.0, (ey + z1 + ey + z2) / 2), align=TextEntityAlignment.MIDDLE_CENTER
        )

    dim_horizontal(-2000, 2000, 0, "4000")
    dim_vertical(-2000, 0, 5000, "5000")
    dim_vertical(-2000, 5000, 7000, "2000")
    dim_vertical(-2000, 7000, 12000, "5000")
    dim_horizontal(-4000, 4000, 7000, "8000", offset=600.0)

    # ---- 平面图 (投影 X-Y，Z=0 基础层) ----
    px, py = view_origin(stem, "plan", PLAN_ORIGIN)

    def plan_pt(node_id: str) -> Tuple[float, float]:
        x, y, _ = NODES[node_id]
        return (px + x, py + y)

    msp.add_text(
        "平面图 PLAN (基础层 Z=0)",
        dxfattribs={"layer": "TEXT", "height": 350.0},
    ).set_placement((px, py - 2800.0), align=TextEntityAlignment.LEFT)

    # 仅绘制 Z=0 和 Z=7000 水平层杆件在平面上的投影（简化：画全部杆件 XY 投影）
    for bar_id, n1, n2, _ in BARS:
        p1, p2 = plan_pt(n1), plan_pt(n2)
        msp.add_line(p1, p2, dxfattribs={"layer": "TRUSS_MAIN"})
        mid = _midpoint(p1, p2)
        msp.add_text(
            bar_id,
            dxfattribs={"layer": "TEXT", "height": 220.0},
        ).set_placement((mid[0], mid[1] + 150.0), align=TextEntityAlignment.MIDDLE_CENTER)

    for nid in NODES:
        p = plan_pt(nid)
        msp.add_circle(p, radius=100.0, dxfattribs={"layer": "TRUSS_NODE"})
        msp.add_text(
            nid,
            dxfattribs={"layer": "TEXT", "height": 180.0},
        ).set_placement((p[0], p[1] - 240.0), align=TextEntityAlignment.MIDDLE_CENTER)

    dim_horizontal_plan = lambda x1, x2, y, label: (
        msp.add_line((px + x1, py + y), (px + x1, py + y - 600), dxfattribs={"layer": "DIM"}),
        msp.add_line((px + x2, py + y), (px + x2, py + y - 600), dxfattribs={"layer": "DIM"}),
        msp.add_line((px + x1, py + y - 600), (px + x2, py + y - 600), dxfattribs={"layer": "DIM"}),
        msp.add_text(label, dxfattribs={"layer": "DIM", "height": 250.0}).set_placement(
            ((px + x1 + px + x2) / 2, py + y - 850.0), align=TextEntityAlignment.MIDDLE_CENTER
        ),
    )
    dim_horizontal_plan(-2000, 2000, -2000, "4000")

    # ---- BOM 构件明细表 ----
    bom_x, bom_y = 8000.0, 22000.0
    col_w = [1200.0, 1400.0, 1800.0, 800.0]
    row_h = 500.0
    headers = ["杆件号", "截面", "长度mm", "数量"]

    msp.add_text(
        "构件明细表 BOM",
        dxfattribs={"layer": "BOM", "height": 400.0},
    ).set_placement((bom_x, bom_y + 800.0), align=TextEntityAlignment.LEFT)

    # 表头
    cx = bom_x
    for i, h in enumerate(headers):
        msp.add_lwpolyline(
            [(cx, bom_y), (cx + col_w[i], bom_y),
             (cx + col_w[i], bom_y + row_h), (cx, bom_y + row_h)],
            close=True,
            dxfattribs={"layer": "BOM"},
        )
        msp.add_text(h, dxfattribs={"layer": "BOM", "height": 280.0}).set_placement(
            (cx + col_w[i] / 2, bom_y + row_h / 2), align=TextEntityAlignment.MIDDLE_CENTER
        )
        cx += col_w[i]

    # 数据行（前 12 行 + 省略提示）
    for row_idx, (bar_id, section, length, qty) in enumerate(BOM_ROWS[:12]):
        ry = bom_y - (row_idx + 1) * row_h
        cx = bom_x
        cells = [bar_id, section, str(length), str(qty)]
        for i, cell in enumerate(cells):
            msp.add_lwpolyline(
                [(cx, ry), (cx + col_w[i], ry),
                 (cx + col_w[i], ry + row_h), (cx, ry + row_h)],
                close=True,
                dxfattribs={"layer": "BOM"},
            )
            msp.add_text(cell, dxfattribs={"layer": "BOM", "height": 240.0}).set_placement(
                (cx + col_w[i] / 2, ry + row_h / 2), align=TextEntityAlignment.MIDDLE_CENTER
            )
            cx += col_w[i]

    msp.add_text(
        "... 共 26 根杆件，详见电子明细",
        dxfattribs={"layer": "BOM", "height": 280.0},
    ).set_placement((bom_x, bom_y - 13 * row_h - 200.0), align=TextEntityAlignment.LEFT)

    # ---- 标题栏 ----
    title_x, title_y = 28000.0, 4200.0
    msp.add_lwpolyline(
        [(title_x, title_y), (title_x + 5000, title_y),
         (title_x + 5000, title_y + 3500), (title_x, title_y + 3500)],
        close=True,
        dxfattribs={"layer": "TITLE"},
    )
    msp.add_text(
        "输电线路铁塔头施工图",
        dxfattribs={"layer": "TITLE", "height": 400.0},
    ).set_placement((title_x + 2500, title_y + 2600.0), align=TextEntityAlignment.MIDDLE_CENTER)
    msp.add_text(
        "图号: TT-DEMO-001",
        dxfattribs={"layer": "TITLE", "height": 280.0},
    ).set_placement((title_x + 2500, title_y + 1800.0), align=TextEntityAlignment.MIDDLE_CENTER)
    msp.add_text(
        "比例: 1:50  单位: mm",
        dxfattribs={"layer": "TITLE", "height": 280.0},
    ).set_placement((title_x + 2500, title_y + 1200.0), align=TextEntityAlignment.MIDDLE_CENTER)
    msp.add_text(
        "engineering-trace demo",
        dxfattribs={"layer": "TITLE", "height": 220.0},
    ).set_placement((title_x + 2500, title_y + 600.0), align=TextEntityAlignment.MIDDLE_CENTER)

    doc.saveas(path)
    return path


def export_tower_dxf_preview(dxf_path: str | Path, png_path: str | Path) -> str:
    """将 DXF 导出为 PNG 预览图。"""
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext, config
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    import matplotlib.pyplot as plt

    dxf_path = Path(dxf_path)
    png_path = Path(png_path)
    doc = ezdxf.readfile(str(dxf_path))
    ctx = RenderContext(doc)
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE)
    fig = plt.figure(figsize=(16, 10), dpi=150)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_aspect("equal")
    backend = MatplotlibBackend(ax)
    Frontend(ctx, backend, config=cfg).draw_layout(doc.modelspace())
    ax.autoscale()
    ax.axis("off")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(png_path), bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)
    return str(png_path)


def make_demo_tower_bom_csv(path: str | Path) -> str:
    """生成与 DXF BOM 表一致的 CSV。"""
    path = Path(path)
    lines = ["bar_id,section,length_mm,qty"]
    for bar_id, section, length, qty in BOM_ROWS:
        lines.append(f"{bar_id},{section},{length},{qty}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


if __name__ == "__main__":
    out_dir = Path(__file__).resolve().parents[2] / "examples"
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf = make_demo_tower_dxf(out_dir / "tower_demo.dxf")
    png = export_tower_dxf_preview(dxf, out_dir / "tower_demo.png")
    csv = make_demo_tower_bom_csv(out_dir / "tower_bom.csv")
    print(f"DXF: {dxf}")
    print(f"PNG: {png}")
    print(f"BOM: {csv}")
