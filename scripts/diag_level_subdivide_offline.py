# -*- coding: utf-8 -*-
"""离线单元级验证：05 册斜材层位打断（subdivide_diag_at_levels）前后
vs GT 05 段斜杆（z 17000-24000，front 面投影）的贪心匹配覆盖率变化。

不跑全量管线：只调 extract_calibrated_centerlines 复现 37 条 ≥300mm 斜线，
再用 eval.metrics.segment_cost 语义做一对一贪心匹配（tol=500mm，
endpoint_sum_cost_lt_tol：双端点误差和，正反顺序取最小）。

纪律：打断函数只吃 z-only 层位常数表；GT 仅用于事后测量覆盖率。
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traceability.intake.centerline_extract import (  # noqa: E402
    extract_calibrated_centerlines, subdivide_diag_at_levels,
)
from traceability.eval.metrics import _classify_3d, segment_cost  # noqa: E402

DXF = "out/35A1-JC1-legsynth11/_dxf_scope/35A1-JC1-05.dxf"
OVERLAY = "examples/external/guowang_35A1/layer_overlay.json"
GT_PATH = "examples/gt/35A1-JC1_ground_truth.json"
STEM = "35A1-JC1-05"

# z-only 层位常数（纪律允许的唯一 GT 派生输入）
LEVELS = [17000, 18000, 19000, 19400, 20700, 21000, 21500, 21900, 22000,
          22800, 24000]
Z_LO, Z_HI = 17000.0, 24000.0
TOL = 500.0


def seg_class_mm(s):
    a = math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180.0
    if a < 12 or a > 168:
        return "horiz"
    if 78 < a < 102:
        return "vert"
    return "diag"


def gt_front_diags():
    """GT 05 段斜杆：双端点 z ∈ [17000, 24000] 且 3D 分类为 diagonal，
    投影到 front 面 (x, z)。保留 front/back 对称杆的 multiplicity。"""
    gt = json.loads(Path(GT_PATH).read_text(encoding="utf-8"))
    nodes = gt["nodes"]
    out = []
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
        out.append(((x1, z1, x2, z2), b["id"]))
    return out


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
    cands, calib, audit = extract_calibrated_centerlines(
        DXF, STEM, OVERLAY, verbose=True)
    diags = [s for s in cands
             if seg_class_mm(s) == "diag"
             and math.hypot(s[2] - s[0], s[3] - s[1]) >= 300.0]
    print(f"model diag >=300mm: {len(diags)}")

    after = subdivide_diag_at_levels(diags, LEVELS)
    n_split = sum(1 for s in diags
                  if sum(1 for a in after
                         if a[0] == s[0] and a[1] == s[1]) != 1)
    print(f"after subdivide: {len(after)} segs "
          f"(+{len(after) - len(diags)})")

    gt = gt_front_diags()
    print(f"GT front diags z[{Z_LO:.0f},{Z_HI:.0f}]: {len(gt)}")

    for tag, model in (("BEFORE", diags), ("AFTER ", after)):
        matches, g_used, m_used = greedy_match(gt, model)
        print(f"[{tag}] matched GT {len(g_used)}/{len(gt)} "
              f"({100.0 * len(g_used) / len(gt):.1f}%), "
              f"model segs used {len(m_used)}/{len(model)}")

    # 明细：打断产物的去向
    print("\n-- split detail (AFTER) --")
    from collections import defaultdict
    # 重新带 handle 跑一遍以追踪 split_from
    dicts = [{"start": (s[0], s[1]), "end": (s[2], s[3]),
              "handle": f"D{i}"} for i, s in enumerate(diags)]
    after_d = subdivide_diag_at_levels(dicts, LEVELS)
    groups = defaultdict(list)
    for c in after_d:
        groups[c.get("split_from") or c["handle"]].append(c)
    for h, kids in sorted(groups.items(),
                          key=lambda kv: -len(kv[1])):
        if len(kids) > 1:
            z0 = min(min(k["start"][1], k["end"][1]) for k in kids)
            z1 = max(max(k["start"][1], k["end"][1]) for k in kids)
            print(f"  {h}: z {z0:.0f}->{z1:.0f} → {len(kids)} 段 "
                  f"(levels {kids[0]['split_levels_z']})")

    # AFTER 匹配明细（按 GT z 排序）
    matches, g_used, _ = greedy_match(gt, after)
    print("\n-- AFTER matched GT (by z) --")
    rows = []
    for c, gi, mi in matches:
        (g, bid) = gt[gi]
        rows.append((min(g[1], g[3]), bid, c))
    for z, bid, c in sorted(rows):
        print(f"  z~{z:7.0f}  {bid}  cost={c:7.1f}")


if __name__ == "__main__":
    main()
