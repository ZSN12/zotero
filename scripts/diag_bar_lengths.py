#!/usr/bin/env python3
"""诊断杆件长度分布 —— 量化「通长斜材断开」问题。

对比模型 vs GT 的各类杆件（斜/竖/水平）长度分布，判断：
    * 斜材是否被 MLLM 在交叉点断开（模型长度远短于 GT = 断开问题）
    * 主材是否分段过粗/过细

用法：
    python3 scripts/diag_bar_lengths.py <gt.json> <model.json>
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def classify_gt(nodes: dict, bars: list) -> Counter:
    """GT 3D 杆按方向分类并统计长度。"""
    lens = {"竖": [], "水平": [], "斜": []}
    for b in bars:
        f = nodes[b["from"]]
        t = nodes[b["to"]]
        dx, dy, dz = t[0] - f[0], t[1] - f[1], t[2] - f[2]
        L = math.hypot(dx, dy, dz)
        if L < 1:
            continue
        if abs(dz) > 0.7 * L:
            lens["竖"].append(L)
        elif abs(dz) < 0.3 * L:
            lens["水平"].append(L)
        else:
            lens["斜"].append(L)
    return lens


def classify_model(comps: dict) -> dict:
    """模型杆按 3D 坐标分类并统计长度。"""
    nodes = {c["id"]: c["properties"] for c in comps.values() if c.get("kind") == "tower_node"}
    bars = [c["properties"] for c in comps.values() if c.get("kind") == "tower_bar"]
    lens = {"竖": [], "水平": [], "斜": []}
    for b in bars:
        f = nodes.get(b["from_node"], {})
        t = nodes.get(b["to_node"], {})
        x1, y1, z1 = f.get("x"), f.get("y"), f.get("z")
        x2, y2, z2 = t.get("x"), t.get("y"), t.get("z")
        if None in (x1, y1, z1, x2, y2, z2):
            continue
        dx, dy, dz = x2 - x1, y2 - y1, z2 - z1
        L = math.hypot(dx, dy, dz)
        if L < 1:
            continue
        if abs(dz) > 0.7 * L:
            lens["竖"].append(L)
        elif abs(dz) < 0.3 * L:
            lens["水平"].append(L)
        else:
            lens["斜"].append(L)
    return lens


def summarize(lens: list) -> str:
    if not lens:
        return "无"
    s = sorted(lens)
    return f"n={len(s)} 中位={s[len(s)//2]:.0f} max={s[-1]:.0f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gt")
    ap.add_argument("model")
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

    gt_lens = classify_gt(gt["nodes"], gt["bars"])
    model_lens = classify_model(model["components"])

    print("=== 杆件长度分布对比（mm）===")
    print(f"{'类型':<6}{'GT':>28}{'模型':>28}{'诊断':>18}")
    for k in ("斜", "竖", "水平"):
        g = summarize(gt_lens[k])
        m = summarize(model_lens[k])
        # 诊断：模型斜材中位远小于 GT = 断开问题
        diag = ""
        if k == "斜" and gt_lens[k] and model_lens[k]:
            gm = sorted(gt_lens[k])[len(gt_lens[k]) // 2]
            mm = sorted(model_lens[k])[len(model_lens[k]) // 2]
            if mm < gm * 0.5:
                diag = "⚠ 斜材断开"
            elif mm < gm * 0.8:
                diag = "略短"
            else:
                diag = "OK"
        print(f"{k:<6}{g:>28}{m:>28}{diag:>18}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
