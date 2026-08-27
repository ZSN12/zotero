#!/usr/bin/env python3
"""P1 坐标对齐：用 GT 反投影自动标定图纸视图的 scale/origin。

背景：国网立面图（如 35A1-JC1-02）横向(塔宽)与竖向(塔高)比例不同，
且一张图含 front+side+详图多簇，region 声明边界常与实际线范围出入。
手工写 scale_ratio 会差 8 倍以上。

方法：GT 的 front 投影是「底宽顶窄」梯形（底宽/顶宽比 ≈ 13.8）。在图纸上
按 x 间隙切分各簇，逐簇算「底/顶宽比 + 梯形方向」，找出与 GT 匹配的簇，
再按底部腿宽 / 塔高 / 中心点 标定 scale_x、scale_z、origin。

用法：
    python3 scripts/calibrate_view.py <dxf> <gt.json> [--bar-layers 1,4]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ezdxf


def collect_segments(dxf_path: str, bar_layers) -> list:
    """收集 bar 图层的线段（含 INSERT 展开）。"""
    from traceability.intake.tower_dxf import _flatten_modelspace_entities, _layer_hit
    doc = ezdxf.readfile(dxf_path)
    segs = []
    for e in _flatten_modelspace_entities(doc.modelspace()):
        layer = getattr(e.dxf, "layer", "0")
        if not _layer_hit(layer, bar_layers):
            continue
        if e.dxftype() == "LINE":
            segs.append((e.dxf.start.x, e.dxf.start.y, e.dxf.end.x, e.dxf.end.y))
        elif e.dxftype() == "LWPOLYLINE":
            try:
                pts = list(e.get_points("xy"))
            except Exception:
                continue
            for i in range(len(pts) - 1):
                segs.append((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]))
    return segs


def split_by_x_gap(segs, gap: float = 15.0) -> list:
    """按线段中点 x 的间隙切分成簇。"""
    mids = sorted((s[0] + s[2]) / 2 for s in segs)
    clusters = []
    cur = [mids[0]]
    for i in range(1, len(mids)):
        if mids[i] - mids[i - 1] > gap:
            clusters.append(cur)
            cur = [mids[i]]
        else:
            cur.append(mids[i])
    clusters.append(cur)
    return clusters


def trapezoid_shape(segs, band: float = 40.0):
    """算一簇线段的梯形特征：底宽/顶宽/高/底顶比。

    底 = y 最大端（图下方），顶 = y 最小端（图上方）。
    返回 (w_bottom, w_top, height, ratio) 或 None。
    """
    xs = [c for s in segs for c in (s[0], s[2])]
    ys = [c for s in segs for c in (s[1], s[3])]
    if not xs:
        return None
    ymax, ymin = max(ys), min(ys)
    bot = [x for s in segs for x in (s[0], s[2]) if max(s[1], s[3]) >= ymax - band]
    top = [x for s in segs for x in (s[0], s[2]) if min(s[1], s[3]) <= ymin + band]
    if not bot or not top:
        return None
    w_b = max(bot) - min(bot)
    w_t = max(top) - min(top)
    h = ymax - ymin
    ratio = w_b / w_t if w_t > 0 else 0.0
    return w_b, w_t, h, ratio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dxf")
    ap.add_argument("gt")
    ap.add_argument("--bar-layers", default="1,4")
    args = ap.parse_args()

    layers = args.bar_layers.split(",")
    segs = collect_segments(args.dxf, layers)
    clusters = split_by_x_gap(segs)

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    nodes = gt["nodes"]
    gx = [v[0] for v in nodes.values()]
    gz = [v[2] for v in nodes.values()]
    gt_w = max(gx) - min(gx)
    gt_h = max(gz) - min(gz)
    # GT front 梯形比值（底宽/顶宽）
    bot = [v[0] for v in nodes.values() if v[2] < 1000]
    top = [v[0] for v in nodes.values() if v[2] > gt_h - 1000]
    gt_ratio = (max(bot) - min(bot)) / (max(top) - min(top))

    print(f"GT front: 宽 {gt_w:.0f}mm 高 {gt_h:.0f}mm 底/顶比 {gt_ratio:.2f}")
    print(f"图纸 {args.dxf} 共 {len(segs)} 段线，{len(clusters)} 个 x 簇：\n")

    for i, cl in enumerate(clusters):
        cl_segs = [s for s in segs if (min(cl) <= (s[0] + s[2]) / 2 <= max(cl))]
        shape = trapezoid_shape(cl_segs)
        if shape is None:
            continue
        w_b, w_t, h, ratio = shape
        # 找与 GT 最匹配的簇（方向正确 + 比值接近）
        match = "front?" if (ratio > 1.5 and abs(math.log(ratio / gt_ratio)) < 1.0) else ""
        sx = gt_w / w_b if w_b else 0
        sz = gt_h / h if h else 0
        print(f"簇{i}: x[{min(cl):.0f},{max(cl):.0f}] 底宽{w_b:.0f} 顶宽{w_t:.0f} "
              f"高{h:.0f} 比{ratio:.2f} {match}")
        if match:
            cx = (min(cl) + max(cl)) / 2
            print(f"  => scale_x={sx:.1f} scale_z={sz:.1f} origin_x={cx:.1f} origin_y(底)={max(s[1] for s in cl_segs):.1f}")


if __name__ == "__main__":
    main()
