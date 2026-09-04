"""生成 110kV 猫头型输电铁塔全套施工图 DXF。

参照国内 110kV 单回路猫头塔（SD11 类）典型参数：
  - 呼高 21m，根开 5240mm，横担高 16.2m，横担外伸 6.5m
  - 塔身 13 节间格构式柱，主材/水平材/斜材分级截面
  - 含：正立面、侧立面、三层平面图、1-1 剖面、节点大样 K1、全量 BOM

单位 mm；杆件中心线建模；图层符合 CAD 解析习惯。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .tower_spec import layer_names as spec_layer_names, view_origin, view_regions

Vec3 = Tuple[float, float, float]
Bar = Tuple[str, str, str, str, str]  # id, n1, n2, section, kind


@dataclass
class TowerModel:
    name: str
    nodes: Dict[str, Vec3] = field(default_factory=dict)
    bars: List[Bar] = field(default_factory=list)

    def add_node(self, nid: str, pos: Vec3) -> None:
        self.nodes[nid] = pos

    def bar_length(self, n1: str, n2: str) -> float:
        x1, y1, z1 = self.nodes[n1]
        x2, y2, z2 = self.nodes[n2]
        return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)

    def bom_rows(self) -> List[Tuple[str, str, int, int]]:
        return [
            (bid, sec, int(round(self.bar_length(n1, n2))), 1)
            for bid, n1, n2, sec, _ in self.bars
        ]


def _leg_section(level: int, n_panels: int) -> str:
    ratio = level / max(n_panels, 1)
    if ratio < 0.35:
        return "L160×12"
    if ratio < 0.65:
        return "L125×10"
    return "L100×8"


def build_110kv_cathead_tower() -> TowerModel:
    """程序化生成 110kV 猫头塔空间桁架（约 300+ 杆件）。"""
    m = TowerModel(name="110kV猫头塔 SD11-21")
    bars: List[Bar] = []
    seq = 1

    def add_bar(n1: str, n2: str, section: str, kind: str) -> str:
        nonlocal seq
        bid = f"M{seq:04d}"
        seq += 1
        bars.append((bid, n1, n2, section, kind))
        return bid

    # ---- 塔身参数 ----
    n_panels = 13
    body_top_z = 16200.0
    total_h = 21000.0
    base_hw = 2620.0  # 半根开
    body_top_hw = 820.0
    z_levels = [body_top_z * i / n_panels for i in range(n_panels + 1)]

    # 四角节点 L{level}_{corner}  corner 1..4 = (+,+)(-,+)(-,-)(+,-)
    for li, z in enumerate(z_levels):
        hw = base_hw - (base_hw - body_top_hw) * (z / body_top_z)
        corners = [(hw, hw), (-hw, hw), (-hw, -hw), (hw, -hw)]
        for ci, (x, y) in enumerate(corners, start=1):
            m.add_node(f"L{li:02d}_{ci}", (x, y, z))

    # 主腿（主材）
    for ci in range(1, 5):
        for li in range(n_panels):
            add_bar(
                f"L{li:02d}_{ci}", f"L{li+1:02d}_{ci}",
                _leg_section(li, n_panels), "LEG",
            )

    # 水平材（每层四边）
    for li in range(n_panels + 1):
        for ci in range(1, 5):
            add_bar(f"L{li:02d}_{ci}", f"L{li:02d}_{(ci % 4) + 1}", "L90×8", "HORIZ")

    # 面内斜材 X 形（每节 4 面 × 2）
    for li in range(n_panels):
        for ci in range(1, 5):
            nj = (ci % 4) + 1
            add_bar(f"L{li:02d}_{ci}", f"L{li+1:02d}_{nj}", "L75×6", "DIAG")
            add_bar(f"L{li:02d}_{nj}", f"L{li+1:02d}_{ci}", "L75×6", "DIAG")

    # 水平面内斜材（每层对角）
    for li in range(n_panels + 1):
        add_bar(f"L{li:02d}_1", f"L{li:02d}_3", "L75×6", "PLAN_D")
        add_bar(f"L{li:02d}_2", f"L{li:02d}_4", "L75×6", "PLAN_D")

    # 塔脚加强膝撑（4 条）
    knee_z = z_levels[1] * 0.55
    knee_hw = base_hw * 0.92
    for ci, (sx, sy) in enumerate([(1, 1), (-1, 1), (-1, -1), (1, -1)], start=1):
        kn = f"KNEE_{ci}"
        m.add_node(kn, (sx * knee_hw, sy * knee_hw, knee_z))
        add_bar(f"L00_{ci}", kn, "L100×8", "KNEE")
        add_bar(kn, f"L01_{ci}", "L100×8", "KNEE")

    # ---- 横担（正面 Y=0 平面，猫头塔单回路）----
    z_arm = body_top_z
    arm_pts = [
        ("CA0_L", -body_top_hw, 0, z_arm),
        ("CA1_L", -4500.0, 0, z_arm),
        ("CA2_L", -6500.0, 0, z_arm),
        ("CA0_R", body_top_hw, 0, z_arm),
        ("CA1_R", 4500.0, 0, z_arm),
        ("CA2_R", 6500.0, 0, z_arm),
    ]
    for nid, x, y, z in arm_pts:
        m.add_node(nid, (x, y, z))

    # 横担与塔身连接
    add_bar("L13_1", "CA0_R", "L125×10", "CROSS")
    add_bar("L13_4", "CA0_R", "L125×10", "CROSS")
    add_bar("L13_2", "CA0_L", "L125×10", "CROSS")
    add_bar("L13_3", "CA0_L", "L125×10", "CROSS")
    # 横担主材
    add_bar("CA0_L", "CA1_L", "L125×10", "CROSS")
    add_bar("CA1_L", "CA2_L", "L125×10", "CROSS")
    add_bar("CA0_R", "CA1_R", "L125×10", "CROSS")
    add_bar("CA1_R", "CA2_R", "L125×10", "CROSS")
    # 横担上斜撑
    add_bar("L12_1", "CA1_R", "L90×8", "CROSS_D")
    add_bar("L12_4", "CA1_R", "L90×8", "CROSS_D")
    add_bar("L12_2", "CA1_L", "L90×8", "CROSS_D")
    add_bar("L12_3", "CA1_L", "L90×8", "CROSS_D")
    add_bar("CA1_L", "CA2_L", "L90×8", "CROSS_D")  # 端部三角
    add_bar("CA0_L", "CA2_L", "L90×8", "CROSS_D")
    add_bar("CA1_R", "CA2_R", "L90×8", "CROSS_D")
    add_bar("CA0_R", "CA2_R", "L90×8", "CROSS_D")
    # 横担竖向吊点杆
    hang_z = z_arm - 1800.0
    for side, cx in [("L", -5500.0), ("R", 5500.0)]:
        hn = f"HANG_{side}"
        m.add_node(hn, (cx, 0, hang_z))
        ca = f"CA2_{side}" if side == "L" else f"CA2_{side}"
        add_bar(ca, hn, "L75×6", "HANG")
        add_bar(hn, f"CA1_{side}", "L75×6", "HANG")

    # ---- 塔头（猫头）16.2m ~ 21m ----
    head_z = [17500.0, 18800.0, 19500.0, 21000.0]
    head_hw = [650.0, 480.0, 320.0]
    for i, z in enumerate(head_z[:3]):
        hw = head_hw[i]
        for ci, (x, y) in enumerate([(hw, hw), (-hw, hw), (-hw, -hw), (hw, -hw)], start=1):
            m.add_node(f"H{i+1}_{ci}", (x, y, z))

    m.add_node("EAR_L", (-1400.0, 0, 19500.0))
    m.add_node("EAR_R", (1400.0, 0, 19500.0))
    m.add_node("PEAK", (0.0, 0.0, total_h))
    m.add_node("PEAK_L", (-900.0, 0.0, 20200.0))
    m.add_node("PEAK_R", (900.0, 0.0, 20200.0))

    # 塔头主材收敛
    for ci in range(1, 5):
        add_bar(f"L13_{ci}", f"H1_{ci}", "L100×8", "HEAD_LEG")
    for ci in range(1, 5):
        add_bar(f"H1_{ci}", f"H2_{ci}", "L90×8", "HEAD_LEG")
    for ci in range(1, 5):
        add_bar(f"H2_{ci}", f"H3_{ci}", "L75×6", "HEAD_LEG")

    # 塔头水平材
    for hi in range(1, 4):
        for ci in range(1, 5):
            add_bar(f"H{hi}_{ci}", f"H{hi}_{(ci % 4) + 1}", "L75×6", "HEAD_H")

    # 塔头斜材
    for hi in range(2):
        for ci in range(1, 5):
            nj = (ci % 4) + 1
            add_bar(f"H{hi+1}_{ci}", f"H{hi+2}_{nj}", "L75×6", "HEAD_D")
            add_bar(f"H{hi+1}_{nj}", f"H{hi+2}_{ci}", "L75×6", "HEAD_D")

    # 猫耳与地线支架
    add_bar("H3_1", "EAR_R", "L75×6", "HEAD_D")
    add_bar("H3_2", "EAR_L", "L75×6", "HEAD_D")
    add_bar("EAR_L", "PEAK_L", "L75×6", "HEAD_D")
    add_bar("EAR_R", "PEAK_R", "L75×6", "HEAD_D")
    add_bar("PEAK_L", "PEAK", "L63×5", "HEAD_D")
    add_bar("PEAK_R", "PEAK", "L63×5", "HEAD_D")
    add_bar("H3_1", "PEAK_R", "L63×5", "HEAD_D")
    add_bar("H3_2", "PEAK_L", "L63×5", "HEAD_D")

    m.bars = bars
    return m


# ---------------------------------------------------------------------------
# DXF 绘制
# ---------------------------------------------------------------------------

LAYER_COLORS = {
    "FRAME": 7,
    "LEG": 1,           # 主材 红
    "HORIZ": 3,         # 水平 绿
    "DIAG": 5,          # 斜材 蓝
    "CROSS": 6,         # 横担 紫
    "HEAD": 4,          # 塔头 青
    "KNEE": 30,
    "HANG": 34,
    "NODE": 2,
    "DIM": 140,
    "TEXT": 8,
    "BOM": 4,
    "TITLE": 7,
    "HIDDEN": 9,
    "DETAIL": 1,
    "PLATE": 30,
}


def _kind_layer(kind: str) -> str:
    if kind == "LEG":
        return "LEG"
    if kind in ("HORIZ", "PLAN_D"):
        return "HORIZ"
    if kind in ("DIAG", "CROSS_D", "HEAD_D", "HEAD_H"):
        return "DIAG"
    if kind in ("CROSS", "HANG"):
        return "CROSS"
    if kind.startswith("HEAD"):
        return "HEAD"
    if kind == "KNEE":
        return "KNEE"
    return "DIAG"


def _mid(a: Tuple[float, float], b: Tuple[float, float]) -> Tuple[float, float]:
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _draw_bars(
    msp,
    model: TowerModel,
    pt_fn,
    label_major_only: bool = False,
    label_size: float = 120.0,
) -> None:
    from ezdxf.enums import TextEntityAlignment

    for bid, n1, n2, _, kind in model.bars:
        p1, p2 = pt_fn(n1), pt_fn(n2)
        layer = _kind_layer(kind)
        msp.add_line(p1, p2, dxfattribs={"layer": layer})
        if label_major_only and kind not in ("LEG", "CROSS", "HEAD_LEG"):
            continue
        mid = _mid(p1, p2)
        msp.add_text(
            bid,
            dxfattribs={"layer": "TEXT", "height": label_size},
        ).set_placement((mid[0], mid[1] + label_size * 0.6), align=TextEntityAlignment.MIDDLE_CENTER)


def _draw_nodes(msp, model: TowerModel, pt_fn, radius: float = 60.0, labels: bool = False) -> None:
    from ezdxf.enums import TextEntityAlignment

    for nid, _ in model.nodes.items():
        p = pt_fn(nid)
        msp.add_circle(p, radius=radius, dxfattribs={"layer": "NODE"})
        if labels:
            msp.add_text(
                nid,
                dxfattribs={"layer": "TEXT", "height": radius * 1.2},
            ).set_placement((p[0], p[1] - radius * 2), align=TextEntityAlignment.MIDDLE_CENTER)


def _dim_h(msp, x1, x2, y, label, ox, oy, off=-500.0):
    from ezdxf.enums import TextEntityAlignment
    yd = oy + y + off
    msp.add_line((ox + x1, oy + y), (ox + x1, yd), dxfattribs={"layer": "DIM"})
    msp.add_line((ox + x2, oy + y), (ox + x2, yd), dxfattribs={"layer": "DIM"})
    msp.add_line((ox + x1, yd), (ox + x2, yd), dxfattribs={"layer": "DIM"})
    msp.add_text(label, dxfattribs={"layer": "DIM", "height": 200.0}).set_placement(
        ((ox + x1 + ox + x2) / 2, yd - 280.0), align=TextEntityAlignment.MIDDLE_CENTER
    )


def _dim_v(msp, x, z1, z2, label, ox, oy, off=-800.0):
    from ezdxf.enums import TextEntityAlignment
    xd = ox + x + off
    msp.add_line((ox + x, oy + z1), (xd, oy + z1), dxfattribs={"layer": "DIM"})
    msp.add_line((ox + x, oy + z2), (xd, oy + z2), dxfattribs={"layer": "DIM"})
    msp.add_line((xd, oy + z1), (xd, oy + z2), dxfattribs={"layer": "DIM"})
    msp.add_text(label, dxfattribs={"layer": "DIM", "height": 200.0}).set_placement(
        (xd - 280.0, (oy + z1 + oy + z2) / 2), align=TextEntityAlignment.MIDDLE_CENTER
    )


def _draw_bom_table(msp, model: TowerModel, rows, origin, max_rows: int = 28) -> None:
    from ezdxf.enums import TextEntityAlignment

    bx, by = origin
    col_w = [1100.0, 1300.0, 1500.0, 700.0, 900.0]
    row_h = 380.0
    headers = ["件号", "截面", "长度", "数量", "类别"]

    msp.add_text("构件明细表 BOM", dxfattribs={"layer": "BOM", "height": 350.0}).set_placement(
        (bx, by + 600.0), align=TextEntityAlignment.LEFT
    )

    cx = bx
    for i, h in enumerate(headers):
        msp.add_lwpolyline(
            [(cx, by), (cx + col_w[i], by), (cx + col_w[i], by + row_h), (cx, by + row_h)],
            close=True, dxfattribs={"layer": "BOM"},
        )
        msp.add_text(h, dxfattribs={"layer": "BOM", "height": 220.0}).set_placement(
            (cx + col_w[i] / 2, by + row_h / 2), align=TextEntityAlignment.MIDDLE_CENTER
        )
        cx += col_w[i]

    kind_map = {b[0]: b[4] for b in model.bars}
    for ri, (bid, sec, length, qty) in enumerate(rows[:max_rows]):
        ry = by - (ri + 1) * row_h
        cx = bx
        cells = [bid, sec, str(length), str(qty), kind_map.get(bid, "")]
        for i, cell in enumerate(cells):
            msp.add_lwpolyline(
                [(cx, ry), (cx + col_w[i], ry), (cx + col_w[i], ry + row_h), (cx, ry + row_h)],
                close=True, dxfattribs={"layer": "BOM"},
            )
            msp.add_text(cell, dxfattribs={"layer": "BOM", "height": 180.0}).set_placement(
                (cx + col_w[i] / 2, ry + row_h / 2), align=TextEntityAlignment.MIDDLE_CENTER
            )
            cx += col_w[i]

    if len(rows) > max_rows:
        msp.add_text(
            f"... 共 {len(rows)} 件，完整清单见 tower_110kv_bom.csv",
            dxfattribs={"layer": "BOM", "height": 240.0},
        ).set_placement((bx, by - (max_rows + 1) * row_h - 150.0), align=TextEntityAlignment.LEFT)


def _draw_node_detail_k1(msp, ox: float, oy: float) -> None:
    """节点大样 K1：主材与斜材螺栓连接节点（示意比例 1:5）。"""
    from ezdxf.enums import TextEntityAlignment

    s = 8.0  # 1:5 示意放大
    msp.add_text(
        "节点大样 K1 (1:5) 主材-斜材螺栓节点",
        dxfattribs={"layer": "TEXT", "height": 300.0},
    ).set_placement((ox, oy + 3200.0), align=TextEntityAlignment.LEFT)

    # 节点板
    pw, ph = 400 * s, 350 * s
    msp.add_lwpolyline(
        [(ox, oy), (ox + pw, oy), (ox + pw, oy + ph), (ox, oy + ph)],
        close=True, dxfattribs={"layer": "PLATE"},
    )
    msp.add_text("节点板 δ=10", dxfattribs={"layer": "TEXT", "height": 220.0}).set_placement(
        (ox + pw / 2, oy + ph + 200.0), align=TextEntityAlignment.MIDDLE_CENTER
    )

    # 主材角钢示意（双线）
    leg_w = 160 * s
    msp.add_line((ox - leg_w, oy + ph / 2), (ox + pw + leg_w * 0.3, oy + ph / 2),
                 dxfattribs={"layer": "LEG"})
    msp.add_line((ox - leg_w, oy + ph / 2 + 80), (ox + pw + leg_w * 0.3, oy + ph / 2 + 80),
                 dxfattribs={"layer": "LEG"})
    msp.add_text("L160×12", dxfattribs={"layer": "TEXT", "height": 200.0}).set_placement(
        (ox - leg_w - 200.0, oy + ph / 2 + 40.0), align=TextEntityAlignment.MIDDLE_CENTER
    )

    # 斜材
    msp.add_line((ox + pw * 0.2, oy - leg_w * 0.5), (ox + pw * 0.75, oy + ph + leg_w * 0.3),
                 dxfattribs={"layer": "DIAG"})
    msp.add_text("L75×6", dxfattribs={"layer": "TEXT", "height": 200.0}).set_placement(
        (ox + pw * 0.9, oy + ph + leg_w * 0.5), align=TextEntityAlignment.LEFT
    )

    # 螺栓孔 M20
    bolt_r = 25.0 * s
    holes = [
        (ox + pw * 0.35, oy + ph * 0.35),
        (ox + pw * 0.65, oy + ph * 0.35),
        (ox + pw * 0.35, oy + ph * 0.65),
        (ox + pw * 0.65, oy + ph * 0.65),
    ]
    for hx, hy in holes:
        msp.add_circle((hx, hy), radius=bolt_r, dxfattribs={"layer": "DETAIL"})
    msp.add_text("4-M20 高强螺栓", dxfattribs={"layer": "TEXT", "height": 200.0}).set_placement(
        (ox + pw / 2, oy - 400.0), align=TextEntityAlignment.MIDDLE_CENTER
    )

    # 尺寸
    _dim_h(msp, 0, pw, 0, f"{int(pw / s)}", ox, oy, off=-350.0)
    _dim_v(msp, 0, 0, ph, f"{int(ph / s)}", ox, oy, off=-500.0)
    _dim_h(msp, pw * 0.35, pw * 0.65, ph * 0.35, f"{int((pw * 0.3) / s)}", ox, oy, off=400.0)


def make_real_tower_dxf(path: str | Path, model: Optional[TowerModel] = None) -> str:
    """生成 110kV 猫头塔全套施工图 DXF。"""
    import ezdxf
    from ezdxf.enums import TextEntityAlignment

    model = model or build_110kv_cathead_tower()
    path = str(path)
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM

    msp = doc.modelspace()

    stem = Path(path).stem
    # 图层名与解析器共用 schema/tower_layer_map.json 的规范
    for name in LAYER_COLORS:
        doc.layers.add(name, color=LAYER_COLORS[name])
    for group in ("bar_layers", "node_layers", "dim_layers", "text_layers"):
        for name in spec_layer_names(group, []):
            if name not in doc.layers:
                doc.layers.add(name, color=7)

    # A0 图框
    fx, fy, fw, fh = 2000.0, 2000.0, 116000.0, 82000.0
    msp.add_lwpolyline(
        [(fx, fy), (fx + fw, fy), (fx + fw, fy + fh), (fx, fy + fh)],
        close=True, dxfattribs={"layer": "FRAME"},
    )
    msp.add_text("A0 1:50", dxfattribs={"layer": "TITLE", "height": 280.0}).set_placement(
        (fx + 2000.0, fy + 1500.0), align=TextEntityAlignment.LEFT
    )

    # ---- 正立面 (X-Z, Y=0 投影) ----
    ex, ey = view_origin(stem, "front", (6000.0, 8000.0))
    msp.add_text(
        "正立面 ELEVATION (正面)",
        dxfattribs={"layer": "TEXT", "height": 400.0},
    ).set_placement((ex, ey - 1500.0), align=TextEntityAlignment.LEFT)

    def front_pt(nid: str) -> Tuple[float, float]:
        x, y, z = model.nodes[nid]
        # 正面：取 X-Z，轻微展开 Y 避免完全重叠
        return (ex + x + y * 0.08, ey + z)

    _draw_bars(msp, model, front_pt, label_major_only=True, label_size=280.0)
    _draw_nodes(msp, model, front_pt, radius=40.0, labels=False)

    _dim_h(msp, -2620, 2620, 0, "5240", ex, ey, off=-900.0)
    _dim_v(msp, -2620, 0, 16200, "16200", ex, ey, off=-1400.0)
    _dim_v(msp, -2620, 16200, 21000, "4800", ex, ey, off=-1400.0)
    _dim_h(msp, -6500, 6500, 16200, "13000", ex, ey, off=700.0)
    _dim_v(msp, 2620, 0, 21000, "21000", ex, ey, off=1200.0)

    # 剖切符号
    msp.add_text("1", dxfattribs={"layer": "TEXT", "height": 350.0}).set_placement(
        (ex - 3500.0, ey + 10000.0), align=TextEntityAlignment.MIDDLE_CENTER
    )
    msp.add_line((ex - 4000.0, ey + 10000.0), (ex + 4000.0, ey + 10000.0),
                 dxfattribs={"layer": "DIM"})

    # ---- 侧立面 (Y-Z, X=0 投影) ----
    sx, sy = view_origin(stem, "side", (42000.0, 8000.0))
    msp.add_text(
        "侧立面 ELEVATION (侧面)",
        dxfattribs={"layer": "TEXT", "height": 400.0},
    ).set_placement((sx, sy - 1500.0), align=TextEntityAlignment.LEFT)

    def side_pt(nid: str) -> Tuple[float, float]:
        x, y, z = model.nodes[nid]
        return (sx + y + x * 0.08, sy + z)

    _draw_bars(msp, model, side_pt, label_major_only=True, label_size=90.0)
    _draw_nodes(msp, model, side_pt, radius=35.0)

    _dim_h(msp, -2620, 2620, 0, "5240", sx, sy, off=-900.0)
    _dim_v(msp, 2620, 0, 21000, "21000", sx, sy, off=1200.0)

    # ---- 平面图 Z=0 / 8100 / 16200 ----
    plan_z_levels = [0.0, 8100.0, 16200.0]
    plan_labels = ["PLAN Z=0 基础层", "PLAN Z=8100 塔身", "PLAN Z=16200 横担层"]
    # 平面图原点按 z_level 从规范读取（规范按 z 升序声明）
    plan_specs = sorted((r for r in view_regions(stem) if r.get("kind") == "plan"),
                        key=lambda r: (r.get("z_level") is None, r.get("z_level", 0.0)))
    for pi, (pz, plab) in enumerate(zip(plan_z_levels, plan_labels)):
        if pi < len(plan_specs) and plan_specs[pi].get("origin"):
            px, py = plan_specs[pi]["origin"]
        else:
            px = 6000.0 + pi * 22000.0
            py = 38000.0
        msp.add_text(plab, dxfattribs={"layer": "TEXT", "height": 350.0}).set_placement(
            (px, py - 3500.0), align=TextEntityAlignment.LEFT
        )

        def plan_pt(nid: str, _pz=pz, _px=px, _py=py) -> Tuple[float, float]:
            x, y, z = model.nodes[nid]
            if abs(z - _pz) > 1500.0 and nid[0] != "C" and nid[0] != "H" and nid[0] != "P":
                # 非本层节点投影到该层高（示意）
                pass
            return (_px + x, _py + y)

        # 只画该层附近杆件（简化投影）
        for bid, n1, n2, _, kind in model.bars:
            z1, z2 = model.nodes[n1][2], model.nodes[n2][2]
            if min(z1, z2) <= pz + 1200 and max(z1, z2) >= pz - 1200:
                p1, p2 = plan_pt(n1), plan_pt(n2)
                layer = _kind_layer(kind)
                msp.add_line(p1, p2, dxfattribs={"layer": layer})

        for nid, (x, y, z) in model.nodes.items():
            if abs(z - pz) < 1500.0:
                p = (px + x, py + y)
                msp.add_circle(p, radius=50.0, dxfattribs={"layer": "NODE"})

        _dim_h(msp, -2620, 2620, -2620, "5240", px, py, off=-800.0)

    # ---- 1-1 剖面 ----
    sec_x, sec_y = view_origin(stem, "section", (72000.0, 38000.0))
    msp.add_text("1-1 剖面 SECTION", dxfattribs={"layer": "TEXT", "height": 350.0}).set_placement(
        (sec_x, sec_y - 3500.0), align=TextEntityAlignment.LEFT
    )

    def section_pt(nid: str) -> Tuple[float, float]:
        x, y, z = model.nodes[nid]
        # 剖面沿 Y 方向，显示 X-Z
        return (sec_x + x, sec_y + z)

    _draw_bars(msp, model, section_pt, label_major_only=True, label_size=85.0)
    _dim_v(msp, 2620, 0, 16200, "16200", sec_x, sec_y, off=1000.0)

    # ---- 节点大样 K1 ----
    kx, ky = view_origin(stem, "detail", (72000.0, 8000.0))
    _draw_node_detail_k1(msp, kx, ky)

    # ---- BOM 表（两份分页）----
    bom = model.bom_rows()
    _draw_bom_table(msp, model, bom[:28], (6000.0, 72000.0), max_rows=28)
    _draw_bom_table(msp, model, bom[28:56], (36000.0, 72000.0), max_rows=28)

    # ---- 设计说明 ----
    notes_x, notes_y = 72000.0, 72000.0
    notes = [
        "设计说明:",
        "1. 本图适用于 110kV 单回路猫头型直线塔，呼高 21m。",
        "2. 钢材 Q345B，螺栓 8.8 级高强螺栓，焊缝三级。",
        "3. 主材=L160×12/L125×10/L100×8，水平=L90×8，斜材=L75×6。",
        "4. 横担外伸 6500mm，挂点跨距 11000mm（挂点中心距）。",
        "5. 所有尺寸单位 mm，标高为加工基准。",
        f"6. 全塔共计 {len(model.bars)} 根杆件，{len(model.nodes)} 个节点。",
        "7. 未注焊缝为周围焊，焊高 hf=6。",
    ]
    for i, line in enumerate(notes):
        msp.add_text(line, dxfattribs={"layer": "TEXT", "height": 260.0}).set_placement(
            (notes_x, notes_y - i * 450.0), align=TextEntityAlignment.LEFT
        )

    # ---- 标题栏 ----
    tx, ty = 98000.0, 2500.0
    msp.add_lwpolyline(
        [(tx, ty), (tx + 18000.0, ty), (tx + 18000.0, ty + 6000.0), (tx, ty + 6000.0)],
        close=True, dxfattribs={"layer": "TITLE"},
    )
    title_lines = [
        ("110kV 单回路猫头型输电铁塔施工图", 400.0, 4500.0),
        ("图号: SD11-21-01", 300.0, 3700.0),
        ("比例: 1:50  单位: mm", 280.0, 3000.0),
        ("呼高: 21000  根开: 5240", 280.0, 2300.0),
        ("横担高: 16200  外伸: 6500", 280.0, 1600.0),
        (f"杆件: {len(model.bars)}  节点: {len(model.nodes)}", 260.0, 900.0),
    ]
    for text, h, dy in title_lines:
        msp.add_text(text, dxfattribs={"layer": "TITLE", "height": h}).set_placement(
            (tx + 9000.0, ty + dy), align=TextEntityAlignment.MIDDLE_CENTER
        )

    doc.saveas(path)
    return path


def export_tower_dxf_preview(dxf_path: str | Path, png_path: str | Path, dpi: int = 120) -> str:
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext, config
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    import matplotlib.pyplot as plt

    dxf_path = Path(dxf_path)
    png_path = Path(png_path)
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
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(png_path), bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    return str(png_path)


def make_tower_bom_csv(path: str | Path, model: Optional[TowerModel] = None) -> str:
    model = model or build_110kv_cathead_tower()
    path = Path(path)
    lines = ["bar_id,section,length_mm,qty,kind"]
    for bid, n1, n2, sec, kind in model.bars:
        length = int(round(model.bar_length(n1, n2)))
        lines.append(f"{bid},{sec},{length},1,{kind}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def make_tower_golden_json(path: str | Path, model: Optional[TowerModel] = None) -> str:
    """金标准节点坐标 JSON，供 3D 求解器验收。"""
    import json

    model = model or build_110kv_cathead_tower()
    data = {
        "name": model.name,
        "nodes": {k: list(v) for k, v in model.nodes.items()},
        "bars": [
            {"id": b[0], "from": b[1], "to": b[2], "section": b[3], "kind": b[4]}
            for b in model.bars
        ],
    }
    path = Path(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[2] / "examples"
    out.mkdir(parents=True, exist_ok=True)
    tower = build_110kv_cathead_tower()
    print(f"塔型: {tower.name}")
    print(f"节点: {len(tower.nodes)}, 杆件: {len(tower.bars)}")

    dxf = make_real_tower_dxf(out / "tower_110kv.dxf", tower)
    png = export_tower_dxf_preview(dxf, out / "tower_110kv.png", dpi=200)
    csv = make_tower_bom_csv(out / "tower_110kv_bom.csv", tower)
    golden = make_tower_golden_json(out / "tower_110kv_golden.json", tower)

    from .tower_clear_preview import export_all_clear_views
    clear_dir = out / "clear"
    clear_files = export_all_clear_views(clear_dir, tower)

    print(f"DXF: {dxf}")
    print(f"PNG (全图): {png}")
    print(f"BOM: {csv}")
    print(f"Golden: {golden}")
    print("高清分视图:")
    for f in clear_files:
        print(f"  {f}")
