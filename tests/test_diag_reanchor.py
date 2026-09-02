# -*- coding: utf-8 -*-
"""单元测试：斜杆端点重锚 reanchor_diag_endpoints (mm 域)。

验证：
1. 单斜杆端点重锚（腿×层位、中心×层位）
2. X 交叉杆两半分别重锚到四个角点
3. 短杆不动（min_seg_len 保护）
4. dict 输入输出与 geometry_origin 标记
5. 细层位排除保护
"""

import math
import pytest
from traceability.intake.centerline_extract import reanchor_diag_endpoints


@pytest.fixture
def sample_legs():
    # 典型主腿：从 z=17000 到 24000
    # 左腿：x = 0.0645 * z - 2637 (z=17000 时 x=-1540.5, z=24000 时 x=-1089.0)
    # 右腿：x = -0.0645 * z + 2637 (z=17000 时 x=1540.5, z=24000 时 x=1089.0)
    l_leg = (-1540.5, 17000.0, -1089.0, 24000.0)
    r_leg = (1540.5, 17000.0, 1089.0, 24000.0)
    return [l_leg, r_leg]


@pytest.fixture
def sample_levels():
    return [17000.0, 18000.0, 19000.0, 19400.0, 20700.0, 21900.0, 24000.0]


def test_endpoint_reanchor_single_diag(sample_legs, sample_levels):
    """用例 1：单根斜杆端点重锚（起点靠近左腿、终点靠近中心轴）。"""
    # 模拟画线斜杆：起点在 (-1500, 17080)，终点在 (-30, 20650)
    # 真实目标节点：起点应吸附到 (-1540.5, 17000.0)，终点吸附到 (0.0, 20700.0)
    raw_seg = (-1500.0, 17080.0, -30.0, 20650.0)
    out = reanchor_diag_endpoints([raw_seg], sample_legs, sample_levels)
    assert len(out) == 1
    rx1, rz1, rx2, rz2 = out[0]

    # 验证左腿节点
    assert math.isclose(rz1, 17000.0, abs_tol=1e-3)
    assert math.isclose(rx1, -1540.5, abs_tol=1.0)

    # 验证中心节点
    assert math.isclose(rz2, 20700.0, abs_tol=1e-3)
    assert math.isclose(rx2, 0.0, abs_tol=1e-3)


def test_x_bracing_four_corners(sample_legs, sample_levels):
    """用例 2：X 交叉杆两半分别重锚到四个角点。"""
    # 节间 z 从 21900 到 24000
    # 左腿在 21900: -1224.5, 在 24000: -1089.0
    # 右腿在 21900:  1224.5, 在 24000:  1089.0
    # 斜杆 1 (左下到右上，微小绘图误差):
    d1 = (-1250.0, 21850.0, 1070.0, 24050.0)
    # 斜杆 2 (右下到左上，微小绘图误差):
    d2 = (1250.0, 21850.0, -1070.0, 24050.0)

    out = reanchor_diag_endpoints([d1, d2], sample_legs, sample_levels)
    assert len(out) == 2

    # d1 重锚到：左下 (-1224.5, 21900) -> 右上 (1089.0, 24000)
    assert math.isclose(out[0][1], 21900.0, abs_tol=1e-3)
    assert math.isclose(out[0][0], -1224.5, abs_tol=1.0)
    assert math.isclose(out[0][3], 24000.0, abs_tol=1e-3)
    assert math.isclose(out[0][2], 1089.0, abs_tol=1.0)

    # d2 重锚到：右下 (1224.5, 21900) -> 左上 (-1089.0, 24000)
    assert math.isclose(out[1][1], 21900.0, abs_tol=1e-3)
    assert math.isclose(out[1][0], 1224.5, abs_tol=1.0)
    assert math.isclose(out[1][3], 24000.0, abs_tol=1e-3)
    assert math.isclose(out[1][2], -1089.0, abs_tol=1.0)


def test_short_segment_untouched(sample_legs, sample_levels):
    """用例 3：短杆不动保护（小于 min_seg_len 保持原坐标）。"""
    short_seg = (-1500.0, 17050.0, -1350.0, 17200.0)  # len ≈ 212mm < 500mm
    out = reanchor_diag_endpoints([short_seg], sample_legs, sample_levels, min_seg_len=500.0)
    assert len(out) == 1
    assert out[0] == short_seg


def test_dict_input_and_metadata(sample_legs, sample_levels):
    """用例 4：dict 结构输入，保留原有字段并标记 geometry_origin='diag_synth'。"""
    dict_seg = {
        "start": (-1500.0, 17080.0),
        "end": (-30.0, 20650.0),
        "handle": "D_TEST_01",
        "layer": "0",
    }
    out = reanchor_diag_endpoints([dict_seg], sample_legs, sample_levels)
    assert len(out) == 1
    res = out[0]
    assert isinstance(res, dict)
    assert res["handle"] == "D_TEST_01"
    assert res["layer"] == "0"
    assert res["geometry_origin"] == "diag_synth"
    assert res.get("reanchored") is True
    assert math.isclose(res["start"][1], 17000.0, abs_tol=1e-3)
    assert math.isclose(res["end"][1], 20700.0, abs_tol=1e-3)


def test_fine_levels_exclusion(sample_legs):
    """用例 5：细层位排除（斜杆端点跳过细层位，锚定到有效结构层位）。"""
    # 包含细层位 18000
    levels = [17000.0, 18000.0, 19000.0]
    # 斜杆画线终点在 z=18600（离 18000 约 600mm，离 19000 约 400mm）
    seg = (-1540.5, 17000.0, 1400.0, 18600.0)

    # 若排除 fine_levels=[18000.0]
    out = reanchor_diag_endpoints([seg], sample_legs, levels, fine_levels=[18000.0])
    assert math.isclose(out[0][3], 19000.0, abs_tol=1e-3)
