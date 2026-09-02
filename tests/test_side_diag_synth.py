# -*- coding: utf-8 -*-
"""P2.4j+ side_diag_synth（侧立面深度斜杆直读通道）单元测试。

深度斜杆（depth_diag）GT 几何：上端点居中（y=0，顶面十字区），下端点
落角点（±hw）；端点层位 = 横杆层位 ∪ 腿段边界。本通道直读侧立面斜线，
双线合并 → z_anchors 层位映射 → 深度 {-hw, 0, +hw} 吸附 → side_reads
追加。零 GT 坐标注入。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest

from traceability.model import Component, EngineeringModel

from test_side_horiz_synth import (
    _mk_dxf, _mk_model_with_front_legs, _mk_overlay,
)


def _mk_overlay_diag(tmp_path: Path, dxf: Path, region, z_anchors, levels, spans):
    ov = {
        "side_horiz_synth": True,
        "centerline_extract": {
            "tower": {
                "side_horiz_synth": True,
                "side_diag_synth": True,
                "side_horiz_synth_region": {
                    "region": region,
                    "origin": [ (region[0] + region[1]) / 2.0, region[2] ],
                    "scale_x": 20.0,
                    "z_anchors": z_anchors,
                },
                "beam_marker_levels_mm": levels,
                "leg_synth_spans_mm": spans,
            }
        },
    }
    p = tmp_path / "overlay.json"
    p.write_text(json.dumps(ov), encoding="utf-8")
    return p


def test_cross_face_diag_read(tmp_path):
    """跨面斜线：上端居中 (y=0, z_high)、下端角点 (±hw, z_low) → 直读。"""
    from traceability.intake.tower_views import side_horiz_synth
    # hw: z=17000→1500, z=19000→1450（线性）。斜线从中心上端到左下角：
    # 图纸域：上端 (ox, y=-9802)（z 19000），下端 (ox-72.5, y=-9865)（z ~17000, 深度 -1450）
    ox = 34735.0
    dxf = _mk_dxf(tmp_path, [
        (ox, -9802, ox - 72.5, -9865),
        (ox + 0.3, -9802, ox - 72.2, -9865),   # 双线
    ])
    model = _mk_model_with_front_legs([17000, 19000], 1500, 1450)
    ovp = _mk_overlay_diag(
        tmp_path, dxf, [34600, 34870, -9922, -9538],
        [[-9868, 17000], [-9802, 19000]],
        [19000], [[17000, 19000]])
    n = side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    reads = model.components["drawing_file"].properties["side_reads"]
    diags = [r for r in reads if r.get("geometry_origin") == "side_diag_synth"]
    assert len(diags) == 1, f"应 1 根斜杆，实际 {len(diags)}"
    r = diags[0]
    # 方向归一：低 z 在前 → from=(y≈-hw, 17000), to=(y≈0, 19000)
    assert abs(r["from"][2] - 17000.0) < 0.01
    assert abs(r["to"][2] - 19000.0) < 0.01
    assert abs(r["from"][1] + 1500) < 1.0   # ≈ -hw(17000)=1500
    assert abs(r["to"][1]) < 0.01           # 居中


def test_diag_dedup_direction_normalized(tmp_path):
    """镜像方向斜线（下→上 vs 上→下）归一后去重。"""
    from traceability.intake.tower_views import side_horiz_synth
    ox = 34735.0
    dxf = _mk_dxf(tmp_path, [
        (ox, -9802, ox - 72.5, -9865),       # 上→下
        (ox - 72.5, -9865, ox, -9802),       # 下→上（同一条线反向）
    ])
    model = _mk_model_with_front_legs([17000, 19000], 1500, 1450)
    ovp = _mk_overlay_diag(
        tmp_path, dxf, [34600, 34870, -9922, -9538],
        [[-9868, 17000], [-9802, 19000]],
        [19000], [[17000, 19000]])
    side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    reads = model.components["drawing_file"].properties["side_reads"]
    diags = [r for r in reads if r.get("geometry_origin") == "side_diag_synth"]
    assert len(diags) == 1, f"反向线应归一去重为 1，实际 {len(diags)}"


def test_same_face_diag_rejected(tmp_path):
    """同面斜线（两端深度同为 -hw）弃——front 视图已覆盖。"""
    from traceability.intake.tower_views import side_horiz_synth
    ox = 34735.0
    dxf = _mk_dxf(tmp_path, [
        (ox - 75, -9802, ox - 70, -9865),   # 深度始终 ≈ -1500/-1400（同面）
        (ox - 74.7, -9802, ox - 69.7, -9865),
    ])
    model = _mk_model_with_front_legs([17000, 19000], 1500, 1450)
    ovp = _mk_overlay_diag(
        tmp_path, dxf, [34600, 34870, -9922, -9538],
        [[-9868, 17000], [-9802, 19000]],
        [19000], [[17000, 19000]])
    side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    reads = model.components["drawing_file"].properties["side_reads"]
    diags = [r for r in reads if r.get("geometry_origin") == "side_diag_synth"]
    assert len(diags) == 0, f"同面斜线应弃，实际 {len(diags)}"


def test_diag_level_must_match_span_boundary(tmp_path):
    """斜杆端点须落层位（横杆层位 ∪ 腿段边界）；悬空端点弃。"""
    from traceability.intake.tower_views import side_horiz_synth
    ox = 34735.0
    # 下端 y=-9840 → z≈17700，不在任何层位（17000/19000 都差 >300）→ 弃
    dxf = _mk_dxf(tmp_path, [
        (ox, -9802, ox - 72.5, -9840),
        (ox + 0.3, -9802, ox - 72.2, -9840),
    ])
    model = _mk_model_with_front_legs([17000, 19000], 1500, 1450)
    ovp = _mk_overlay_diag(
        tmp_path, dxf, [34600, 34870, -9922, -9538],
        [[-9868, 17000], [-9802, 19000]],
        [19000], [[17000, 19000]])
    side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    reads = model.components["drawing_file"].properties["side_reads"]
    diags = [r for r in reads if r.get("geometry_origin") == "side_diag_synth"]
    assert len(diags) == 0, f"端点不落层位应弃，实际 {len(diags)}"


def test_diag_inclination_filter(tmp_path):
    """过陡（近竖）与过缓（近水平）斜线不进斜杆通道。"""
    from traceability.intake.tower_views import side_horiz_synth
    ox = 34735.0
    dxf = _mk_dxf(tmp_path, [
        # 近竖：dx=1u, dy=63u → 斜率 63 > 3.5 弃
        (ox, -9802, ox - 1.0, -9865),
        # 近水平：dx=60u, dy=5u → 斜率 0.08 < 0.35 弃
        (ox - 30, -9865, ox + 30, -9860),
    ])
    model = _mk_model_with_front_legs([17000, 19000], 1500, 1450)
    ovp = _mk_overlay_diag(
        tmp_path, dxf, [34600, 34870, -9922, -9538],
        [[-9868, 17000], [-9802, 19000]],
        [19000], [[17000, 19000]])
    side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    reads = model.components["drawing_file"].properties["side_reads"]
    diags = [r for r in reads if r.get("geometry_origin") == "side_diag_synth"]
    assert len(diags) == 0, f"倾角出界线应弃，实际 {len(diags)}"


def test_diag_off_by_default(tmp_path):
    """册级 side_diag_synth 未开 → 斜杆通道不动。"""
    from traceability.intake.tower_views import side_horiz_synth
    ox = 34735.0
    dxf = _mk_dxf(tmp_path, [
        (ox, -9802, ox - 72.5, -9865),
        (ox + 0.3, -9802, ox - 72.2, -9865),
    ])
    model = _mk_model_with_front_legs([17000, 19000], 1500, 1450)
    ovp = _mk_overlay_diag(
        tmp_path, dxf, [34600, 34870, -9922, -9538],
        [[-9868, 17000], [-9802, 19000]],
        [19000], [[17000, 19000]])
    ov = json.loads(ovp.read_text(encoding="utf-8"))
    ov["centerline_extract"]["tower"]["side_diag_synth"] = False
    ovp.write_text(json.dumps(ov), encoding="utf-8")
    side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    reads = model.components["drawing_file"].properties["side_reads"]
    diags = [r for r in reads if r.get("geometry_origin") == "side_diag_synth"]
    assert len(diags) == 0, "开关未开不应产生斜杆读取"
