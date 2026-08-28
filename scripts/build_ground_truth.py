#!/usr/bin/env python3
"""P0：国网 GIM .mod + 计算文件 .NODE → 35A1-JC1 Ground Truth JSON。

权威 GT 数据源（非手标，非 BOM）：
    GIM/.../解析成果/35A1-JC1.mod
        P,<node_id>,x,y,z          节点（mm）
        R,<from>,<to>,<section>,<material>,...  杆段（细分到每段）
    计算文件/35A/35A1/35A1-JC1/35A1-JC1.NODE
        <node_id> x y z ...        标准 30m 呼高单座独立塔的节点清单（米制）

默认用 .NODE 节点 ID 把 .mod 的杆件裁剪成「标准 30m 呼高单座独立铁塔」，
剔除 .mod 中其余 7 种呼高（9m~27m，Body1/2 + Leg1-7）重叠进来的冲突杆件，
避免导出后画面呈现为多塔叠加的"鸟巢"。若 .NODE 缺失，则回退为全塔(8 塔重叠)。

.m 的 R 行是「分段」：同一根物理杆件沿高度被拆成 700-800、800-900… 多段。
本脚本按「端点相接 + 截面相同 + 材质相同」把分段合并回物理杆件，
输出对齐 tower_110kv_golden.json 的 {nodes, bars} 结构：

    nodes: {node_id: [x, y, z]}
    bars:  [{id, from, to, section, material, segments}]

产物：examples/gt/35A1-JC1_ground_truth.json
用法：python3 scripts/build_ground_truth.py [mod路径] [输出路径] [.NODE路径]
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
# 单塔提纯依据：计算文件里该型号「标准 30m 呼高」独立塔的节点清单（官方 .NODE）。
# .mod 把 8 种呼高（9m~30m，Body1/2 + Leg1-8）的全部节点/杆件重叠在同一坐标空间，
# 用本 .NODE 的节点 ID 集合把 GIM 杆件过滤成单座独立铁塔，剔除其余 7 种呼高的冲突杆件。
DEFAULT_NODE = (
    Path.home() / "Downloads"
    / "输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)"
    / "计算文件/35A/35A1/35A1-JC1/35A1-JC1.NODE"
)
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "examples/gt/35A1-JC1_ground_truth.json"


def parse_mod(path: Path):
    """解析 .mod：返回 (nodes, segments)。

    nodes: dict[int, list[float]]，mm 坐标。
    segments: list[(from, to, section, material)]。
    """
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


def parse_node_file(path: Path) -> set[int]:
    """解析计算文件 .NODE，返回该座独立铁塔的节点 ID 集合。

    .NODE 每行：<node_id> <x> <y> <z> ...（米制，仅用于挑选节点 ID，
    坐标仍以 .mod 的 mm 坐标系为准，避免两套坐标换算误差）。
    """
    ids: set[int] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 4:
            try:
                ids.add(int(parts[0]))
            except ValueError:
                continue
    return ids


def filter_to_single_tower(nodes, segments, keep_ids: set[int]):
    """把重叠的 8 塔模型裁剪成单座独立塔（仅保留 keep_ids 节点 + 两端都在其中的杆件）。"""
    nodes = {k: v for k, v in nodes.items() if k in keep_ids}
    segments = [s for s in segments if s[0] in keep_ids and s[1] in keep_ids]
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


def drop_orphan_nodes(nodes: dict, bars: list) -> tuple[dict, int]:
    """剔除没有任何杆件引用的孤立节点（拓扑可信：节点必须落在某根杆件端点）。

    返回 (裁剪后的 nodes, 剔除的节点数)。仅清理节点，不改动 bars 拓扑。
    """
    used = {str(b["from"]) for b in bars} | {str(b["to"]) for b in bars}
    dropped = [str(k) for k in nodes if str(k) not in used]
    nodes = {k: v for k, v in nodes.items() if str(k) in used}
    return nodes, len(dropped)


def build(mod_path: Path, out_path: Path, node_file: Path | None = None) -> dict:
    nodes, segments = parse_mod(mod_path)
    single_tower = False
    if node_file and node_file.exists():
        keep = parse_node_file(node_file)
        nodes, segments = filter_to_single_tower(nodes, segments, keep)
        single_tower = True
    bars = merge_segments(segments)
    nodes, dropped_nodes = drop_orphan_nodes(nodes, bars)
    name = "35A1-JC1 国网官方计算模型 (GIM .mod)"
    if single_tower:
        name = "35A1-JC1 标准 30m 呼高单座独立铁塔 (计算文件 .NODE + GIM .mod)"
    gt = {
        "name": name,
        "source": str(mod_path),
        "nodes": {str(k): v for k, v in nodes.items()},
        "bars": bars,
        "stats": {
            "nodes": len(nodes),
            "segments": len(segments),
            "physical_bars": len(bars),
            "single_tower_30m": single_tower,
            "dropped_orphan_nodes": dropped_nodes,
        },
    }
    if single_tower:
        gt["stats"]["node_file"] = str(node_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(gt, ensure_ascii=False, indent=1), encoding="utf-8")
    return gt


if __name__ == "__main__":
    mod = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MOD
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    node_file = Path(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_NODE
    if not mod.exists():
        print(f"✗ 找不到 .mod 文件：{mod}", file=sys.stderr)
        sys.exit(1)
    gt = build(mod, out, node_file)
    label = "单塔" if gt["stats"].get("single_tower_30m") else "全塔(8 塔重叠)"
    print(f"✓ Ground Truth 已生成 -> {out}（{label}）")
    print(f"  节点: {gt['stats']['nodes']} | 杆段: {gt['stats']['segments']} | 物理杆件: {gt['stats']['physical_bars']}"
          f" | 剔除孤立节点: {gt['stats'].get('dropped_orphan_nodes', 0)}")
