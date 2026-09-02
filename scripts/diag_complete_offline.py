# -*- coding: utf-8 -*-
"""离线验证：05 册截断斜杆补全（complete_truncated_diags / diag_complete）
vs GT 05 段斜杆（z 17000-24000，front 面投影）的贪心匹配覆盖率对比。

纪律：
1. 不跑全量管线；
2. 不改 eval/metrics.py；
3. 不提交 git commit；
4. 图纸证据（画线方向、腿线拟合）+ z-only 层位常数，不注入 GT 坐标。
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traceability.intake.centerline_extract import (
    extract_centerline_drawing_segments,
    _extract_leg_profiles,
)
from traceability.eval.metrics import _classify_3d, segment_cost
from traceability.intake.tower_spec import view_region, dimension_beat_anchor_config
from traceability.intake.tower_dxf import dimension_beat_anchors
import ezdxf

DXF = "out/35A1-JC1-diagres2/_dxf_scope/35A1-JC1-05.dxf"
OVERLAY = "examples/external/guowang_35A1/layer_overlay.json"
GT_PATH = "examples/gt/35A1-JC1_ground_truth.json"
STEM = "35A1-JC1-05"
Z_LO, Z_HI = 17000.0, 24000.0
TOL = 500.0


def gt_diags_analysis():
    """读取 GT 05 段斜杆，并分别返回：
    1. gt_all: 全部 112 根 3D diagonal 在 front 面的投影；
    2. gt_front_face: 仅 front/back 面内的大斜杆（56 根，排除 side 面垂直退化杆）；
    3. gt_unique: 几何去重后的 front 投影（44 根）；
    4. gt_68: 全部 68 根 front/back 面斜杆（含多重 GT 结构）。
    """
    gt = json.loads(Path(GT_PATH).read_text(encoding="utf-8"))
    nodes = gt["nodes"]
    gt_all = []
    gt_front_face = []
    gt_unique_map = {}
    gt_68 = []

    for b in gt["bars"]:
        f, t = nodes.get(b["from"]), nodes.get(b["to"])
        if f is None or t is None:
            continue
        if not (Z_LO <= f[2] <= Z_HI and Z_LO <= t[2] <= Z_HI):
            continue

        x1, z1, x2, z2 = f[0], f[2], t[0], t[2]
        if (x1, z1) > (x2, z2):
            x1, z1, x2, z2 = x2, z2, x1, z1

        dx = abs(t[0] - f[0])
        dy = abs(t[1] - f[1])
        dz = abs(t[2] - f[2])
        if dz > 100.0 and dx > 400.0:
            gt_68.append(((x1, z1, x2, z2), b["id"]))

        if _classify_3d((tuple(f), tuple(t))) != "diagonal":
            continue

        gt_all.append(((x1, z1, x2, z2), b["id"]))

        if dx > 500.0:
            gt_front_face.append(((x1, z1, x2, z2), b["id"]))

        key = (round(x1, 1), round(z1, 1), round(x2, 1), round(z2, 1))
        if key not in gt_unique_map:
            gt_unique_map[key] = b["id"]

    gt_unique = [(k, v) for k, v in gt_unique_map.items()]
    return gt_all, gt_front_face, gt_unique, gt_68


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


def extract_segments_in_mm(overlay_dict, stem=STEM, dxf_path=DXF):
    """从 extract_centerline_drawing_segments 运行并转换为 mm 域线段（按 Front/Back 双面展开）。"""
    region = view_region(stem, "front", overlay=overlay_dict)
    scale_x = float(region.get("scale_x") or 20.0)
    origin_x = float(region["origin"][0])

    _beat_cfg = dimension_beat_anchor_config(stem, overlay=overlay_dict)
    _doc = ezdxf.readfile(str(dxf_path))
    _ba = dimension_beat_anchors(
        _doc.modelspace(), region,
        float(_beat_cfg.get("z_base_mm", 0.0)),
        beat_min_mm=float(_beat_cfg.get("beat_min_mm", 350.0)),
        beat_max_mm=float(_beat_cfg.get("beat_max_mm", 800.0)),
        mode=str(_beat_cfg.get("mode", "beats")),
        z_span_mm=tuple(_beat_cfg.get("z_span_mm", ())) if _beat_cfg.get("z_span_mm") else None,
    )
    _yz_pairs = sorted(zip((float(v) for v in _ba["y_draw"]), (float(v) for v in _ba["z"])))

    def _yz_of(u_y: float) -> float:
        ps = _yz_pairs
        if u_y <= ps[0][0]:
            (y0, z0), (y1, z1) = ps[0], ps[1]
        elif u_y >= ps[-1][0]:
            (y0, z0), (y1, z1) = ps[-2], ps[-1]
        else:
            for i in range(len(ps) - 1):
                if ps[i][0] <= u_y <= ps[i + 1][0]:
                    (y0, z0), (y1, z1) = ps[i], ps[i + 1]
                    break
        return z0 + (u_y - y0) / (y1 - y0) * (z1 - z0)

    segs, audit = extract_centerline_drawing_segments(dxf_path, stem, overlay=overlay_dict)

    diags_mm = []
    for s in segs:
        orig = s.get("geometry_origin")
        # 仅关注画线/重锚/补全的斜杆
        if orig not in ("dxf_geom", "diag_synth", "diag_complete"):
            continue
        sx1, sy1 = s["start"]
        sx2, sy2 = s["end"]
        x1m = (sx1 - origin_x) * scale_x
        z1m = _yz_of(sy1)
        x2m = (sx2 - origin_x) * scale_x
        z2m = _yz_of(sy2)
        dx = abs(x2m - x1m)
        dz = abs(z2m - z1m)
        if dx > 100.0 and dz > 100.0 and dx < dz * 4.0:
            if z1m > z2m or (z1m == z2m and x1m > x2m):
                x1m, z1m, x2m, z2m = x2m, z2m, x1m, z1m
            diags_mm.append(((x1m, z1m, x2m, z2m), orig, s))

    return diags_mm, audit


def main():
    print("=" * 70)
    print("diag_complete 离线原型验证：05 册截断斜杆补全前后对比")
    print("=" * 70)

    raw_overlay = json.loads(Path(OVERLAY).read_text(encoding="utf-8"))

    # 1. 基线配置：diag_complete = False
    overlay_base = json.loads(json.dumps(raw_overlay))
    overlay_base.setdefault("centerline_extract", {}).setdefault(STEM, {})["diag_complete"] = False
    diags_base, audit_base = extract_segments_in_mm(overlay_base)

    # 2. 补全配置：diag_complete = True
    overlay_comp = json.loads(json.dumps(raw_overlay))
    overlay_comp.setdefault("centerline_extract", {}).setdefault(STEM, {})["diag_complete"] = True
    diags_comp, audit_comp = extract_segments_in_mm(overlay_comp)

    print(f"1. 基线提取 (diag_complete=False): 输出斜杆 {len(diags_base)} 根")
    print(f"   Audit: n_diag_reanchor={audit_base.get('n_diag_reanchor_out')}, n_diag_complete={audit_base.get('n_diag_complete_out')}")
    print(f"2. 接入补全 (diag_complete=True) : 输出斜杆 {len(diags_comp)} 根 (新增 +{len(diags_comp) - len(diags_base)} 根)")
    print(f"   Audit: n_diag_reanchor={audit_comp.get('n_diag_reanchor_out')}, n_diag_complete={audit_comp.get('n_diag_complete_out')}")

    gt_all, gt_front_face, gt_unique, gt_68 = gt_diags_analysis()
    print(f"\n3. GT 斜杆基准:")
    print(f"   - GT 05 段 f/b 面斜杆集合: {len(gt_68)} 根")
    print(f"   - GT front 面主斜杆 (排除退化杆): {len(gt_front_face)} 根")
    print(f"   - GT 几何去重斜杆: {len(gt_unique)} 根")
    print(f"   - GT 全量 front 投影 (含 side 退化): {len(gt_all)} 根")

    # 4. 对比评测
    # 提供两套口径：
    # A) 1x 独立线段口径（与 diag_reanchor_offline 纯 2D 对齐）
    # B) 2x F/B 双面对称展开口径（与三维 4face 生成后的 3D 实体能力对齐）
    segs_base_1x = [s[0] for s in diags_base]
    segs_comp_1x = [s[0] for s in diags_comp]
    segs_base_fb = segs_base_1x + segs_base_1x
    segs_comp_fb = segs_comp_1x + segs_comp_1x

    print("\n" + "=" * 70)
    print("4A. 贪心匹配结果对比 (tol = 500mm，1x 单面线段口径)")
    print("=" * 70)

    benchmarks = [
        ("GT 05 段 f/b 面斜杆 (68 根)", gt_68),
        ("GT front 面主斜杆 (56 根)", gt_front_face),
        ("GT 几何独立斜杆 (44 根)", gt_unique),
        ("GT 全量 front 投影 (112 根)", gt_all),
    ]

    for label, gt_set in benchmarks:
        matches_b, g_used_b, _ = greedy_match(gt_set, segs_base_1x)
        matches_a, g_used_a, _ = greedy_match(gt_set, segs_comp_1x)
        delta = len(g_used_a) - len(g_used_b)
        rate_b = 100.0 * len(g_used_b) / len(gt_set)
        rate_a = 100.0 * len(g_used_a) / len(gt_set)
        print(f"\n【{label}】")
        print(f"  BEFORE : 命中 {len(g_used_b):2d}/{len(gt_set):2d} ({rate_b:5.1f}%)")
        print(f"  AFTER  : 命中 {len(g_used_a):2d}/{len(gt_set):2d} ({rate_a:5.1f}%)")
        print(f"  NET GAIN: +{delta} 根 (相对提升 +{100.0 * delta / max(len(g_used_b), 1):.1f}%)")

    print("\n" + "=" * 70)
    print("4B. 贪心匹配结果对比 (tol = 500mm，2x F/B 双面对称展开口径)")
    print("=" * 70)

    for label, gt_set in benchmarks:
        matches_b, g_used_b, _ = greedy_match(gt_set, segs_base_fb)
        matches_a, g_used_a, _ = greedy_match(gt_set, segs_comp_fb)
        delta = len(g_used_a) - len(g_used_b)
        rate_b = 100.0 * len(g_used_b) / len(gt_set)
        rate_a = 100.0 * len(g_used_a) / len(gt_set)
        print(f"\n【{label}】")
        print(f"  BEFORE : 命中 {len(g_used_b):2d}/{len(gt_set):2d} ({rate_b:5.1f}%)")
        print(f"  AFTER  : 命中 {len(g_used_a):2d}/{len(gt_set):2d} ({rate_a:5.1f}%)")
        print(f"  NET GAIN: +{delta} 根 (相对提升 +{100.0 * delta / max(len(g_used_b), 1):.1f}%)")

    # 展示补全通道直接命中的新增 GT
    matches_b, g_used_b, _ = greedy_match(gt_68, segs_base_fb)
    matches_a, g_used_a, m_used_a = greedy_match(gt_68, segs_comp_fb)
    newly_matched_gi = sorted(g_used_a - g_used_b)
    print("\n" + "=" * 70)
    print(f"5. diag_complete 新增命中的 GT 斜杆明细 (共 +{len(newly_matched_gi)} 根)")
    print("=" * 70)
    for gi in newly_matched_gi:
        g_seg, g_id = gt_68[gi]
        # 找到匹配它的 model seg
        c_best = 999999.0
        m_best = None
        for mi in m_used_a:
            c = segment_cost(g_seg, segs_comp_fb[mi])
            if c < c_best:
                c_best = c
                m_best = segs_comp_fb[mi]
        print(f"  * {g_id:10s} | cost: {c_best:5.1f}mm")
        print(f"      GT 坐标  : ({g_seg[0]:7.1f}, {g_seg[1]:7.1f}) -> ({g_seg[2]:7.1f}, {g_seg[3]:7.1f})")
        print(f"      补全杆件 : ({m_best[0]:7.1f}, {m_best[1]:7.1f}) -> ({m_best[2]:7.1f}, {m_best[3]:7.1f})")


if __name__ == "__main__":
    main()
