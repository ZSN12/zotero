#!/usr/bin/env python3
"""阶段2.2 region 拆分权威判据：按连通分量分类每段图面内容。

桶级 y-span 指标会把「竖直堆叠的多个局部大样」误判成全高立面（02 段实测：
三个节点大样各 46-53 单位高，叠在相邻 x 桶里合成 337 单位 y-span → 假「全高簇」）。
本脚本改用端点吸附 union-find 做连通分量，每个分量单独看 y-span 与规模，
把图面内容分成：立面(front/side) / 节点大样 / 材料表，并与 overlay view_regions
声明对照，输出每段的 region 修正建议（可直接落到 layer_overlay.json）。

用法：
    python3 scripts/classify_sheet_views.py [--dxf-dir ...] [--overlay ...] [--stems ...]
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict

import ezdxf


class UF:
    def __init__(self):
        self.p = {}

    def f(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def u(self, a, b):
        self.p[self.f(a)] = self.f(b)


def _load_overlay(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def components_of(segs, tol=8.0):
    """端点吸附 union-find：返回 {root: [seg_idx...]}。"""
    uf = UF()
    grid = defaultdict(list)
    for i, ((x0, y0), (x1, y1)) in enumerate(segs):
        for pt in ((x0, y0), (x1, y1)):
            grid[(round(pt[0] / tol), round(pt[1] / tol))].append((i, pt))
    for cell, items in grid.items():
        cx, cy = cell
        near = [(i, p) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for i, p in grid.get((cx + dx, cy + dy), [])]
        for a, (i, pa) in enumerate(items):
            for j, pb in near[a + 1:]:
                if i != j and abs(pa[0] - pb[0]) <= tol and abs(pa[1] - pb[1]) <= tol:
                    uf.u(i, j)
    comp = defaultdict(list)
    for i in range(len(segs)):
        comp[uf.f(i)].append(i)
    return comp


def classify_sheet(stem: str, dxf_dir: str, overlay: dict, tol=8.0) -> dict:
    fp = os.path.join(dxf_dir, f"{stem}.dxf")
    if not os.path.exists(fp):
        return {"stem": stem, "error": "DXF 缺失"}
    doc = ezdxf.readfile(fp)
    msp = doc.modelspace()
    bar_layers = set(overlay.get("bar_layers_by_stem", {}).get(stem, []))
    regions = overlay.get("view_regions", {}).get(stem, [])

    segs = []
    for e in msp:
        if e.dxftype() != "LINE":
            continue
        if bar_layers and e.dxf.layer not in bar_layers:
            continue
        segs.append(((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)))

    comps = components_of(segs, tol=tol)
    total = len(segs)

    # 每个分量：bbox + 规模 + y-span（图面单位，*scale 得 mm）
    scale = 20.0
    for r in regions:
        if r.get("kind") in ("front", "side", "elevation"):
            scale = float(r.get("scale_y", r.get("scale_x", 20.0)))
            break
    infos = []
    for root, idxs in comps.items():
        if len(idxs) < 8:
            continue  # 忽略零散短线
        xs = [c for i in idxs for c in (segs[i][0][0], segs[i][1][0])]
        ys = [c for i in idxs for c in (segs[i][0][1], segs[i][1][1])]
        bbox = (min(xs), max(xs), min(ys), max(ys))
        h_mm = (bbox[3] - bbox[2]) * scale
        w_mm = (bbox[2 - 2] - bbox[0]) * scale if False else (bbox[1] - bbox[0]) * scale
        infos.append({
            "n": len(idxs),
            "bbox": bbox,
            "w_mm": round(w_mm),
            "h_mm": round(h_mm),
            "kind": _classify_kind(bbox, w_mm, h_mm, regions),
        })
    infos.sort(key=lambda x: -x["n"])
    return {"stem": stem, "total_lines": total, "scale": scale, "components": infos,
            "elevation_regions": [r["region"] for r in regions
                                  if r.get("kind") in ("front", "side", "elevation")]}


def _classify_kind(bbox, w_mm, h_mm, regions):
    """粗略分类：全高且窄→立面；局部→大样；全高且宽(>3000mm)且矩形网格→材料表。"""
    # 全高 = 与立面 region 等高（±30%）
    elev_h = None
    for r in regions:
        if r.get("kind") in ("front", "side", "elevation"):
            elev_h = (r["region"][3] - r["region"][2]) * 20.0
            break
    if elev_h and h_mm >= 0.7 * elev_h:
        if w_mm >= 3000:
            return "table/wide"
        return "elevation"
    return "detail"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dxf-dir", default="out/xianyu-acceptance/batch-jc1/dxf")
    ap.add_argument("--overlay", default="examples/external/guowang_35A1/layer_overlay.json")
    ap.add_argument("--stems", nargs="*", default=None)
    ap.add_argument("--tol", type=float, default=8.0)
    args = ap.parse_args()

    overlay = _load_overlay(args.overlay)
    stems = args.stems or [
        "35A1-JC1-40", "35A1-JC1-07", "35A1-JC1-06",
        "35A1-JC1-05", "35A1-JC1-04", "35A1-JC1-02",
    ]
    print(f"=== 阶段2.2 连通分量分类 (TOL={args.tol}) ===")
    for stem in stems:
        rep = classify_sheet(stem, args.dxf_dir, overlay, tol=args.tol)
        if "error" in rep:
            print(f"{stem}: {rep['error']}")
            continue
        print(f"\n{stem}: 结构线 {rep['total_lines']} 根, 立面 region {rep['elevation_regions']}")
        for c in rep["components"]:
            x0, x1, y0, y1 = c["bbox"]
            print(f"  [{c['kind']:>10}] n={c['n']:3d} "
                  f"x[{x0:.0f},{x1:.0f}] y[{y0:.0f},{y1:.0f}] "
                  f"{c['w_mm']:5d}mm × {c['h_mm']:5d}mm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
