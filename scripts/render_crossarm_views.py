"""S10 横担桁架模板补全——专项渲染（塔头区域放大 + 全塔全景 + GT 对照）。"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt

REPO = Path(__file__).resolve().parent.parent
MODEL = REPO / "out/35A1-JC1-full-deliver/model.json"
GT = REPO / "examples/gt/35A1-JC1_ground_truth.json"
OUT = REPO / "out/35A1-JC1-full-deliver/crossarm_render.png"


def load_model_bars():
    m = json.loads(MODEL.read_text(encoding="utf-8"))
    comps = m["components"]
    nodes = {cid: c["properties"] for cid, c in comps.items() if c.get("kind") == "tower_node"}
    out = []
    for c in comps.values():
        if c.get("kind") != "tower_bar":
            continue
        p = c["properties"]
        fn, tn = nodes.get(p.get("from_node")), nodes.get(p.get("to_node"))
        if not fn or not tn:
            continue
        if fn.get("z") is None or tn.get("z") is None:
            continue
        out.append((
            (fn["x"], fn["y"], fn["z"]), (tn["x"], tn["y"], tn["z"]),
            p.get("geometry_origin") or p.get("role") or "DIAG",
        ))
    return out


def load_gt_bars():
    g = json.loads(GT.read_text(encoding="utf-8"))
    nodes = g["nodes"]
    out = []
    for b in g["bars"]:
        fn, tn = nodes.get(b["from"]), nodes.get(b["to"])
        if fn and tn:
            out.append((tuple(fn), tuple(tn), b.get("id")))
    return out


def main():
    model_bars = load_model_bars()
    gt_bars = load_gt_bars()

    zmin_arm, zmax_arm = 28000.0, 34000.0

    def in_zone(s, t, lo=zmin_arm, hi=zmax_arm):
        return lo <= s[2] <= hi and lo <= t[2] <= hi

    fig = plt.figure(figsize=(22, 14), dpi=170)

    # ---- 1) 塔头区 3D 等轴测（模型 + 新模板横担高亮） ----
    ax = fig.add_subplot(2, 3, 1, projection="3d")
    ax.set_title("塔头区 3D（红=新模板横担，蓝=既有模型杆）", fontsize=12, fontweight="bold")
    for s, t, tag in model_bars:
        if not in_zone(s, t):
            continue
        is_xarm = tag == "crossarm_truss_completion"
        c = "#e63946" if is_xarm else "#1d3557"
        lw = 2.2 if is_xarm else 0.8
        alpha = 0.95 if is_xarm else 0.45
        ax.plot([s[0], t[0]], [s[1], t[1]], [s[2], t[2]], color=c, linewidth=lw, alpha=alpha)
    ax.view_init(elev=18, azim=40)
    ax.set_zlim(zmin_arm, zmax_arm + 3000)
    ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_zlabel("Z")

    # ---- 2) 塔头区 3D 等轴测（GT 对照） ----
    ax = fig.add_subplot(2, 3, 2, projection="3d")
    ax.set_title("塔头区 3D（GT ground truth 对照）", fontsize=12, fontweight="bold")
    for s, t, _bid in gt_bars:
        if not in_zone(s, t):
            continue
        ax.plot([s[0], t[0]], [s[1], t[1]], [s[2], t[2]], color="#2a9d8f", linewidth=0.9, alpha=0.6)
    ax.view_init(elev=18, azim=40)
    ax.set_zlim(zmin_arm, zmax_arm + 3000)
    ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_zlabel("Z")

    # ---- 3) 正立面 X-Z 塔头区：模型 vs GT 叠加 ----
    ax = fig.add_subplot(2, 3, 3)
    ax.set_title("正立面 X-Z（绿=GT，红=新模板横担，浅蓝=模型）", fontsize=12, fontweight="bold")
    for s, t, tag in model_bars:
        if not in_zone(s, t):
            continue
        is_xarm = tag == "crossarm_truss_completion"
        c = "#e63946" if is_xarm else "#a8dadc"
        lw = 2.0 if is_xarm else 0.7
        alpha = 0.95 if is_xarm else 0.5
        ax.plot([s[0], t[0]], [s[2], t[2]], color=c, linewidth=lw, alpha=alpha)
    for s, t, _bid in gt_bars:
        if not in_zone(s, t):
            continue
        ax.plot([s[0], t[0]], [s[2], t[2]], color="#2a9d8f", linewidth=1.1, alpha=0.85)
    ax.set_xlabel("X (mm)"); ax.set_ylabel("Z (mm)")
    ax.set_xlim(-2600, 2600)
    ax.set_ylim(zmin_arm, zmax_arm + 3000)
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, linestyle="--", alpha=0.3)

    # ---- 4) 侧立面 Y-Z 塔头区 ----
    ax = fig.add_subplot(2, 3, 4)
    ax.set_title("侧立面 Y-Z（绿=GT，红=新模板横担，浅蓝=模型）", fontsize=12, fontweight="bold")
    for s, t, tag in model_bars:
        if not in_zone(s, t):
            continue
        is_xarm = tag == "crossarm_truss_completion"
        c = "#e63946" if is_xarm else "#a8dadc"
        lw = 2.0 if is_xarm else 0.7
        alpha = 0.95 if is_xarm else 0.5
        ax.plot([s[1], t[1]], [s[2], t[2]], color=c, linewidth=lw, alpha=alpha)
    for s, t, _bid in gt_bars:
        if not in_zone(s, t):
            continue
        ax.plot([s[1], t[1]], [s[2], t[2]], color="#2a9d8f", linewidth=1.1, alpha=0.85)
    ax.set_xlabel("Y (mm)"); ax.set_ylabel("Z (mm)")
    ax.set_ylim(zmin_arm, zmax_arm + 3000)
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, linestyle="--", alpha=0.3)

    # ---- 5) 全塔等轴测（新模板横担高亮） ----
    ax = fig.add_subplot(2, 3, 5, projection="3d")
    ax.set_title("全塔 3D 全景（红=新模板横担）", fontsize=12, fontweight="bold")
    for s, t, tag in model_bars:
        is_xarm = tag == "crossarm_truss_completion"
        c = "#e63946" if is_xarm else "#1d3557"
        lw = 2.4 if is_xarm else 0.55
        alpha = 0.95 if is_xarm else 0.4
        ax.plot([s[0], t[0]], [s[1], t[1]], [s[2], t[2]], color=c, linewidth=lw, alpha=alpha)
    ax.view_init(elev=15, azim=40)
    ax.set_zlim(0, 37000)
    ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_zlabel("Z")

    # ---- 6) 正立面全塔（新模板横担高亮） ----
    ax = fig.add_subplot(2, 3, 6)
    ax.set_title("正立面全塔（红=新模板横担）", fontsize=12, fontweight="bold")
    for s, t, tag in model_bars:
        is_xarm = tag == "crossarm_truss_completion"
        c = "#e63946" if is_xarm else "#1d3557"
        lw = 2.0 if is_xarm else 0.5
        alpha = 0.95 if is_xarm else 0.4
        ax.plot([s[0], t[0]], [s[2], t[2]], color=c, linewidth=lw, alpha=alpha)
    ax.set_xlabel("X (mm)"); ax.set_ylabel("Z (mm)")
    ax.set_ylim(0, 37000)
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, linestyle="--", alpha=0.3)

    plt.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT, dpi=170, bbox_inches="tight")
    n_xarm = sum(1 for s, t, tag in model_bars if tag == "crossarm_truss_completion")
    print(f"✓ 横担专项渲染: {OUT}（模板杆 {n_xarm} 根高亮）")


if __name__ == "__main__":
    main()
