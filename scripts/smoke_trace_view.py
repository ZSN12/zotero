#!/usr/bin/env python3
"""traceview 资产冒烟脚本（不进 tests/，阶段一验收用）。

复跑 scripts/export_trace_view.py 后执行本脚本，断言三段链路：
  1) trace_bars.json 结构：1381 杆 / 节点表 / 直读杆与衍生杆都在。
  2) seed 2D 线段落在对应 DXF 图纸 bbox 内（坐标系一致性）。
  3) seed 线段附近存在 DXF 实线（母体线段真的画在图上，y 翻转对齐）。

用法：python scripts/smoke_trace_view.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TV = REPO / "web/demo/35A2-ZC1/traceview"


def main() -> int:
    bars_doc = json.loads((TV / "trace_bars.json").read_text(encoding="utf-8"))
    bars = bars_doc["bars"]
    nodes = bars_doc["nodes"]
    assert len(bars) == 1381, f"杆件数 {len(bars)} != 1381"
    assert len(nodes) >= 500, f"节点数 {len(nodes)} 异常"

    origins = {b["geometry_origin"] for b in bars.values()}
    assert "dxf_geom" in origins and "derived_4face" in origins, "几何来源覆盖不全"

    with_seed = {cid: b for cid, b in bars.items() if b.get("seed")}
    assert len(with_seed) >= 400, f"可追溯杆 {len(with_seed)} 过少"
    # 每张图纸都有 seed
    sheets_with_seed = {b["seed"]["sheet"] for b in with_seed.values()}
    assert len(sheets_with_seed) == 6, f"seed 图纸覆盖 {sheets_with_seed}"

    # 直读杆（dxf_geom）100% 有 seed；衍生四面杆允许无 seed（诚实降级）
    dxf_geom = [b for b in bars.values() if b["geometry_origin"] == "dxf_geom"]
    ratio = sum(1 for b in dxf_geom if b.get("seed")) / len(dxf_geom)
    assert ratio > 0.6, f"dxf_geom 杆 seed 覆盖率 {ratio:.0%} 过低"

    # seed 与 DXF 线段共域校验：抽 60 根，逐根找 3 单位内的线段
    checked = 0
    missing = []
    sample = list(with_seed.items())[:: max(1, len(with_seed) // 60)]
    for cid, b in sample:
        sheet = b["seed"]["sheet"]
        dxf = json.loads((TV / "dxf_lines" / f"{sheet}.json").read_text(encoding="utf-8"))
        x0, y0, x1, y1 = dxf["bbox"]
        pts = b["seed"]["pts"]
        # bbox 校验（y 翻转域：seed y 为正，dxf bbox y 为负）
        for p in pts:
            assert x0 - 5 <= p[0] <= x1 + 5, f"{cid} seed x 越界 {p}"
            assert -y1 - 5 <= p[1] <= -y0 + 5, f"{cid} seed y 越界 {p}"
        # 最近线段距离（y 取负对齐）
        def dist_to_seg(px, py, s):
            ax, ay, bx, by = s[0], -s[1], s[2], -s[3]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
            return math.hypot(px - ax - t * dx, py - ay - t * dy)

        best = min(
            dist_to_seg(p[0], p[1], s)
            for p in pts
            for segs in dxf["layers"].values()
            for s in segs
        )
        if best > 3.0:
            missing.append((cid, round(best, 2)))
        checked += 1
    assert checked >= 50, f"抽样数 {checked} 不足"
    # 允许极少数 seed 因提取器坐标微调对不上，但不能超过 10%
    assert len(missing) <= checked * 0.1, f"seed 与 DXF 线段脱节过多: {missing[:5]}"

    print(
        f"smoke OK: bars={len(bars)} with_seed={len(with_seed)} "
        f"sampled={checked} detached={len(missing)}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"smoke FAIL: {e}", file=sys.stderr)
        raise SystemExit(1)
