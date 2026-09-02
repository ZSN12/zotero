# -*- coding: utf-8 -*-
"""离线验证：05 册画线斜杆端点重锚（reanchor_diag_endpoints）前后
vs GT 05 段斜杆（z 17000-24000，front 面投影）的贪心匹配覆盖率变化。

纪律：
1. 不跑全量管线；
2. 不改 eval/metrics.py；
3. 不提交 git commit；不动 overlay；
4. GT 仅允许 z-only 层位常数，模型 x 来自图纸主腿线证据。
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traceability.intake.centerline_extract import (
    extract_calibrated_centerlines,
    reanchor_diag_endpoints,
    seg_class,
)
from traceability.eval.metrics import _classify_3d, segment_cost

DXF = "out/35A1-JC1-legsynth11/_dxf_scope/35A1-JC1-05.dxf"
OVERLAY = "examples/external/guowang_35A1/layer_overlay.json"
GT_PATH = "examples/gt/35A1-JC1_ground_truth.json"
STEM = "35A1-JC1-05"

# 层位设计常数（z-only）
LEVELS = [17000.0, 18000.0, 19000.0, 19400.0, 20700.0, 21000.0, 21500.0, 21900.0, 22000.0, 22800.0, 24000.0]
FINE_LEVELS = [18000.0, 21000.0, 21500.0]  # 非斜杆节点的细层位
Z_LO, Z_HI = 17000.0, 24000.0
TOL = 500.0


def seg_class_mm(s):
    a = math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180.0
    if a < 12 or a > 168:
        return "horiz"
    if 78 < a < 102:
        return "vert"
    return "diag"


def gt_diags_analysis():
    """读取 GT 05 段斜杆，并分别返回：
    1. gt_all: 全部 112 根 3D diagonal 在 front 面的投影；
    2. gt_front_face: 仅 front/back 面内的大斜杆（56 根，排除 side 面垂直退化杆）；
    3. gt_unique: 几何去重后的 front 投影（44 根）。
    """
    gt = json.loads(Path(GT_PATH).read_text(encoding="utf-8"))
    nodes = gt["nodes"]
    gt_all = []
    gt_front_face = []
    gt_unique_map = {}

    for b in gt["bars"]:
        f, t = nodes.get(b["from"]), nodes.get(b["to"])
        if f is None or t is None:
            continue
        if not (Z_LO <= f[2] <= Z_HI and Z_LO <= t[2] <= Z_HI):
            continue
        if _classify_3d((tuple(f), tuple(t))) != "diagonal":
            continue

        x1, z1, x2, z2 = f[0], f[2], t[0], t[2]
        if (x1, z1) > (x2, z2):
            x1, z1, x2, z2 = x2, z2, x1, z1

        gt_all.append(((x1, z1, x2, z2), b["id"]))

        # 区分 front 面还是 side 面：front 面的斜材 dx 显著大于 dy
        dx = abs(t[0] - f[0])
        dy = abs(t[1] - f[1])
        if dx > 500.0:
            gt_front_face.append(((x1, z1, x2, z2), b["id"]))

        key = (round(x1, 1), round(z1, 1), round(x2, 1), round(z2, 1))
        if key not in gt_unique_map:
            gt_unique_map[key] = b["id"]

    gt_unique = [(k, v) for k, v in gt_unique_map.items()]
    return gt_all, gt_front_face, gt_unique


def greedy_match(gt_segs, model_segs, tol=TOL):
    """一对一贪心匹配：全部 (cost<tol) 配对按 cost 升序依次占用。"""
    pairs = []
    for gi, (g, _) in enumerate(gt_segs):
        for mi, m in enumerate(model_segs):
            c = segment_cost(g, m)
            if c < tol:
                pairs.append((c, gi, mi))
    pairs.sort()
    g_used, m_used, matches = set(), set(), []
    for c, gi, mi in pairs:
        if gi in g_used or mi in m_used:
            continue
        g_used.add(gi)
        m_used.add(mi)
        matches.append((c, gi, mi))
    return matches, g_used, m_used


def main():
    print("=" * 70)
    print("diag_synth 离线原型验证：05 册画线斜杆端点重锚 (reanchor_diag_endpoints)")
    print("=" * 70)

    cands, calib, audit = extract_calibrated_centerlines(
        DXF, STEM, OVERLAY, verbose=False
    )
    diags = [s for s in cands
             if seg_class_mm(s) == "diag"
             and math.hypot(s[2] - s[0], s[3] - s[1]) >= 300.0]
    legs = [s for s in cands if seg_class_mm(s) == "vert"]

    print(f"1. 图纸中心线提取: 画线斜杆 >=300mm: {len(diags)} 条，腿线: {len(legs)} 条")

    # 执行重锚
    reanchored = reanchor_diag_endpoints(
        diags,
        legs,
        LEVELS,
        fine_levels=FINE_LEVELS,
        reanchor_tol=750.0,
        min_seg_len=500.0,
    )
    print(f"2. 端点重锚完成: 输出杆件 {len(reanchored)} 条")

    gt_all, gt_front_face, gt_unique = gt_diags_analysis()
    print(f"3. GT 斜杆基准:")
    print(f"   - GT 全量 front 投影 (含 side 面退化杆，保留 multiplicity): {len(gt_all)} 根")
    print(f"   - GT front 面内主斜杆 (排除 side 面退化杆): {len(gt_front_face)} 根")
    print(f"   - GT front 投影几何去重: {len(gt_unique)} 根")

    print("\n" + "=" * 70)
    print("4. 贪心匹配结果对比 (tol = 500mm)")
    print("=" * 70)

    benchmarks = [
        ("GT 全量 front 投影 (112 根)", gt_all),
        ("GT front 面主斜杆 (56 根)", gt_front_face),
        ("GT 几何独立斜杆 (44 根)", gt_unique),
    ]

    for label, gt_set in benchmarks:
        matches_b, g_used_b, m_used_b = greedy_match(gt_set, diags)
        matches_a, g_used_a, m_used_a = greedy_match(gt_set, reanchored)
        delta = len(g_used_a) - len(g_used_b)
        rate_b = 100.0 * len(g_used_b) / len(gt_set)
        rate_a = 100.0 * len(g_used_a) / len(gt_set)
        print(f"\n【{label}】")
        print(f"  BEFORE : 命中 {len(g_used_b):2d}/{len(gt_set):2d} ({rate_b:5.1f}%), 模型利用率 {len(m_used_b):2d}/{len(diags)}")
        print(f"  AFTER  : 命中 {len(g_used_a):2d}/{len(gt_set):2d} ({rate_a:5.1f}%), 模型利用率 {len(m_used_a):2d}/{len(reanchored)}")
        print(f"  NET GAIN: +{delta} 根 (相对提升 +{100.0 * delta / max(len(g_used_b), 1):.1f}%)")

    # 详细匹配明细展示
    matches_a, g_used_a, m_used_a = greedy_match(gt_all, reanchored)
    print("\n" + "=" * 70)
    print("5. 重锚后命中明细清单 (前 14 根匹配详情)")
    print("=" * 70)
    for rank, (c, gi, mi) in enumerate(matches_a, 1):
        g_seg, g_id = gt_all[gi]
        m = reanchored[mi]
        orig_s = diags[mi]
        orig_c = segment_cost(g_seg, orig_s)
        print(f"  #{rank:02d} D{mi:02d} -> {g_id:10s} | cost: {orig_c:6.1f}mm -> {c:5.1f}mm (降 {orig_c - c:5.1f}mm)")
        print(f"      画线原始: ({orig_s[0]:7.1f}, {orig_s[1]:7.1f}) -> ({orig_s[2]:7.1f}, {orig_s[3]:7.1f})")
        print(f"      重锚结果: ({m[0]:7.1f}, {m[1]:7.1f}) -> ({m[2]:7.1f}, {m[3]:7.1f})")
        print(f"      GT 对齐 : ({g_seg[0]:7.1f}, {g_seg[1]:7.1f}) -> ({g_seg[2]:7.1f}, {g_seg[3]:7.1f})")


if __name__ == "__main__":
    main()
