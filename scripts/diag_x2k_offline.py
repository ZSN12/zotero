# -*- coding: utf-8 -*-
"""离线验证：06 册画线 X 撑 → 层位 K 撑拓扑转换 (diag_x2k)
前后 vs GT 06 段斜杆 (z 12000-17000, front 面投影) 的贪心匹配覆盖率变化。

纪律：
1. 不跑全量管线；
2. 不改 eval/metrics.py；
3. 不提交 git commit；
4. GT 仅允许 z-only 层位常数，模型 x 来自图纸主腿线证据。
"""

import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from traceability.intake.centerline_extract import (
    collect_segments,
    stitch_collinear,
    pair_double_lines,
    seg_class,
    seg_len,
    reanchor_diag_endpoints,
    x_to_k_braces,
)
from traceability.intake.tower_spec import view_region, dimension_beat_anchor_config
from traceability.intake.tower_dxf import dimension_beat_anchors
from traceability.eval.metrics import _classify_3d, segment_cost
import ezdxf

DXF = "out/xianyu-acceptance/batch-jc1/dxf/35A1-JC1-06.dxf"
OVERLAY = "examples/external/guowang_35A1/layer_overlay.json"
GT_PATH = "examples/gt/35A1-JC1_ground_truth.json"
STEM = "35A1-JC1-06"

LEVELS = [12000.0, 13000.0, 14000.0, 14400.0, 14500.0, 16000.0, 17000.0]
Z_LO, Z_HI = 12000.0, 17000.0
TOL = 500.0


def gt_06_diags_analysis():
    """读取 GT 06 段斜杆 (z 12000-17000)。"""
    gt = json.loads(Path(GT_PATH).read_text(encoding="utf-8"))
    nodes = gt["nodes"]
    gt_fb_36 = []
    target_ids = [
        "PM_0566", "PM_0567", "PM_0568", "PM_0569",
        "PM_0578", "PM_0579", "PM_0580", "PM_0581",
        "PM_0590", "PM_0591", "PM_0592", "PM_0593",
        "PM_0680", "PM_0681", "PM_0682", "PM_0683",
        "PM_0698", "PM_0699", "PM_0700", "PM_0701",
        "PM_0766", "PM_0767", "PM_0768", "PM_0769",
        "PM_0864", "PM_0865", "PM_0866", "PM_0867",
        "PM_0974", "PM_0975", "PM_0976", "PM_0977",
        "PM_0982", "PM_0983", "PM_0984", "PM_0985",
    ]
    bars_map = {b["id"]: b for b in gt["bars"]}
    for tid in target_ids:
        b = bars_map[tid]
        f, t = nodes[b["from"]], nodes[b["to"]]
        x1, z1, x2, z2 = f[0], f[2], t[0], t[2]
        if (x1, z1) > (x2, z2):
            x1, z1, x2, z2 = x2, z2, x1, z1
        gt_fb_36.append(((x1, z1, x2, z2), tid))
    return gt_fb_36


