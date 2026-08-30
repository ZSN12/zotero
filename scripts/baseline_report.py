#!/usr/bin/env python3
"""S0 基线报告：冻结当前模型状态的全部可量化指标。

产出（每次实验必须输出，否则实验无效）：
    * 总杆数 / physical 杆数 / front 可评测杆数
    * 主腿 / 横隔 / 斜材 数量
    * 斜材长度分布（<0.5m / 0.5-2m / 2-5m / 5-6m / >6m）
    * >6m 超长杆数（按类别）
    * degree-1 节点数（模型拓扑，来自 drawing_file 组件属性）
    * A2 TP / Recall（分 tol）
    * 各类别 TP / Recall（leg / diagonal / horizontal）
    * 各段 TP / Recall（source_file 维度）
    * 横隔 z 水平数与 GT 18 平台对比

用法：
    python3 scripts/baseline_report.py \
        examples/gt/35A1-JC1_ground_truth.json \
        out/35A1-JC1-full-deliver/model.json \
        [--view front] [--out out/baseline.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from traceability.eval.metrics import (
    DEFAULT_TOLS,
    eval_a2_geometry_2d,
    eval_segment_pr,
    bars_from_model_2d,
    segment_cost,
)


def _role(p: dict) -> str:
    r = str(p.get("role") or "").upper()
    return r


def _is_diaphragm(p: dict) -> bool:
    return bool(p.get("diaphragm")) or str(p.get("face") or "").lower() == "diaphragm"


def _seg_len_2d(seg) -> float:
    return math.hypot(seg[2] - seg[0], seg[3] - seg[1])


def _classify_2d(seg) -> str:
    dx = abs(seg[2] - seg[0])
    dy = abs(seg[3] - seg[1])
    L = math.hypot(dx, dy)
    if L < 1e-6:
        return "degenerate"
    incl = math.degrees(math.atan2(dy, dx))  # 0=水平, 90=竖直
    if incl >= 70.0:
        return "leg"
    if incl <= 20.0:
        return "horizontal"
    return "diagonal"


def model_stats(model: dict) -> dict:
    comps = model.get("components", {})
    bars = [c for c in comps.values() if c.get("kind") == "tower_bar"]
    nodes = [c for c in comps.values() if c.get("kind") == "tower_node"]

    total_bars = len(bars)

    def phys(p):
        from traceability.eval.metrics import is_physical_bar
        return is_physical_bar(p)

    physical_bars = [b for b in bars if phys(b.get("properties", {}))]

    # 杆件语义分类
    role_count = Counter()
    dia_count = 0
    for b in bars:
        p = b.get("properties", {})
        role_count[_role(p)] += 1
        if _is_diaphragm(p):
            dia_count += 1

    # front 可评测杆数
    front_bars = bars_from_model_2d(model, view="front", mode="physical")

    # 斜材长度分布（front 投影）
    diag_lens = []
    for seg, p in front_bars:
        if _classify_2d(seg) == "diagonal":
            diag_lens.append(_seg_len_2d(seg))

    def bucket(L):
        if L < 500:
            return "<0.5m"
        if L < 2000:
            return "0.5-2m"
        if L < 5000:
            return "2-5m"
        if L < 6000:
            return "5-6m"
        return ">6m"

    len_dist = Counter()
    for L in diag_lens:
        len_dist[bucket(L)] += 1

    # >6m 超长杆（按类别）
    overlong = {"diagonal": 0, "leg": 0, "horizontal": 0}
    for seg, p in front_bars:
        if _seg_len_2d(seg) > 6000.0:
            c = _classify_2d(seg)
            overlong[c] += 1

    # degree-1 节点（来自 drawing_file 属性，若存在）
    df = model.get("drawing_file")
    degree1 = None
    genuine_degree1 = None
    components_topology = None
    if isinstance(df, dict):
        props = df.get("properties", {})
        degree1 = props.get("topology_degree1")
        genuine_degree1 = props.get("topology_genuine_dangling")
        components_topology = props.get("topology_components")

    # 横隔 z 水平数
    dia_z = set()
    for b in bars:
        p = b.get("properties", {})
        if _is_diaphragm(p):
            f, t = p.get("from_node"), p.get("to_node")
            nf = comps.get(f) if f else None
            nt = comps.get(t) if t else None
            if nf and nt:
                zf = nf.get("properties", {}).get("z")
                zt = nt.get("properties", {}).get("z")
                if zf is not None and zt is not None and abs(zf - zt) < 1.0:
                    dia_z.add(round((zf + zt) / 2.0))

    return {
        "total_bars": total_bars,
        "physical_bars": len(physical_bars),
        "front_evaluable_bars": len(front_bars),
        "nodes": len(nodes),
        "role_count": dict(role_count),
        "diaphragm_count": dia_count,
        "diag_len_distribution": dict(len_dist),
        "diag_count_front": len(diag_lens),
        "diag_len_median": round(float(sorted(diag_lens)[len(diag_lens) // 2]), 1) if diag_lens else 0.0,
        "overlong_gt6m": overlong,
        "degree1": degree1,
        "genuine_degree1": genuine_degree1,
        "topology_components": components_topology,
        "diaphragm_z_levels": sorted(dia_z),
        "diaphragm_z_level_count": len(dia_z),
    }


def gt_stats(gt: dict) -> dict:
    bars = gt.get("bars", [])
    nodes = gt.get("nodes", {})
    # nodes 可能是 {id: [x,y,z]} 或 {id: {x,y,z}}
    def _coord(nid):
        n = nodes.get(nid)
        if n is None:
            return None
        if isinstance(n, (list, tuple)) and len(n) >= 3:
            return (float(n[0]), float(n[1]), float(n[2]))
        if isinstance(n, dict):
            x, y, z = n.get("x"), n.get("y"), n.get("z")
            if x is not None and y is not None and z is not None:
                return (float(x), float(y), float(z))
        return None

    lengths = []
    for b in bars:
        n1 = b.get("from") or b.get("from_node")
        n2 = b.get("to") or b.get("to_node")
        p1 = _coord(n1)
        p2 = _coord(n2)
        if p1 and p2:
            L = math.sqrt(sum((p2[i] - p1[i]) ** 2 for i in range(3)))
            lengths.append(L)
    return {
        "total": len(bars),
        "min_length": round(min(lengths), 1) if lengths else 0.0,
        "median_length": round(float(sorted(lengths)[len(lengths) // 2]), 1) if lengths else 0.0,
        "max_length": round(max(lengths), 1) if lengths else 0.0,
    }


def per_category_recall(gt: dict, model: dict, view: str) -> dict:
    """按类别（leg/diagonal/horizontal）细分 A2 召回。"""
    from traceability.eval.metrics import gt_bars_2d

    g = gt_bars_2d(gt, view)
    m = bars_from_model_2d(model, view=view, mode="physical")
    gt_segs = [s for s, _, _ in g]
    model_segs = [s for s, _ in m]

    # GT 分类
    gt_cat = [_classify_2d(s) for s in gt_segs]
    model_cat = [_classify_2d(s) for s in model_segs]

    out = {}
    for cat in ("leg", "diagonal", "horizontal"):
        g_idx = [i for i, c in enumerate(gt_cat) if c == cat]
        m_idx = [i for i, c in enumerate(model_cat) if c == cat]
        if not g_idx:
            out[cat] = {"gt": 0, "model": len(m_idx), "tp": 0, "recall": 0.0}
            continue
        sub_gt = [gt_segs[i] for i in g_idx]
        sub_m = [model_segs[i] for i in m_idx]
        res = eval_segment_pr(sub_gt, sub_m, segment_cost, [500.0])
        tp = res["sweep"][0]["tp"]
        out[cat] = {
            "gt": len(sub_gt),
            "model": len(sub_m),
            "tp": tp,
            "recall": round(tp / len(sub_gt), 4),
        }
    return out


def per_segment_recall(model: dict, view: str) -> dict:
    """按 source_file（段）细分 front 杆件分布与可匹配性。"""
    bars = bars_from_model_2d(model, view=view, mode="physical")
    by_seg = Counter()
    by_seg_len = defaultdict(list)
    for seg, p in bars:
        sf = str(p.get("source_file") or p.get("derived_from") or "unknown")
        # source_file 可能形如 35A1-JC1-06.dxf
        stem = Path(sf).stem if ".dxf" in sf or "." in sf else sf
        by_seg[stem] += 1
        by_seg_len[stem].append(_seg_len_2d(seg))
    out = {}
    for stem, n in sorted(by_seg.items()):
        lens = sorted(by_seg_len[stem])
        out[stem] = {
            "bars": n,
            "median_len": round(lens[len(lens) // 2], 1) if lens else 0.0,
            "overlong_gt6m": sum(1 for L in lens if L > 6000),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt")
    ap.add_argument("model")
    ap.add_argument("--view", default="front")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

    ms = model_stats(model)
    gs = gt_stats(gt)
    a2 = eval_a2_geometry_2d(gt, model, view=args.view, tols=DEFAULT_TOLS)
    cat = per_category_recall(gt, model, args.view)
    seg = per_segment_recall(model, args.view)

    report = {
        "model": ms,
        "gt": gs,
        "a2": {
            "n_gt": a2["n_gt"],
            "n_model": a2["n_model"],
            "sweep": a2["sweep"],
        },
        "recall_by_category": cat,
        "front_bars_by_segment": seg,
    }

    print("=== S0 基线报告 ===")
    print(f"模型杆件总数: {ms['total_bars']}")
    print(f"physical 杆件: {ms['physical_bars']}")
    print(f"front 可评测杆件: {ms['front_evaluable_bars']}")
    print(f"节点数: {ms['nodes']}")
    print(f"语义分类(role): {ms['role_count']}")
    print(f"横隔杆数: {ms['diaphragm_count']}  | 横隔 z 水平数: {ms['diaphragm_z_level_count']}")
    print(f"斜材(front)数: {ms['diag_count_front']}  | 中位长度: {ms['diag_len_median']}mm")
    print(f"斜材长度分布: {ms['diag_len_distribution']}")
    print(f">6m 超长杆(按类): {ms['overlong_gt6m']}")
    print(f"degree-1 节点: {ms['degree1']}  | genuine: {ms['genuine_degree1']}  | 拓扑组件: {ms['topology_components']}")
    print()
    print("GT 杆件:", gs)
    print()
    print("A2 tolerance sweep:")
    print(f"{'tol':>6} {'TP':>5} {'FP':>5} {'FN':>5} {'P':>8} {'R':>8}")
    for s in a2["sweep"]:
        print(f"{s['tol']:>6.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>8.1%} {s['recall']:>8.1%}")
    print()
    print("各类别 500mm 召回:")
    for c, d in cat.items():
        print(f"  {c:>10}: GT={d['gt']:>4} model={d['model']:>4} TP={d['tp']:>4} recall={d['recall']:.1%}")
    print()
    print("各段 front 杆件分布:")
    for stem, d in seg.items():
        print(f"  {stem:>20}: bars={d['bars']:>4} median={d['median_len']:>8}mm overlong>6m={d['overlong_gt6m']}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写: {args.out}")


if __name__ == "__main__":
    main()
