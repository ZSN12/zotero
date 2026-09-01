# -*- coding: utf-8 -*-
"""P1.1 centerline_extract 单元测试。

覆盖：双线配对（方向归一化）、共线缝合、标记层位识别、斜杆端点簇
（腿裁剪）、生产标定（overlay 锚点 + 图纸证据锚点）、tower_dxf 注入段
形状。GT 隔离：本测试不读 GT，只用合成几何 + 临时 overlay。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in __import__("sys").path:
    import sys
    sys.path.insert(0, str(REPO))

from traceability.intake.centerline_extract import (  # noqa: E402
    CenterlineCalibration,
    collect_segments,
    diagonal_endpoint_clusters,
    extract_centerline_drawing_segments,
    find_beam_markers,
    pair_double_lines,
    seg_angle,
    seg_class,
    seg_len,
    stitch_collinear,
    subdiv_t_x,
    synth_beams,
)


# ---------------------------------------------------------------------------
# 几何原语
# ---------------------------------------------------------------------------

def test_seg_class_horiz_vert_diag():
    assert seg_class((0, 0, 100, 0)) == "horiz"
    assert seg_class((0, 0, 0, 100)) == "vert"
    assert seg_class((0, 0, 100, 100)) == "diag"
    # 接近水平/垂直的容差
    assert seg_class((0, 0, 100, 5)) == "horiz"
    assert seg_class((0, 0, 5, 100)) == "vert"


def test_seg_angle_normalized_0_180():
    assert seg_angle((0, 0, 10, 0)) == 0.0
    assert 89 < seg_angle((0, 0, 0, 10)) < 91
    # (0,0)→(-10,10) 方向 135°：与 (0,0)→(10,-10) 同一条无向线，%180 归一后为 135
    assert 134 < seg_angle((0, 0, -10, 10)) < 136


# ---------------------------------------------------------------------------
# 双线配对
# ---------------------------------------------------------------------------

def test_pair_double_lines_merges_parallel_pair():
    segs = [
        (0.0, 0.0, 0.0, 100.0, "L1"),
        (3.0, 0.0, 3.0, 100.0, "L1"),  # 偏距 3u 的平行线
    ]
    out = pair_double_lines(segs)
    assert len(out) == 1
    cx = (out[0][0] + out[0][2]) / 2
    assert abs(cx - 1.5) < 0.1  # 中心线在双线中间


def test_pair_double_lines_keeps_far_parallel():
    segs = [
        (0.0, 0.0, 0.0, 100.0, "L1"),
        (30.0, 0.0, 30.0, 100.0, "L1"),  # 偏距 30u：两根独立杆
    ]
    out = pair_double_lines(segs)
    assert len(out) == 2


def test_pair_double_lines_direction_normalized():
    """同线反向重复：翻转归一化后合并，不产生零长中心线。"""
    segs = [
        (10.0, 10.0, 10.0, 50.0, "L1"),
        (10.2, 50.0, 10.2, 10.0, "L2"),  # 反向、近平行、同长
    ]
    out = pair_double_lines(segs, max_off=6.0)
    assert len(out) == 1
    assert seg_len(out[0]) > 30  # 非退化


# ---------------------------------------------------------------------------
# 共线缝合
# ---------------------------------------------------------------------------

def test_stitch_collinear_joins_gaps():
    segs = [
        (0.0, 0.0, 0.0, 40.0, "L"),
        (0.0, 44.0, 0.0, 90.0, "L"),  # gap 4u
    ]
    out = stitch_collinear(segs, gap_tol=6.0)
    assert len(out) == 1
    assert abs(out[0][1]) < 0.1 and abs(out[0][3] - 90.0) < 0.1


def test_stitch_collinear_keeps_offset_lines():
    segs = [
        (0.0, 0.0, 0.0, 40.0, "L"),
        (5.0, 44.0, 5.0, 90.0, "L"),  # 平移 5u：不是共线
    ]
    out = stitch_collinear(segs, gap_tol=6.0, col_tol=1.5)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# 标记层位
# ---------------------------------------------------------------------------

def test_find_beam_markers_clusters_levels():
    # 塔中心 x=50：两层标记（每层两条短划）
    segs = [
        (48.0, 10.0, 52.0, 10.0, "L"),
        (48.0, 10.5, 52.0, 10.5, "L"),
        (48.0, 40.0, 52.0, 40.0, "L"),
        (48.0, 40.5, 52.0, 40.5, "L"),
        (0.0, 25.0, 20.0, 25.0, "L"),  # 远离中心：非标记
    ]
    levels = find_beam_markers(segs, x_center=50.0, center_tol=14.0)
    assert len(levels) == 2
    assert any(abs(v - 10.25) < 0.5 for v in levels)
    assert any(abs(v - 40.25) < 0.5 for v in levels)


def test_find_beam_markers_single_wide_line():
    segs = [(44.0, 22.0, 56.0, 22.0, "L")]  # 宽 ≥10 的单线也算层位
    levels = find_beam_markers(segs, x_center=50.0)
    assert len(levels) == 1 and abs(levels[0] - 22.0) < 0.5


# ---------------------------------------------------------------------------
# 斜杆端点簇（腿裁剪）
# ---------------------------------------------------------------------------

def _centers_fixture():
    """两根腿 + 三层 X 撐（端点在腿上）+ 腿下两条斜散线（图签噪声）。"""
    left, right = -100.0, 100.0
    segs = [
        (left, -50.0, left, 200.0, "L"),   # 左腿
        (right, -50.0, right, 200.0, "L"),  # 右腿
        # X 撐：端点 y=0 与 y=60（两根对角）
        (left, 0.0, right, 60.0, "L"),
        (right, 0.0, left, 60.0, "L"),
        # X 撐：端点 y=60 与 y=120
        (left, 60.0, right, 120.0, "L"),
        (right, 60.0, left, 120.0, "L"),
        # 腿以下斜散线（图签噪声）：y=-70/-60 各两条（成簇过 min_support）
        (left + 10, -70.0, left + 40, -60.0, "L"),
        (right - 10, -70.0, right - 40, -60.0, "L"),
    ]
    return segs


def test_diagonal_clusters_within_leg_extent():
    segs = _centers_fixture()
    leg_extent = (-50.0, 200.0)
    clusters = diagonal_endpoint_clusters(segs, leg_y_extent=leg_extent)
    # 端点簇: 0, 60, 120（-70 被腿范围裁掉）
    assert clusters == pytest.approx([0.0, 60.0, 120.0], abs=1.0)


def test_diagonal_clusters_without_clip_keeps_noise():
    segs = _centers_fixture()
    clusters = diagonal_endpoint_clusters(segs, leg_y_extent=None)
    assert min(clusters) == pytest.approx(-70.0, abs=1.0)  # 噪声进入


# ---------------------------------------------------------------------------
# 生产标定
# ---------------------------------------------------------------------------

def test_calibration_linear_maps():
    calib = CenterlineCalibration(
        x_origin_u=50.0, x_scale_mm=20.0,
        z_anchor_lo_y=-100.0, z_anchor_hi_y=100.0,
        z_anchor_lo_mm=13000.0, z_anchor_hi_mm=17000.0,
    )
    assert calib.x_of_u(50.0) == 0.0
    assert calib.x_of_u(60.0) == pytest.approx(200.0)  # 10u × 20mm/u
    assert calib.z_of_y(-100.0) == 13000.0
    assert calib.z_of_y(100.0) == 17000.0
    assert calib.z_of_y(0.0) == pytest.approx(15000.0)  # 线性中点


def test_calibration_zero_span_degenerate():
    calib = CenterlineCalibration(
        x_origin_u=0.0, x_scale_mm=20.0,
        z_anchor_lo_y=10.0, z_anchor_hi_y=10.0,  # 退化跨度
        z_anchor_lo_mm=13000.0, z_anchor_hi_mm=17000.0,
    )
    assert calib.z_of_y(99.0) == 13000.0  # 不除零


# ---------------------------------------------------------------------------
# 合成横杆 + T/X 细分
# ---------------------------------------------------------------------------

def test_synth_beams_all_pairs():
    levels = [100.0]
    legs = [-100.0, 0.0, 100.0]  # 断点含中心
    z_of_y = lambda y: y * 20.0
    x_of_u = lambda u: u * 20.0
    out = synth_beams(levels, legs, z_of_y, x_of_u, x_center_u=0.0)
    # P2.3 同半侧全对：(-100,0) 与 (0,100) 相邻对；跨中心 (-100,100)
    # 两端均非中心——GT 环梁无此结构（GT 层 = [0,±hw] 全跨 + [±hw/2,±hw]
    # 弦段，无 [-hw,hw] 通长），排除。
    assert len(out) == 2  # (-2000,0),(0,2000)
    # 层位 z = 100u × 20 = 2000mm
    assert all(seg[1] == 2000.0 and seg[3] == 2000.0 for seg in out)
    # min_span 过滤
    out2 = synth_beams(levels, legs, z_of_y, x_of_u, 0.0, min_span_mm=1500.0)
    assert len(out2) == 2  # 两对都是 2000mm
    out3 = synth_beams(levels, legs, z_of_y, x_of_u, 0.0, min_span_mm=2500.0)
    assert len(out3) == 0  # 跨中心对已排除，无 ≥2500 对


def test_subdiv_t_x_splits_at_crossing():
    # 竖线 (0,0)-(0,100) 与横线 (-50,50)-(50,50) 真交于 (0,50)
    segs = [(0.0, 0.0, 0.0, 100.0), (-50.0, 50.0, 50.0, 50.0)]
    out = subdiv_t_x(segs, snap=5.0)
    assert len(out) == 4  # 竖线 2 段 + 横线 2 段
    zs = sorted((s[1] + s[3]) / 2 for s in out if abs(s[2] - s[0]) < 1e-6)
    assert zs == pytest.approx([25.0, 75.0])


def test_subdiv_t_x_t_junction():
    # 水平通长线 + 一根竖线端点落在其内部（T 形）
    segs = [(-100.0, 0.0, 100.0, 0.0), (30.0, 0.0, 30.0, 50.0)]
    out = subdiv_t_x(segs, snap=5.0)
    horiz = [s for s in out if abs(s[1] - s[3]) < 1e-6]
    assert len(horiz) == 2  # 通长线在 x=30 劈开
    xs = sorted((s[0] + s[2]) / 2 for s in horiz)
    assert xs == pytest.approx([-35.0, 65.0])


# ---------------------------------------------------------------------------
# 注入段形状（tower_dxf 契约）
# ---------------------------------------------------------------------------

def test_extract_drawing_segments_shape(tmp_path):
    """合成最小 DXF（双线腿 + X 撐 + 标记）→ 注入段形状契约。"""
    import ezdxf

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    # 双线左腿 x=-10/-7，双线右腿 x=10/7，y ∈ [0,100]
    for x in (-20.0, -17.0, 17.0, 20.0):
        msp.add_line((x, 0.0), (x, 100.0))
    # X 撐（0..100 两根对角）
    msp.add_line((-18.5, 0.0), (18.5, 50.0))
    msp.add_line((18.5, 0.0), (-18.5, 50.0))
    # 层位标记（塔中心 x=0，两条短划）
    msp.add_line((-2.0, 50.0), (2.0, 50.0))
    msp.add_line((-2.0, 50.5), (2.0, 50.5))
    dxf = tmp_path / "fake-06.dxf"
    doc.saveas(str(dxf))

    overlay = {
        "view_regions": {
            "fake-06": [{
                "kind": "front",
                "region": [-50.0, 50.0, -20.0, 120.0],
                "origin": [0.0, 0.0],
                "scale_x": 20.0,
                "scale_y": 20.0,
                "z_offset": 13000,
                "z_span_mm": 4000.0,
            }],
        },
        "centerline_extract": {"fake-06": {"enabled": True}},
    }
    ov_path = tmp_path / "ov.json"
    ov_path.write_text(__import__("json").dumps(overlay), encoding="utf-8")

    segs, audit = extract_centerline_drawing_segments(str(dxf), "fake-06", overlay=str(ov_path))
    assert segs, "应产出注入段"
    for s in segs:
        assert s["view_type"] == "front"
        assert "handle" not in s  # handle 由 tower_dxf 注入时分配
        assert "start" in s and "end" in s
        assert s["source_extractor"] == "centerline_extract"
    # 合成横杆存在（marker 层位）
    synth = [s for s in segs if s["geometry_origin"] == "marker_synth"]
    assert synth, "标记层位应合成横杆"
    assert audit["units"] == "drawing"
    assert audit["n_output_segments"] == len(segs)


def test_no_gt_inputs_in_production_path():
    """GT 隔离：centerline_extract 模块不得 import eval.metrics / 读 GT。"""
    import traceability.intake.centerline_extract as ce
    src = Path(ce.__file__).read_text(encoding="utf-8")
    for banned in ("ground_truth", "eval.metrics", "gt_bars", "examples/gt"):
        assert banned not in src, f"生产模块出现 GT 依赖: {banned}"
