"""高清分视图导出 — 解决 A0 全图挤在一起、316 件编号糊成一团的问题。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .tower_real_dxf import (
    TowerModel,
    build_110kv_cathead_tower,
    _kind_layer,
)


def _setup_chinese_font() -> None:
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    candidates = [
        "PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS",
        "Noto Sans CJK SC", "SimHei",
    ]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return

Vec2 = Tuple[float, float]

# 杆件类别 → 颜色 / 线宽（屏幕输出）
STYLE: Dict[str, Tuple[str, float]] = {
    "LEG": ("#B71C1C", 2.8),       # 主材 深红粗线
    "HORIZ": ("#1B5E20", 1.2),    # 水平
    "DIAG": ("#1565C0", 1.0),     # 斜材
    "CROSS": ("#6A1B9A", 2.4),    # 横担
    "HEAD": ("#00838F", 1.6),
    "KNEE": ("#E65100", 1.8),
    "OTHER": ("#455A64", 0.9),
}

LABEL_KINDS = frozenset({"LEG", "CROSS", "HEAD_LEG", "KNEE"})


def _style_for_kind(kind: str) -> Tuple[str, float]:
    layer = _kind_layer(kind)
    if layer in STYLE:
        return STYLE[layer]
    if kind.startswith("HEAD"):
        return STYLE["HEAD"]
    return STYLE["OTHER"]


def _front_xy(model: TowerModel, nid: str) -> Vec2:
    x, y, z = model.nodes[nid]
    return x + y * 0.06, z


def _side_yz(model: TowerModel, nid: str) -> Vec2:
    x, y, z = model.nodes[nid]
    return y + x * 0.06, z


def _plan_xy(model: TowerModel, nid: str) -> Vec2:
    x, y, _ = model.nodes[nid]
    return x, y


def _draw_tower_2d(
    ax,
    model: TowerModel,
    pt_fn: Callable[[str], Vec2],
    show_labels: bool = True,
    z_filter: Optional[Tuple[float, float]] = None,
) -> None:
    """在 matplotlib axes 上绘制塔架 2D 投影。"""
    for bid, n1, n2, sec, kind in model.bars:
        if z_filter is not None:
            z1, z2 = model.nodes[n1][2], model.nodes[n2][2]
            lo, hi = z_filter
            if max(z1, z2) < lo - 800 or min(z1, z2) > hi + 800:
                continue
        x1, y1 = pt_fn(n1)
        x2, y2 = pt_fn(n2)
        color, lw = _style_for_kind(kind)
        ax.plot([x1, x2], [y1, y2], color=color, linewidth=lw, solid_capstyle="round")

        if show_labels and kind in LABEL_KINDS:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            ax.text(
                mx, my, bid,
                fontsize=7, ha="center", va="center",
                color="#111", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85),
            )


def _add_dim_h(ax, x1, x2, y, label, offset=-1200):
    yd = y + offset
    ax.plot([x1, x1], [y, yd], color="#0277BD", linewidth=1.0)
    ax.plot([x2, x2], [y, yd], color="#0277BD", linewidth=1.0)
    ax.plot([x1, x2], [yd, yd], color="#0277BD", linewidth=1.0)
    ax.text((x1 + x2) / 2, yd + (80 if offset < 0 else -120), label,
            ha="center", va="bottom" if offset < 0 else "top",
            fontsize=14, color="#01579B", fontweight="bold")


def _add_dim_v(ax, x, y1, y2, label, offset=-1800):
    xd = x + offset
    ax.plot([x, xd], [y1, y1], color="#0277BD", linewidth=1.0)
    ax.plot([x, xd], [y2, y2], color="#0277BD", linewidth=1.0)
    ax.plot([xd, xd], [y1, y2], color="#0277BD", linewidth=1.0)
    ax.text(xd - 200, (y1 + y2) / 2, label, ha="right", va="center",
            fontsize=14, color="#01579B", fontweight="bold", rotation=90)


def _add_legend(ax) -> None:
    from matplotlib.lines import Line2D
    items = [
        Line2D([0], [0], color=STYLE["LEG"][0], lw=3, label="主材 LEG"),
        Line2D([0], [0], color=STYLE["HORIZ"][0], lw=2, label="水平 HORIZ"),
        Line2D([0], [0], color=STYLE["DIAG"][0], lw=1.5, label="斜材 DIAG"),
        Line2D([0], [0], color=STYLE["CROSS"][0], lw=3, label="横担 CROSS"),
        Line2D([0], [0], color=STYLE["HEAD"][0], lw=2, label="塔头 HEAD"),
    ]
    ax.legend(handles=items, loc="upper right", fontsize=11, framealpha=0.95)


def export_front_hd(model: TowerModel, path: Path, dpi: int = 300) -> str:
    import matplotlib.pyplot as plt

    _setup_chinese_font()

    _setup_chinese_font()

    fig, ax = plt.subplots(figsize=(14, 20), dpi=dpi)
    fig.patch.set_facecolor("white")
    _draw_tower_2d(ax, model, lambda n: _front_xy(model, n), show_labels=True)

    _add_dim_h(ax, -2620, 2620, 0, "5240", offset=-1400)
    _add_dim_v(ax, -2620, 0, 16200, "16200", offset=-2200)
    _add_dim_v(ax, -2620, 16200, 21000, "4800", offset=-2200)
    _add_dim_h(ax, -6500, 6500, 16200, "13000", offset=900)
    _add_dim_v(ax, 3000, 0, 21000, "21000", offset=1600)

    ax.set_title(
        "110kV 猫头塔 — 正立面 (1:50)\n主材/横担编号，斜材按颜色区分",
        fontsize=16, fontweight="bold", pad=16,
    )
    ax.set_xlabel("mm", fontsize=12)
    ax.set_ylabel("标高 mm", fontsize=12)
    ax.set_aspect("equal")
    ax.grid(True, linestyle=":", alpha=0.35)
    _add_legend(ax)
    ax.margins(0.12)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return str(path)


def export_side_hd(model: TowerModel, path: Path, dpi: int = 300) -> str:
    import matplotlib.pyplot as plt

    _setup_chinese_font()

    fig, ax = plt.subplots(figsize=(14, 20), dpi=dpi)
    _draw_tower_2d(ax, model, lambda n: _side_yz(model, n), show_labels=True)
    _add_dim_h(ax, -2620, 2620, 0, "5240", offset=-1400)
    _add_dim_v(ax, 3000, 0, 21000, "21000", offset=1600)
    ax.set_title("110kV 猫头塔 — 侧立面 (1:50)", fontsize=16, fontweight="bold")
    ax.set_aspect("equal")
    ax.grid(True, linestyle=":", alpha=0.35)
    _add_legend(ax)
    ax.margins(0.12)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return str(path)


def export_plan_hd(
    model: TowerModel, path: Path, z_level: float, title: str, dpi: int = 300,
) -> str:
    import matplotlib.pyplot as plt

    _setup_chinese_font()

    fig, ax = plt.subplots(figsize=(12, 12), dpi=dpi)
    _draw_tower_2d(
        ax, model, lambda n: _plan_xy(model, n),
        show_labels=False,
        z_filter=(z_level - 1500, z_level + 1500),
    )
    _add_dim_h(ax, -2620, 2620, -2620, "5240", offset=-800)
    ax.set_title(title, fontsize=15, fontweight="bold")
    ax.set_aspect("equal")
    ax.grid(True, linestyle=":", alpha=0.35)
    _add_legend(ax)
    ax.margins(0.15)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return str(path)


def export_bom_hd(model: TowerModel, path: Path, dpi: int = 200) -> str:
    """BOM 分页大图，每页 40 行。"""
    import matplotlib.pyplot as plt

    _setup_chinese_font()
    rows = model.bom_rows()
    kind_map = {b[0]: b[4] for b in model.bars}
    pages = [rows[i:i + 40] for i in range(0, len(rows), 40)]

    fig, axes = plt.subplots(len(pages), 1, figsize=(16, 11 * len(pages)), dpi=dpi)
    if len(pages) == 1:
        axes = [axes]

    for ax, page in zip(axes, pages):
        ax.axis("off")
        table_data = [
            [bid, sec, str(length), str(qty), kind_map.get(bid, "")]
            for bid, sec, length, qty in page
        ]
        table = ax.table(
            cellText=table_data,
            colLabels=["件号", "截面", "长度mm", "数量", "类别"],
            loc="center",
            cellLoc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1.0, 1.8)
        ax.set_title(
            f"构件明细表 BOM（共 {len(rows)} 件）",
            fontsize=18, fontweight="bold", pad=20,
        )

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return str(path)


def export_node_detail_hd(path: Path, dpi: int = 300) -> str:
    """节点大样 K1 矢量重绘。"""
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Rectangle

    _setup_chinese_font()

    s = 1.0
    fig, ax = plt.subplots(figsize=(12, 10), dpi=dpi)
    pw, ph = 400, 350

    ax.add_patch(Rectangle((0, 0), pw, ph, fill=False, edgecolor="#333", linewidth=2))
    ax.plot([-160, pw + 50], [ph / 2, ph / 2], color="#B71C1C", linewidth=4, label="L160×12 主材")
    ax.plot([-160, pw + 50], [ph / 2 + 16, ph / 2 + 16], color="#B71C1C", linewidth=4)
    ax.plot([pw * 0.2, pw * 0.75], [-80, ph + 40], color="#1565C0", linewidth=2.5, label="L75×6 斜材")

    for hx, hy in [(pw * 0.35, ph * 0.35), (pw * 0.65, ph * 0.35),
                   (pw * 0.35, ph * 0.65), (pw * 0.65, ph * 0.65)]:
        ax.add_patch(Circle((hx, hy), 12, fill=False, edgecolor="#111", linewidth=1.5))

    ax.text(pw / 2, ph + 60, "节点板 δ=10", ha="center", fontsize=14, fontweight="bold")
    ax.text(pw / 2, -120, "4-M20 高强螺栓", ha="center", fontsize=13)
    _add_dim_h(ax, 0, pw, 0, "400", offset=-50)
    _add_dim_v(ax, 0, 0, ph, "350", offset=-60)
    _add_dim_h(ax, pw * 0.35, pw * 0.65, ph * 0.35, "120", offset=40)

    ax.set_title("节点大样 K1 (1:5)  主材-斜材螺栓连接", fontsize=16, fontweight="bold")
    ax.set_aspect("equal")
    ax.legend(loc="upper right", fontsize=11)
    ax.set_xlim(-250, pw + 100)
    ax.set_ylim(-200, ph + 120)
    ax.axis("off")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return str(path)


def export_all_clear_views(out_dir: Path, model: Optional[TowerModel] = None) -> List[str]:
    """导出全套高清分视图。"""
    model = model or build_110kv_cathead_tower()
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        export_front_hd(model, out_dir / "tower_front_hd.png"),
        export_side_hd(model, out_dir / "tower_side_hd.png"),
        export_plan_hd(model, out_dir / "tower_plan_z0_hd.png", 0, "平面图 Z=0 基础层 (1:50)"),
        export_plan_hd(model, out_dir / "tower_plan_z8100_hd.png", 8100, "平面图 Z=8100 塔身 (1:50)"),
        export_plan_hd(model, out_dir / "tower_plan_z16200_hd.png", 16200, "平面图 Z=16200 横担层 (1:50)"),
        export_node_detail_hd(out_dir / "tower_node_k1_hd.png"),
        export_bom_hd(model, out_dir / "tower_bom_hd.png"),
    ]
    return paths


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[2] / "examples" / "clear"
    files = export_all_clear_views(out)
    for f in files:
        print(f)
