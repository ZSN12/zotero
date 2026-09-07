#!/usr/bin/env python3
"""阶段一 3D→2D 追溯联动页的数据导出器（零框架、可离线重跑）。

产出三组轻量资产，全部落在 web/demo/35A2-ZC1/traceview/：

  trace_bars.json    全部 1381 根 tower_bar 的骨架线（端点坐标）+ 血统字段
                     （source_file / face / geometry_origin / bar_id / section /
                      derived_from / root_bar_id / 2D seed 线段索引）。
  dxf_lines/<sheet>.json  每张分段立面 DXF 的 LINE/LWPOLYLINE/LWPOLYLINE 弧段
                     离散后的 2D 线段表（layer 分组，前端按需 fetch 渲染 canvas）。
  smoke_trace_view.json   自检结果（计数 + 抽样断言），供 scripts 冒烟复核。

坐标约定（与既有 trace/ SVG 生成一致）：
  * 2D 视口 y = -DXF y（图纸立面朝上）。
  * 杆件 2D seed 线段直接取 trace/overlay.json 的 pts（已经过提取器坐标系
    对齐，和 DXF 线段表可共域渲染）。
  * 3D 节点坐标直接取 model.json tower_node 的 x/y/z（Z-up，前端旋转到 Y-up）。

用法：
  python scripts/export_trace_view.py [--model out/phase3-zc1/model.json] \
      [--dxf-dir out/phase3-zc1/_dxf_scope] [--overlay web/demo/35A2-ZC1/trace/overlay.json] \
      [--out web/demo/35A2-ZC1/traceview]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 血统字段白名单：只导出前端展示需要的键，model.json 8.8MB 不整体进前端。
BAR_FIELDS = (
    "bar_id", "source_file", "face", "geometry_origin", "role",
    "derived_from", "root_bar_id", "section", "layer",
    "from_node", "to_node", "length_mm_3d", "corner_leg",
)

FACE_CN = {"f": "正立面", "b": "背立面", "l": "左立面", "r": "右立面"}


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def _flatten_pts(poly) -> list[list[float]]:
    return [[round(p[0], 3), round(p[1], 3)] for p in poly]


def export_dxf_sheet(dxf_path: Path) -> dict:
    """一张 DXF → {layers: {layer: [seg, ...]}, bbox: [...]}. seg = [x1,y1,x2,y2]."""
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()
    layers: dict[str, list] = {}
    minx = miny = math.inf
    maxx = maxy = -math.inf

    def add(layer: str, pts: list[list[float]]) -> None:
        nonlocal minx, miny, maxx, maxy
        if len(pts) < 2:
            return
        segs = layers.setdefault(layer, [])
        for a, b in zip(pts, pts[1:]):
            segs.append([a[0], a[1], b[0], b[1]])
            minx = min(minx, a[0], b[0]); maxx = max(maxx, a[0], b[0])
            miny = min(miny, a[1], b[1]); maxy = max(maxy, a[1], b[1])

    for e in msp:
        t = e.dxftype()
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            add(e.dxf.layer, [[s.x, s.y], [en.x, en.y]])
        elif t == "LWPOLYLINE":
            pts = _flatten_pts(e.get_points("xy"))
            if e.closed:
                pts = pts + [pts[0]]
            add(e.dxf.layer, pts)
    return {
        "layers": layers,
        "bbox": [round(minx, 2), round(miny, 2), round(maxx, 2), round(maxy, 2)],
        "y_flip": True,  # 前端渲染时 y 取负（与 trace SVG 一致）
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="out/phase3-zc1/model.json")
    ap.add_argument("--dxf-dir", default="out/phase3-zc1/_dxf_scope")
    ap.add_argument("--overlay", default="web/demo/35A2-ZC1/trace/overlay.json")
    ap.add_argument("--inventory", default="out/phase3-zc1/bar_inventory.json")
    ap.add_argument("--out", default="web/demo/35A2-ZC1/traceview")
    args = ap.parse_args()

    model_path = REPO / args.model
    dxf_dir = REPO / args.dxf_dir
    overlay_path = REPO / args.overlay
    out_dir = REPO / args.out

    model = load_json(model_path)
    overlay = load_json(overlay_path)
    inventory = load_json(args.inventory) if (REPO / args.inventory).exists() else None

    # bar_id → section（来自料单清点，杆级 properties.section 全塔为 null）
    section_by_bar = {}
    for entry in (inventory or {}).get("entries", []):
        if entry.get("sections"):
            section_by_bar[entry["bar_id"]] = entry["sections"][0]

    # ---------- 1) 节点表 ----------
    nodes = {}
    for cid, comp in model["components"].items():
        if isinstance(comp, dict) and comp.get("kind") == "tower_node":
            p = comp.get("properties") or {}
            if all(k in p for k in ("x", "y", "z")):
                nodes[cid] = [
                    round(float(p["x"]), 2),
                    round(float(p["y"]), 2),
                    round(float(p["z"]), 2),
                ]

    # ---------- 2) overlay 句柄索引（sheet, handle → seed 线段）----------
    ov_by_handle: dict[tuple, dict] = {}
    ov_by_key: dict[tuple, dict] = {}
    for sid, entries in overlay.items():
        for key, v in entries.items():
            if not (isinstance(v, dict) and v.get("pts")):
                continue
            seed = {"key": key, "pts": v["pts"]}
            if v.get("handle"):
                ov_by_handle.setdefault((sid, v["handle"]), seed)
            ov_by_key.setdefault((sid, key), seed)

    # ---------- 3) 杆件表 ----------
    bars = {}
    for cid, comp in model["components"].items():
        if not (isinstance(comp, dict) and comp.get("kind") == "tower_bar"):
            continue
        p = comp.get("properties") or {}
        fn, tn = p.get("from_node"), p.get("to_node")
        if fn not in nodes or tn not in nodes:
            continue
        bar = {k: p.get(k) for k in BAR_FIELDS}
        if bar.get("bar_id") and not bar.get("section"):
            bar["section"] = section_by_bar.get(bar["bar_id"])
        bar["p1"] = nodes[fn]
        bar["p2"] = nodes[tn]

        # 2D seed 解析链：projection_refs.handle → root_bar_id → source_file+face 兜底
        seed = None
        for ref in p.get("projection_refs") or []:
            sid, h = ref.get("sheet_id"), ref.get("source_component_id")
            if sid and h and (sid, h) in ov_by_handle:
                seed = {"sheet": sid, **ov_by_handle[(sid, h)]}
                break
        if seed is None and p.get("root_bar_id") and "__" in p["root_bar_id"]:
            sheet, key = p["root_bar_id"].split("__", 1)
            hit = ov_by_key.get((sheet, key))
            if hit:
                seed = {"sheet": sheet, **hit}
        if seed is None and p.get("source_file"):
            sid = p["source_file"]
            bar_id = p.get("bar_id")
            face = (p.get("face") or "f")
            # face f/b 的 seed 在 <sheet>__front；l/r 在 side（该塔侧立面）
            for key in (f"bar_{bar_id}_front", f"bar_{bar_id}_side"):
                hit = ov_by_key.get((sid, key))
                if hit:
                    seed = {"sheet": sid, **hit, "via": "fallback"}
                    break
        if seed is not None:
            bar["seed"] = seed
        bars[cid] = bar

    # ---------- 4) DXF 线段表 ----------
    dxf_files = sorted(dxf_dir.glob("*.dxf"))
    index = {"sheets": {}, "counts": {}}
    for dxf in dxf_files:
        sheet = dxf.stem
        data = export_dxf_sheet(dxf)
        save_json(out_dir / "dxf_lines" / f"{sheet}.json", data)
        seg_total = sum(len(v) for v in data["layers"].values())
        index["sheets"][sheet] = {
            "bbox": data["bbox"],
            "segments": seg_total,
            "layers": {k: len(v) for k, v in data["layers"].items()},
        }
        print(f"  {sheet}: layers={len(data['layers'])} segments={seg_total}")

    # ---------- 5) 汇总输出 ----------
    traced = sum(1 for b in bars.values() if b.get("seed"))
    save_json(out_dir / "trace_bars.json", {"nodes": nodes, "bars": bars})
    index["counts"] = {
        "nodes": len(nodes),
        "bars": len(bars),
        "bars_with_seed": traced,
        "bars_without_seed": len(bars) - traced,
        "sheets": len(dxf_files),
    }
    save_json(out_dir / "index.json", index)

    # ---------- 6) 冒烟自检 ----------
    sample = next(iter(bars.values()))
    assert sample["p1"] and sample["p2"], "杆件端点缺失"
    with_seed = [b for b in bars.values() if b.get("seed")]
    assert with_seed, "无任何杆件解析出 2D seed"
    assert all(len(b["seed"]["pts"]) >= 2 for b in with_seed[:50]), "seed 线段点数异常"
    # 直读杆与衍生杆都必须在表里
    origins = {b["geometry_origin"] for b in bars.values()}
    assert "dxf_geom" in origins and "derived_4face" in origins, "几何来源覆盖不全"
    smoke = {
        "ok": True,
        "counts": index["counts"],
        "origin_coverage": len(origins),
        "sample_cid": next(iter(bars)),
        "sample_seed": with_seed[0].get("seed"),
    }
    save_json(out_dir / "smoke_trace_view.json", smoke)
    print(
        f"导出完成: bars={len(bars)} nodes={len(nodes)} "
        f"with_seed={traced}/{len(bars)} → {out_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
