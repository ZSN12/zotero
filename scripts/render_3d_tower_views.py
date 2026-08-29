"""3D 铁塔模型多视角高清渲染器（用于直观检视 3D 空间结构形态）。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
import numpy as np

REPO = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO / "out/35A1-JC1-full-deliver/model.json"
OUT_IMG = REPO / "out/35A1-JC1-full-deliver/tower_3d_inspection.png"


def main():
    model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    comps = model["components"]
    nodes = {cid: c["properties"] for cid, c in comps.items() if c.get("kind") == "tower_node"}
    bars = [c["properties"] for c in comps.values() if c.get("kind") == "tower_bar"]

    # 提取 3D 线段
    segments = []
    for b in bars:
        fn, tn = nodes.get(b.get("from_node")), nodes.get(b.get("to_node"))
        if not fn or not tn:
            continue
        # 优先 3D 坐标 x/y/z，其次 view 坐标
        p1 = [fn.get("x", 0.0), fn.get("y", 0.0), fn.get("z", 0.0)]
        p2 = [tn.get("x", 0.0), tn.get("y", 0.0), tn.get("z", 0.0)]
        if p1[2] is None or p2[2] is None:
            continue
        role = b.get("role", "DIAG")
        if b.get("corner_leg"):
            role = "LEG"
        elif b.get("diaphragm"):
            role = "HORIZ"
        segments.append((p1, p2, role))

    print(f"有效 3D 杆件绘制数: {len(segments)}")

    fig = plt.figure(figsize=(20, 10), dpi=200)

    # 颜色配置
    colors = {
        "LEG": "#e63946",       # 鲜红（四角主立柱）
        "DIAG": "#1d3557",      # 深蓝/青蓝（四面斜腹杆）
        "HORIZ": "#457b9d",     # 浅蓝（水平横隔框）
        "CROSS": "#f4a261",     # 金橙（横担悬臂）
    }

    # 视角 1: 3D 等轴测全景透视
    ax1 = fig.add_subplot(1, 3, 1, projection="3d")
    ax1.set_title("35A1-JC1 3D 等轴测全景 (Isometric 3D)", fontsize=13, fontweight="bold", pad=10)
    for p1, p2, role in segments:
        c = colors.get(role, "#2a9d8f")
        lw = 1.6 if role == "LEG" else 1.0 if role == "CROSS" else 0.7
        alpha = 0.9 if role == "LEG" else 0.65
        ax1.plot([p1[0], p2[0]], [p1[1], p2[1]], [p1[2], p2[2]], color=c, linewidth=lw, alpha=alpha)

    ax1.view_init(elev=20, azim=45)
    ax1.set_xlabel("X (mm)")
    ax1.set_ylabel("Y (mm)")
    ax1.set_zlabel("Z (mm)")
    ax1.set_zlim(0, 37000)

    # 视角 2: 正立面 (Front Elevation X-Z)
    ax2 = fig.add_subplot(1, 3, 2)
    ax2.set_title("正立面 (Front View X-Z)", fontsize=13, fontweight="bold", pad=10)
    for p1, p2, role in segments:
        c = colors.get(role, "#2a9d8f")
        lw = 1.4 if role == "LEG" else 0.6
        alpha = 0.85 if role == "LEG" else 0.55
        ax2.plot([p1[0], p2[0]], [p1[2], p2[2]], color=c, linewidth=lw, alpha=alpha)
    ax2.set_xlabel("X (mm)")
    ax2.set_ylabel("Z (mm)")
    ax2.set_ylim(0, 37000)
    ax2.set_aspect("equal", adjustable="datalim")
    ax2.grid(True, linestyle="--", alpha=0.3)

    # 视角 3: 侧立面 (Side Elevation Y-Z)
    ax3 = fig.add_subplot(1, 3, 3)
    ax3.set_title("侧立面 (Side View Y-Z)", fontsize=13, fontweight="bold", pad=10)
    for p1, p2, role in segments:
        c = colors.get(role, "#2a9d8f")
        lw = 1.4 if role == "LEG" else 0.6
        alpha = 0.85 if role == "LEG" else 0.55
        ax3.plot([p1[1], p2[1]], [p1[2], p2[2]], color=c, linewidth=lw, alpha=alpha)
    ax3.set_xlabel("Y (mm)")
    ax3.set_ylabel("Z (mm)")
    ax3.set_ylim(0, 37000)
    ax3.set_aspect("equal", adjustable="datalim")
    ax3.grid(True, linestyle="--", alpha=0.3)

    plt.tight_layout()
    OUT_IMG.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_IMG, dpi=200, bbox_inches="tight")
    print(f"✓ 3D 铁塔渲染图已生成: {OUT_IMG}")


if __name__ == "__main__":
    main()
