# -*- coding: utf-8 -*-
"""单元测试：X→K 拓扑转换 x_to_k_braces (mm 域)。

验证：
1. 跨中心 X 线劈成两根 K 撑（人字撑：中心顶点 (0, z_high) -> 左右腿位 (x_L/R, z_low)）
2. 中心轴交点在层位附近时的劈分 (err_cross <= center_cross_tol)
3. 交点及端点离层位远时不劈（原样透传）
4. 非 X 撑（半宽线、同半侧斜杆）不动（原样透传）
5. 层位 × 中心与腿节点坐标精确性 (x = al*z + bl, ar*z + br)
6. dict 结构输入支持与 metadata 标记 (geometry_class, source_extractor, diag_x2k)
7. 空输入安全退化
"""

import math
import pytest
from traceability.intake.centerline_extract import x_to_k_braces


@pytest.fixture
def sample_legs():
    # 模拟 06 册立面腿线（微斜度/竖直腿）：
    # 左腿：x = 0.0701 * z - 2728.5 (z=14000 时 x=-1746.9, z=16000 时 x=-1606.7, z=12000 时 x=-1887.2)
    # 右腿：x = -0.0701 * z + 2728.4 (z=14000 时 x=1746.8, z=16000 时 x=1606.6, z=12000 时 x=1887.0)
    l_leg = (-1887.2, 12000.0, -1606.7, 16000.0)
    r_leg = (1887.0, 12000.0, 1606.6, 16000.0)
    return [l_leg, r_leg]


@pytest.fixture
def sample_levels():
    return [12000.0, 13000.0, 14000.0, 14400.0, 14500.0, 16000.0, 17000.0]


def test_cross_center_x_split_to_k_braces(sample_legs, sample_levels):
    """用例 1：跨中心 X 撑被重写为两根 K 撑 (人字撑)。
    画线从 (1583.1, 16413.0) 跨中心到 (-1771.6, 13858.3)。
    顶端吸附到 z=16000，底端吸附到 z=14000。
    应输出两根 K 撑：
      (0.0, 16000.0) -> (-1746.9, 14000.0)
      (0.0, 16000.0) -> ( 1746.8, 14000.0)
    """
    raw_seg = (1583.1, 16413.0, -1771.6, 13858.3)
    out = x_to_k_braces([raw_seg], sample_legs, sample_levels, snap_tol=650.0)
    assert len(out) == 2

    k_left = next(s for s in out if s[2] < 0)
    k_right = next(s for s in out if s[2] > 0)

    # 顶点：(0.0, 16000.0)
    assert math.isclose(k_left[0], 0.0, abs_tol=1e-3)
    assert math.isclose(k_left[1], 16000.0, abs_tol=1e-3)
    assert math.isclose(k_right[0], 0.0, abs_tol=1e-3)
    assert math.isclose(k_right[1], 16000.0, abs_tol=1e-3)

    # 底端腿位与层位
    assert math.isclose(k_left[3], 14000.0, abs_tol=1e-3)
    assert math.isclose(k_left[2], -1746.9, abs_tol=1.0)

    assert math.isclose(k_right[3], 14000.0, abs_tol=1e-3)
    assert math.isclose(k_right[2], 1746.8, abs_tol=1.0)


def test_center_crossing_at_level_split(sample_legs, sample_levels):
    """用例 2：中心交点 z 接近层位时劈分 (Condition 2)。
    斜线穿过 x=0 的 z 近似为 14050，靠近层位 14000 (diff=50 <= 300)。
    下层位应自动取 14000 下方的 12000/13000。
    """
    # 从 (-1500, 13550) 到 (1500, 14550), x=0 时 z=14050, span_z = 1000 >= 800
    seg = (-1500.0, 13550.0, 1500.0, 14550.0)
    out = x_to_k_braces([seg], sample_legs, sample_levels, snap_tol=200.0, center_cross_tol=300.0)
    assert len(out) == 2
    for s in out:
        assert math.isclose(s[0], 0.0, abs_tol=1e-3)
        assert math.isclose(s[1], 14000.0, abs_tol=1e-3)


def test_far_from_level_not_split(sample_legs):
    """用例 3：交点及两端均离层位太远时，不劈分，保留原线。"""
    # 层位仅有 12000 和 16000
    levels = [12000.0, 16000.0]
    # 斜线在 13800 到 14200 之间，中心交点 z=14000，离 12000 和 16000 均差 2000mm
    seg = (-1000.0, 13800.0, 1000.0, 14200.0)
    out = x_to_k_braces([seg], sample_legs, levels, snap_tol=500.0, center_cross_tol=300.0)
    assert len(out) == 1
    assert out[0] == seg


def test_non_x_brace_not_touched(sample_legs, sample_levels):
    """用例 4：非 X 撑（半宽线，未跨越中心轴）不劈分，原样保留。"""
    # 纯在右半侧 (x > 0) 的斜撑
    half_width = (842.4, 13296.9, 1856.1, 12599.0)
    out = x_to_k_braces([half_width], sample_legs, sample_levels)
    assert len(out) == 1
    assert out[0] == half_width

    # 纯在左半侧 (x < 0) 的斜撑
    left_half = (-842.5, 13296.9, -1856.2, 12599.0)
    out2 = x_to_k_braces([left_half], sample_legs, sample_levels)
    assert len(out2) == 1
    assert out2[0] == left_half


def test_dict_input_metadata(sample_legs, sample_levels):
    """用例 5：dict 输入支持及通道 metadata 标记。"""
    raw_dict = {
        "start": (1583.1, 16413.0),
        "end": (-1771.6, 13858.3),
        "layer": "dxf_geom",
        "geometry_origin": "dxf_geom",
        "geometry_class": "recognized",
        "source_extractor": "centerline_extract",
    }
    out = x_to_k_braces([raw_dict], sample_legs, sample_levels)
    assert len(out) == 2
    for item in out:
        assert isinstance(item, dict)
        assert item["geometry_class"] == "recognized"
        assert item["source_extractor"] == "centerline_extract"
        assert item["diag_x2k"] is True
        assert item["reanchored"] is True
        assert item["layer"] == "diag_synth"


def test_node_coordinate_precision(sample_legs, sample_levels):
    """用例 6：输出层位 × 中心/腿节点坐标精确性。"""
    # 节间 12000 -> 14000 的 X 撑
    seg = (1759.6, 13899.7, -1947.1, 11393.6)
    out = x_to_k_braces([seg], sample_legs, sample_levels, snap_tol=650.0)
    assert len(out) == 2

    # 腿拟合公式：
    # al = 0.0701, bl = -2728.5 -> z=12000: x = -1887.2
    # ar = -0.0701, br = 2728.4 -> z=12000: x = 1887.0
    for s in out:
        assert math.isclose(s[0], 0.0, abs_tol=1e-6)
        assert math.isclose(s[1], 14000.0, abs_tol=1e-6)
        assert math.isclose(s[3], 12000.0, abs_tol=1e-6)
        if s[2] < 0:
            assert math.isclose(s[2], -1887.2, abs_tol=0.2)
        else:
            assert math.isclose(s[2], 1887.0, abs_tol=0.2)


def test_empty_input_fallback(sample_legs, sample_levels):
    """用例 7：空输入安全退化。"""
    assert x_to_k_braces([], sample_legs, sample_levels) == []
    assert x_to_k_braces([(0, 0, 1, 1)], sample_legs, []) == [(0, 0, 1, 1)]
