#!/usr/bin/env python3
"""阶段2.2 region 拆分审计：量化每段声明 region 对结构线的覆盖与多栏布局。

归因（2026-08-30）：hybrid 生产路径（tower_agent_pipeline / hybrid_dxf_agent）
用 overlay view_regions 的 bbox **裁剪图片喂给 MLLM**——MLLM 只看到 region bbox
内的像素。region bbox 若太窄，立面右半/材料表之外的真实杆件 MLLM 根本看不到，
直接造成 A2 二维召回偏低（基线 R@500=2.8%）。

本脚本对每段（DXF + overlay）报告：
    1. 结构图层 LINE 在声明 region 内/外的根数与覆盖率；
    2. 结构线 x 直方图（用于识别「立面单栏 vs 多栏 + 材料表/标题栏」布局）；
    3. 覆盖不足（<80%）的段列为待拆分候选。

用法：
    python3 scripts/audit_regions.py [--dxf-dir out/xianyu-acceptance/batch-jc1/dxf]
                                    [--overlay examples/external/guowang_35A1/layer_overlay.json]
                                    [--stems 35A1-JC1-06 ...]
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import ezdxf


def _load_overlay(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def audit_sheet(stem: str, dxf_dir: str, overlay: dict) -> dict:
    """返回单个 sheet 的 region 覆盖审计报告。"""
    fp = os.path.join(dxf_dir, f"{stem}.dxf")
    if not os.path.exists(fp):
        return {"stem": stem, "error": "DXF 缺失"}
    doc = ezdxf.readfile(fp)
    msp = doc.modelspace()
    bar_layers = set(overlay.get("bar_layers_by_stem", {}).get(stem, []))
    regions = overlay.get("view_regions", {}).get(stem, [])

    # 结构线收集
    segs = []
    for e in msp:
        if e.dxftype() != "LINE":
            continue
        if bar_layers and e.dxf.layer not in bar_layers:
            continue
        segs.append((e.dxf.start.x, e.dxf.start.y, e.dxf.end.x, e.dxf.end.y))

    total = len(segs)
    front = [r for r in regions if r.get("kind") in ("front", "elevation") and r.get("region")]
    if not front:
        return {"stem": stem, "total_lines": total, "regions": len(regions),
                "front_regions": 0, "note": "无 front 区域声明"}

    # 覆盖率（按段中点）+ 区域外分桶 y 跨度（簇感知）
    covered = 0
    outside_buckets: dict = {}
    rx0, rx1, ry0, ry1 = front[0]["region"]
    reg_h = ry1 - ry0
    for x0, y0, x1, y1 in segs:
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if rx0 <= mx <= rx1 and ry0 <= my <= ry1:
            covered += 1
            continue
        b = round(mx // 50) * 50
        s = outside_buckets.setdefault(b, [1e18, -1e18, 0])
        s[0] = min(s[0], y0, y1)
        s[1] = max(s[1], y0, y1)
        s[2] += 1

    # 簇感知：区域外「全高」桶（y 跨度 >= 70% region 高）——立面被裁或全高表格，
    # 须目检甄别；局部桶是节点大样/材料表局部框线，region 排除它是正确行为。
    full_height = {b: s for b, s in outside_buckets.items() if s[1] - s[0] >= 0.7 * reg_h}
    partial = {b: s for b, s in outside_buckets.items() if s[1] - s[0] < 0.7 * reg_h}
    fh_lines = sum(s[2] for s in full_height.values())

    # x 直方图（每 50 单位）
    hist = Counter()
    for x0, _y0, x1, _y1 in segs:
        hist[round(((x0 + x1) / 2) // 50) * 50] += 1

    # 声明 region 与实际结构线 bbox 的对照
    xs = [c for s in segs for c in (s[0], s[2])]
    ys = [c for s in segs for c in (s[1], s[3])]
    actual_bbox = (min(xs), max(xs), min(ys), max(ys))
    decl_bbox = tuple(front[0]["region"]) if front else None

    return {
        "stem": stem,
        "total_lines": total,
        "covered": covered,
        "coverage": round(covered / total, 3) if total else 0.0,
        "declared_bbox": decl_bbox,
        "actual_bbox": actual_bbox,
        "x_histogram_50": dict(sorted(hist.items())),
        # 簇感知指标
        "outside_full_height_lines": fh_lines,
        "outside_full_height_buckets": sorted(full_height),
        "outside_partial_lines": sum(s[2] for s in partial.values()),
        # 区域外有全高内容才需要处理（立面被裁或全高表格，目检甄别）
        "needs_split": fh_lines > 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dxf-dir", default="out/xianyu-acceptance/batch-jc1/dxf")
    ap.add_argument("--overlay", default="examples/external/guowang_35A1/layer_overlay.json")
    ap.add_argument("--stems", nargs="*", default=None)
    args = ap.parse_args()

    overlay = _load_overlay(args.overlay)
    stems = args.stems or [
        "35A1-JC1-40", "35A1-JC1-07", "35A1-JC1-06",
        "35A1-JC1-05", "35A1-JC1-04", "35A1-JC1-02",
    ]
    print("=== 阶段2.2 region 拆分审计 ===")
    print(f"DXF 目录: {args.dxf_dir}")
    print(f"overlay: {args.overlay}\n")
    for stem in stems:
        rep = audit_sheet(stem, args.dxf_dir, overlay)
        if "error" in rep:
            print(f"{stem}: {rep['error']}")
            continue
        flag = ("  ← 区域外有全高内容(立面被裁或全高表格,须目检)"
                if rep["needs_split"] else "  ✓ 区域外无全高内容")
        print(f"{stem}: 结构线 {rep['total_lines']} 根, region 覆盖 "
              f"{rep['covered']}/{rep['total_lines']} ({rep['coverage']:.0%}){flag}")
        print(f"   声明 bbox {rep['declared_bbox']}")
        print(f"   区域外全高簇 {rep['outside_full_height_lines']} 根 "
              f"(桶{rep['outside_full_height_buckets']}) | "
              f"局部簇(大样/表) {rep['outside_partial_lines']} 根")
        print(f"   x 直方图(50桶) {rep['x_histogram_50']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
