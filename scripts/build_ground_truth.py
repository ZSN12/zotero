#!/usr/bin/env python3
"""P0：国网 GIM .mod 解析成果 → 35A1-JC1 Ground Truth JSON。

权威 GT 数据源（非手标，非 BOM）：
    GIM/.../解析成果/35A1-JC1.mod
        P,<node_id>,x,y,z          1707 个节点（mm）
        R,<from>,<to>,<section>,<material>,...  3473 根杆段（细分到每段）

.m 的 R 行是「分段」：同一根物理杆件沿高度被拆成 700-800、800-900… 多段。
本脚本按「端点相接 + 截面相同 + 材质相同」把分段合并回物理杆件，
输出对齐 tower_110kv_golden.json 的 {nodes, bars} 结构：

    nodes: {node_id: [x, y, z]}
    bars:  [{id, from, to, section, material, segments}]

产物：examples/gt/35A1-JC1_ground_truth.json
用法：python3 scripts/build_ground_truth.py [mod路径] [输出路径]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

# 默认 .mod 路径（国网官方资料包）
DEFAULT_MOD = (
    Path.home() / "Downloads"
    / "输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)"
    / "GIM/35A1/35A1-JC1/35A1-JC1-GIM输出/解析成果/35A1-JC1.mod"
)
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "examples/gt/35A1-JC1_ground_truth.json"


def parse_mod(path: Path):
    """解析 .mod：返回 (nodes, segments)。"""
    nodes: dict[int, list[float]] = {}
    segments: list[tuple[int, int, str, str]] = []  # (from, to, section, material)
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("P,"):
            p = line.split(",")
            nodes[int(p[1])] = [float(p[2]), float(p[3]), float(p[4])]
        elif line.startswith("R,"):
            p = line.split(",")
            segments.append((int(p[1]), int(p[2]), p[3], p[4]))
    return nodes, segments


def merge_segments(segments):
    """把「端点相接 + 截面/材质相同」的分段合并回物理杆件。

    返回 bars：[{id, from, to, section, material, segments}]。
    from/to 是合并后链的首尾节点 id；segments 是参与合并的原始段数。
    """
    by_from: dict[int, list] = defaultdict(list)
    for s in segments:
        by_from[s[0]].append(s)

    used: set[int] = set()
    bars = []
    for i, s in enumerate(segments):
        if i in used:
            continue
        chain = [s]
        used.add(i)
        # 向后延伸：找「以当前尾节点为起点、截面/材质相同」的段
        cur = s
        while True:
            nxt = None
            for j, cand in enumerate(segments):
                if j in used:
                    continue
                if cand[0] == cur[1] and cand[2] == s[2] and cand[3] == s[3]:
                    nxt = j
                    break
            if nxt is None:
                break
            cur = segments[nxt]
            used.add(nxt)
            chain.append(cur)

        bars.append({
            "id": f"PM_{len(bars)+1:04d}",   # 物理杆件编号（GT 真值无件号时用）
            "from": str(s[0]),
            "to": str(cur[1]),
            "section": s[2],
            "material": s[3],
            "segments": len(chain),
        })
    return bars


def build(mod_path: Path, out_path: Path) -> dict:
    nodes, segments = parse_mod(mod_path)
    bars = merge_segments(segments)
    gt = {
        "name": "35A1-JC1 国网官方计算模型 (GIM .mod)",
        "source": str(mod_path),
        "nodes": {str(k): v for k, v in nodes.items()},
        "bars": bars,
        "stats": {
            "nodes": len(nodes),
            "segments": len(segments),
            "physical_bars": len(bars),
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(gt, ensure_ascii=False, indent=1), encoding="utf-8")
    return gt


if __name__ == "__main__":
    mod = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MOD
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    if not mod.exists():
        print(f"✗ 找不到 .mod 文件：{mod}", file=sys.stderr)
        sys.exit(1)
    gt = build(mod, out)
    print(f"✓ Ground Truth 已生成 -> {out}")
    print(f"  节点: {gt['stats']['nodes']} | 杆段: {gt['stats']['segments']} | 物理杆件: {gt['stats']['physical_bars']}")
