#!/usr/bin/env python3
"""站点 3D 追溯页 evidence.json 生成器（四级原型解析，可离线重跑）。

输入 out/<tower>-full-deliver/model.json + web/demo/<tower>/trace/overlay.json，
输出 web/demo/<tower>/trace/evidence.json（component_id → 血统 + 原型链接）。

原型解析链（诚实分级，先命中先用）：
  1. direct      直读杆：projection_refs 自带 sheet_id + source_component_id；
  2. mirror      镜像衍生：沿 derived_from/root_bar_id 链（≤3 跳，含 4f_ 前缀/面后缀
                 变体）找到带 refs 的母体杆——卡跳到母体的图元；
  3. side        侧视直读/镜像：source_file 册 + bar_<id>_side overlay 键；
  4. sheet_cover 高程区段：杆件 z 中点落入某分册立面区段窗（该分册覆盖此高程）——
                 只声明「所在高程由图 X 覆盖」，不声明单根图元对应。

产物字段：{role, section, geometry_origin, bar_id, projection_refs, confidence,
is_derived, prototype:{sheet, handle?, pts?, mode}}。
塔头/塔底无演示分册覆盖的区段保持无 prototype（诚实降级，不硬凑）。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TOWERS = {
    "35A1-JC1": {
        "model": "out/35A1-JC1-full-deliver/model.json",
        "overlay": "web/demo/35A1-JC1/trace/overlay.json",
        "examples_overlay": "examples/external/guowang_35A1/layer_overlay.json",
        "out": "web/demo/35A1-JC1/trace/evidence.json",
        "demo_sheets": ["35A1-JC1-02", "35A1-JC1-04", "35A1-JC1-05", "35A1-JC1-06", "35A1-JC1-07"],
    },
    "35A2-ZC1": {
        # 阶段一-b（2026-09-07）：demo 证据源切到 full-deliver 主产物。
        # 旧 phase3-zc1 是 R8 上线前的 1381 杆陈旧快照，与 trace/tower.glb
        # （skeleton.glb 同步副本，448 杆）脱节——页面上点击一半的杆查不到血统。
        "model": "out/35A2-ZC1-full-deliver/model.json",
        "overlay": "web/demo/35A2-ZC1/trace/overlay.json",
        "examples_overlay": "examples/external/guowang_35A2_zc1/layer_overlay.json",
        "out": "web/demo/35A2-ZC1/trace/evidence.json",
        "demo_sheets": ["35A2-ZC1-05", "35A2-ZC1-07", "35A2-ZC1-08", "35A2-ZC1-09",
                        "35A2-ZC1-10", "35A2-ZC1-12"],
    },
}


def load(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sheet_z_windows(tower: str, cfg: dict, overlay) -> dict:
    """每演示分册的立面 z 窗 [z_lo, z_hi]：优先 demo overlay 区段，退回 examples overlay。"""
    wins = {}
    ex = load(REPO / cfg["examples_overlay"]) if (REPO / cfg["examples_overlay"]).exists() else {}
    vr = ex.get("view_regions") or {}
    for sid in cfg["demo_sheets"]:
        r = (vr.get(sid) or [None])[0]
        if r and r.get("z_offset") is not None and r.get("z_span_mm"):
            wins[sid] = (float(r["z_offset"]), float(r["z_offset"]) + float(r["z_span_mm"]))
            continue
        # demo overlay 条目内嵌 region_id 兜底
        for o in (overlay.get(sid) or {}).values():
            rid = o.get("region_id") if isinstance(o, dict) else None
            if rid and rid.get("z_offset") is not None and rid.get("z_span_mm"):
                wins[sid] = (float(rid["z_offset"]), float(rid["z_offset"]) + float(rid["z_span_mm"]))
                break
    return wins


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tower", choices=sorted(TOWERS), required=True)
    args = ap.parse_args()
    cfg = TOWERS[args.tower]

    model = load(REPO / cfg["model"])
    overlay = load(REPO / cfg["overlay"])
    comps = model["components"]
    bars = {cid: c["properties"] for cid, c in comps.items()
            if isinstance(c, dict) and c.get("kind") == "tower_bar"}
    nodes = {cid: c["properties"] for cid, c in comps.items()
             if isinstance(c, dict) and c.get("kind") == "tower_node"}
    wins = sheet_z_windows(args.tower, cfg, overlay)

    # overlay 键索引：sheet → key 集合；bar_id → [(sheet, key, entry)]
    side_index: dict[str, list] = {}
    for sid, entries in overlay.items():
        for key, entry in entries.items():
            m = re.match(r"^bar_(.+?)_(side|front)(_\d+)?$", key)
            if m:
                side_index.setdefault(m.group(1), []).append((sid, key, entry))

    def mother_with_refs(props, max_hops=3):
        """沿 derived_from/root_bar_id 链找带 refs 的母体（含 4f_ 变体）。"""
        df = props.get("derived_from") or props.get("root_bar_id")
        seen = set()
        for _ in range(max_hops):
            if not df or df in seen:
                return None
            seen.add(df)
            hit = None
            for cand in (df, f"4f_{df}", f"4f_{df}_F", f"4f_{df}_B", f"4f_{df}_L", f"4f_{df}_R"):
                if cand in bars and cand != df.split("__", 1)[-1] or cand in bars:
                    hit = cand
                    break
            if not hit:
                return None
            mp = bars[hit]
            if mp.get("projection_refs"):
                return mp
            df = mp.get("derived_from") or mp.get("root_bar_id")
        return None

    evidence = {}
    stat = {"direct": 0, "mirror": 0, "side": 0, "sheet_cover": 0, "none": 0}
    for cid, p in bars.items():
        refs = p.get("projection_refs") or []
        ref0 = next((r for r in refs if r.get("sheet_id")), None)
        proto = None
        if ref0:
            proto = {"sheet": ref0["sheet_id"], "handle": ref0.get("source_component_id"),
                     "mode": "direct"}
            stat["direct"] += 1
        else:
            mp = mother_with_refs(p)
            if mp:
                r0 = next(r for r in mp["projection_refs"] if r.get("sheet_id"))
                proto = {"sheet": r0["sheet_id"], "handle": r0.get("source_component_id"),
                         "mode": "mirror"}
                stat["mirror"] += 1
        if proto is None and p.get("source_file") and p.get("bar_id"):
            cands = side_index.get(str(p["bar_id"]), [])
            for sid, key, entry in cands:
                if sid == p.get("source_file") and "_side" in key:
                    proto = {"sheet": sid, "handle": entry.get("handle"),
                             "pts": entry.get("pts"), "mode": "side"}
                    stat["side"] += 1
                    break
        if proto is None:
            n1, n2 = nodes.get(p.get("from_node")) or {}, nodes.get(p.get("to_node")) or {}
            z1, z2 = n1.get("z"), n2.get("z")
            if z1 is not None and z2 is not None:
                zc = (float(z1) + float(z2)) / 2.0
                sid = next((s for s, (a, b) in sorted(wins.items()) if a <= zc <= b), None)
                if sid:
                    proto = {"sheet": sid, "mode": "sheet_cover"}
                    stat["sheet_cover"] += 1
        if proto is None:
            stat["none"] += 1
        evidence[cid] = {
            "role": p.get("role"),
            "section": p.get("section"),
            "geometry_origin": p.get("geometry_origin"),
            "bar_id": p.get("bar_id"),
            "projection_refs": refs,
            "confidence": p.get("confidence"),
            "is_derived": not refs,
            **({"prototype": proto} if proto else {}),
        }

    out = REPO / cfg["out"]
    out.write_text(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"{args.tower}: bars={len(evidence)} " +
          " ".join(f"{k}={v}" for k, v in stat.items()) + f" → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