def greedy_match(gt_segs, model_segs, tol=TOL):
    """一对一贪心匹配。"""
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
    print("diag_x2k 离线验证：06 册画线 X 撑 → 层位 K 撑拓扑转换")
    print("=" * 70)

    # 1. 提取图纸几何与标定
    region = view_region(STEM, "front", overlay=OVERLAY)
    bbox = tuple(float(v) for v in region["region"])
    scale_x = float(region["scale_x"])
    origin_x = float(region["origin"][0])

    doc = ezdxf.readfile(DXF)
    beat_cfg = dimension_beat_anchor_config(STEM, overlay=OVERLAY)
    ba = dimension_beat_anchors(
        doc.modelspace(), region,
        float(beat_cfg.get("z_base_mm", 0.0)),
        beat_min_mm=float(beat_cfg.get("beat_min_mm", 350.0)),
        beat_max_mm=float(beat_cfg.get("beat_max_mm", 800.0)),
        mode=str(beat_cfg.get("mode", "beats")),
    )
    yz_pairs = sorted(zip((float(v) for v in ba["y_draw"]),
                          (float(v) for v in ba["z"])))

    def yz_of(u_y: float) -> float:
        ps = yz_pairs
        if u_y <= ps[0][0]:
            (y0, z0), (y1, z1) = ps[0], ps[1]
        elif u_y >= ps[-1][0]:
            (y0, z0), (y1, z1) = ps[-2], ps[-1]
        else:
            (y0, z0), (y1, z1) = ps[0], ps[1]
            for i in range(len(ps) - 1):
                if ps[i][0] <= u_y <= ps[i + 1][0]:
                    (y0, z0), (y1, z1) = ps[i], ps[i + 1]
                    break
        if y1 == y0:
            return z0
        return z0 + (u_y - y0) / (y1 - y0) * (z1 - z0)

    segs = [s for s in collect_segments(DXF, bbox) if seg_len(s) >= 0.8]
    stitched = stitch_collinear(segs, gap_tol=8.0, ang_tol=6.0, col_tol=1.8)
    centers = pair_double_lines(stitched, max_off=6.0)

    # 提取 mm 域 diag 与 leg
    diag_mm = []
    for s in centers:
        sx1, sy1, sx2, sy2 = s[0], s[1], s[2], s[3]
        dxmm, dzmm = (sx2 - sx1) * scale_x, yz_of(sy2) - yz_of(sy1)
        if abs(dzmm) < 100.0 or abs(dxmm) < 100.0 or abs(dxmm) > abs(dzmm) * 4.0:
            continue
        diag_mm.append(((sx1 - origin_x) * scale_x, yz_of(sy1),
                        (sx2 - origin_x) * scale_x, yz_of(sy2)))

    leg_mm = [(((s[0] - origin_x) * scale_x, yz_of(s[1]),
                (s[2] - origin_x) * scale_x, yz_of(s[3])))
              for s in centers if seg_class(s) == "vert"]

    print(f"1. 图纸中心线提取: diag 斜线: {len(diag_mm)} 条，vert 腿线: {len(leg_mm)} 条")

    # 2. 基线：仅 reanchor (无 x2k)
    base_reanchored = reanchor_diag_endpoints(
        diag_mm, leg_mm, LEVELS, reanchor_tol=750.0, min_seg_len=500.0
    )

    # 3. 启用 diag_x2k + reanchor
    x2k_segs = x_to_k_braces(
        diag_mm, leg_mm, LEVELS,
        snap_tol=650.0, min_span_x=1200.0, min_span_z=800.0,
        center_cross_tol=300.0, max_copies_per_panel=2,
    )
    final_reanchored = reanchor_diag_endpoints(
        x2k_segs, leg_mm, LEVELS, reanchor_tol=750.0, min_seg_len=500.0
    )
    print(f"2. x_to_k_braces 转换完成: {len(diag_mm)} 条 -> {len(x2k_segs)} 条")
    print(f"3. reanchor_diag_endpoints 完成: 输出 {len(final_reanchored)} 条")

    # 4. 评估 GT 36 根 f/b diag 贪心匹配
    gt_36 = gt_06_diags_analysis()
    matches_raw, g_raw, m_raw = greedy_match(gt_36, diag_mm)
    matches_base, g_base, m_base = greedy_match(gt_36, base_reanchored)
    matches_x2k, g_x2k, m_x2k = greedy_match(gt_36, final_reanchored)

    print("\n" + "=" * 70)
    print("4. 贪心匹配对比 (GT 36 根 f/b diag, tol = 500mm)")
    print("=" * 70)
    print(f"  BASELINE (原始画线)     : 命中 {len(g_raw):2d}/{len(gt_36):2d} ({100.0*len(g_raw)/len(gt_36):5.1f}%)")
    print(f"  BASELINE + reanchor     : 命中 {len(g_base):2d}/{len(gt_36):2d} ({100.0*len(g_base)/len(gt_36):5.1f}%)")
    print(f"  AFTER diag_x2k+reanchor : 命中 {len(g_x2k):2d}/{len(gt_36):2d} ({100.0*len(g_x2k)/len(gt_36):5.1f}%)")
    print(f"  NET GAIN                : +{len(g_x2k) - len(g_base)} 根 (0 -> {len(g_x2k)})")

    print("\n" + "=" * 70)
    print("5. diag_x2k 命中明细清单")
    print("=" * 70)
    for rank, (c, gi, mi) in enumerate(matches_x2k, 1):
        g_seg, gid = gt_36[gi]
        m = final_reanchored[mi]
        print(f"  #{rank:02d} {gid:10s} | cost: {c:5.1f}mm")
        print(f"      模型杆: ({m[0]:7.1f}, {m[1]:7.1f}) -> ({m[2]:7.1f}, {m[3]:7.1f})")
        print(f"      GT 杆 : ({g_seg[0]:7.1f}, {g_seg[1]:7.1f}) -> ({g_seg[2]:7.1f}, {g_seg[3]:7.1f})")


if __name__ == "__main__":
    main()
