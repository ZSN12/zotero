#!/usr/bin/env python3
"""DXF → 追溯 SVG 导出。

把交付目录里 _dxf_scope 的 DXF 图纸导成轻量 SVG，专供
web/site/trace.html 的「左 3D / 右图纸」点杆联动使用：

* 全部背景实体按 DXF handle 打 data-h 分组（LINE/LWPOLYLINE/CIRCLE/TEXT/…），
  前景可高亮图层（含 CLE 合成中心线的杆件折线）按 data-g 分组。
* 高亮 overlay：sheet JSON 里每根 tower_bar 的 from/to 节点带 view_x/view_y
  （region×scale 换算后的图纸坐标），翻回图纸 mm 坐标画成折线，
  id = bar 组件 id —— 3D 页点杆后 getElementById 精确点亮。

用法：
  python3 scripts/export_trace_svg.py \
      --deliver out/35A1-JC1-full-deliver --sheets 35A1-JC1-02,35A1-JC1-04,... \
      --out web/demo/35A1-JC1/trace
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import ezdxf


def _fmt(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- SVG 导出

STROKE_BY_LAYER = {
    "0": "#8fa3bd", "1": "#6f86a6", "2": "#5f7896", "3": "#5f7896",
    "4": "#7d92b0", "5": "#55688a", "7": "#8fa3bd", "8": "#55688a",
}


def _ents_of(msp):
    for e in msp:
        yield e


def _line(e, parts, stroke, w):
    s, t = e.dxf.start, e.dxf.end
    parts.append(
        f'<line data-h="{e.dxf.handle}" x1="{_fmt(s.x)}" y1="{_fmt(-s.y)}" '
        f'x2="{_fmt(t.x)}" y2="{_fmt(-t.y)}" stroke="{stroke}" stroke-width="{w}" '
        f'vector-effect="non-scaling-stroke"/>')


def _poly(parts, pts, handle, stroke, w, closed):
    if len(pts) < 2:
        return
    d = "M" + " L".join(f"{_fmt(x)} {_fmt(y)}" for x, y in pts)
    if closed:
        d += " Z"
    parts.append(f'<path data-h="{handle}" d="{d}" fill="none" stroke="{stroke}" '
                 f'stroke-width="{w}" vector-effect="non-scaling-stroke"/>')


def _lwpolyline(e, parts, stroke, w):
    pts = [(p[0], -p[1]) for p in e.get_points("xy")]
    closed = e.closed
    _poly(parts, pts, e.dxf.handle, stroke, w, closed)


def _circle(e, parts, stroke):
    c, r = e.dxf.center, e.dxf.radius
    parts.append(
        f'<circle data-h="{e.dxf.handle}" cx="{_fmt(c.x)}" cy="{_fmt(-c.y)}" '
        f'r="{_fmt(r)}" fill="none" stroke="{stroke}" stroke-width="1" '
        f'vector-effect="non-scaling-stroke"/>')


def _arc(e, parts, stroke):
    c, r = e.dxf.center, e.dxf.radius
    a0, a1 = e.dxf.start_angle, e.dxf.end_angle
    if a1 < a0:
        a1 += 360.0
    large = 1 if (a1 - a0) > 180 else 0
    rad = math.radians
    x0 = c.x + r * math.cos(rad(a0))
    y0 = -c.y - r * math.sin(rad(a0))
    x1 = c.x + r * math.cos(rad(a1))
    y1 = -c.y - r * math.sin(rad(a1))
    parts.append(
        f'<path data-h="{e.dxf.handle}" d="M{_fmt(x0)} {_fmt(y0)} '
        f'A{_fmt(r)} {_fmt(r)} 0 {large} 1 {_fmt(x1)} {_fmt(y1)}" '
        f'fill="none" stroke="{stroke}" stroke-width="1" '
        f'vector-effect="non-scaling-stroke"/>')


def _text(e, parts, fill):
    ins = e.dxf.insert
    h = getattr(e.dxf, "height", 2.5) or 2.5
    txt = (e.dxf.text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if not txt.strip():
        return
    parts.append(
        f'<text data-h="{e.dxf.handle}" x="{_fmt(ins.x)}" y="{_fmt(-ins.y)}" '
        f'font-size="{_fmt(h)}" fill="{fill}">{txt}</text>')


def export_sheet_svg(dxf_path: Path, out_svg: Path, pad: float = 40.0) -> dict:
    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()

    parts: list[str] = []
    minx = miny = math.inf
    maxx = maxy = -math.inf

    def grow(x, y):
        nonlocal minx, miny, maxx, maxy
        minx, miny = min(minx, x), min(miny, y)
        maxx, maxy = max(maxx, x), max(maxy, y)

    for e in _ents_of(msp):
        t = e.dxftype()
        # 网格底色图层（digitized 台账线）淡一点
        stroke = STROKE_BY_LAYER.get(str(e.dxf.layer), "#7d92b0")
        try:
            if t == "LINE":
                _line(e, parts, stroke, 1.1)
                grow(e.dxf.start.x, -e.dxf.start.y); grow(e.dxf.end.x, -e.dxf.end.y)
            elif t == "LWPOLYLINE":
                _lwpolyline(e, parts, stroke, 1.1)
                for px, py in [(p[0], -p[1]) for p in e.get_points("xy")]:
                    grow(px, py)
            elif t == "CIRCLE":
                _circle(e, parts, stroke)
                grow(e.dxf.center.x - e.dxf.radius, -e.dxf.center.y - e.dxf.radius)
                grow(e.dxf.center.x + e.dxf.radius, -e.dxf.center.y + e.dxf.radius)
            elif t == "ARC":
                _arc(e, parts, stroke)
                grow(e.dxf.center.x - e.dxf.radius, -e.dxf.center.y - e.dxf.radius)
                grow(e.dxf.center.x + e.dxf.radius, -e.dxf.center.y + e.dxf.radius)
            elif t == "TEXT":
                _text(e, parts, "#9fb2cc")
                ins = e.dxf.insert
                grow(ins.x, -ins.y); grow(ins.x + 30, -ins.y - 6)
            elif t == "INSERT":
                # 块引用：占位小方块只画不撑盒——块插入点常带图框/符号的
                # 远端坐标，会把 viewBox 撑大几十倍（07 册实测 981 条线
                # 被挤成 20px）。几何以 LINE/LWPOLYLINE 为准。
                ins = e.dxf.insert
                parts.append(
                    f'<rect data-h="{e.dxf.handle}" x="{_fmt(ins.x)}" y="{_fmt(-ins.y)}" '
                    f'width="4" height="4" fill="{stroke}"/>')
            elif t == "DIMENSION":
                # 尺寸标注只贡献包围盒（文字渲染走 TEXT 分支已覆盖大部分）
                try:
                    p = e.dxf.defpoint
                    grow(p.x, -p.y)
                except Exception:
                    pass
        except Exception:
            continue  # 坏实体不出局整张图

    if not math.isfinite(minx):
        raise SystemExit(f"{dxf_path}: modelspace 无可渲染几何")

    minx -= pad; miny -= pad; maxx += pad; maxy += pad
    w, h = maxx - minx, maxy - miny
    body = "".join(parts)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_fmt(minx)} {_fmt(miny)} '
        f'{_fmt(w)} {_fmt(h)}" font-family="ui-monospace,Menlo,monospace">\n'
        f'<g id="bg">{body}</g>\n'
        f'<g id="hl" data-empty="1"></g>\n'
        f"</svg>\n")
    out_svg.parent.mkdir(parents=True, exist_ok=True)
    out_svg.write_text(svg, encoding="utf-8")
    return {"w": round(w), "h": round(h), "entities": len(parts)}


# ------------------------------------------------- 高亮 overlay（3D 杆折线）

def build_overlay(deliver: Path, sheets: list[str]) -> dict:
    """sheet JSON 的 tower_bar → 图纸坐标折线，按 3D 杆 id 双向映射。"""
    overlay = {}   # sheet -> {bar_component_id: {"pts": [[x,y],...], "bar_id": str}}
    for sheet in sheets:
        sj = json.loads((deliver / "sheets" / f"{sheet}.json").read_text(encoding="utf-8"))
        comps = sj["components"]
        nodes = {k: v for k, v in comps.items() if v.get("kind") == "tower_node"}
        out = {}
        for cid, comp in comps.items():
            if comp.get("kind") != "tower_bar":
                continue
            props = comp.get("properties", {})
            fn, tn = props.get("from_node"), props.get("to_node")
            n1, n2 = nodes.get(fn), nodes.get(tn)
            if not n1 or not n2:
                continue
            p1 = n1.get("properties", {})
            p2 = n2.get("properties", {})
            # node 的 x/y 是图纸 mm 绝对坐标（view_* 是 region 局部坐标）
            if None in (p1.get("x"), p1.get("y"), p2.get("x"), p2.get("y")):
                continue
            # handle = projection_refs 里的 DXF 合成中心线 id（CLE%04d），
            # 塔级 model.json 的 projection_refs 用同一 id —— 全链联接键
            refs = props.get("projection_refs") or []
            handle = next((r.get("source_component_id") for r in refs
                           if r.get("source_component_id")), None)
            out[cid] = {"pts": [[p1["x"], -p1["y"]], [p2["x"], -p2["y"]]],
                        "bar_id": props.get("bar_id") or "", "handle": handle}
        overlay[sheet] = out
    return overlay


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deliver", required=True, help="交付目录（含 sheets/ 与 _dxf_scope/）")
    ap.add_argument("--sheets", required=True, help="逗号分隔 sheet id（留空=全部含 DXF 的）")
    ap.add_argument("--out", required=True, help="SVG 输出目录")
    args = ap.parse_args()

    deliver = Path(args.deliver)
    scope = deliver / "_dxf_scope"
    if args.sheets:
        sheets = [s.strip() for s in args.sheets.split(",") if s.strip()]
    else:
        sheets = sorted(p.stem for p in scope.glob("*.dxf"))

    outdir = Path(args.out)
    index = {}
    for sheet in sheets:
        dxf = scope / f"{sheet}.dxf"
        if not dxf.exists():
            print(f"  跳过（无 DXF）: {sheet}")
            continue
        info = export_sheet_svg(dxf, outdir / f"{sheet}.svg")
        index[sheet] = info
        n_bars = len(build_overlay(deliver, [sheet])[sheet])
        print(f"  {sheet}: {info['entities']} 实体 → {outdir / (sheet + '.svg')} (overlay bars={n_bars})")

    overlay = build_overlay(deliver, sheets)
    (outdir / "overlay.json").write_text(
        json.dumps(overlay, ensure_ascii=False), encoding="utf-8")
    (outdir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成：{len(index)} 张 SVG + overlay.json → {outdir}")


if __name__ == "__main__":
    main()
