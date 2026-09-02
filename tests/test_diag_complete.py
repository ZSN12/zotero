# -*- coding: utf-8 -*-
"""单元测试：截断斜杆补全 complete_truncated_diags (mm 域)。

验证：
1. 单截断线延长补全（好端吸附腿节点，坏端沿射线延长交对面腿并吸附最近层位）
2. X 成对补全（面板内双向截断线成对补全两根完整对角线）
3. 无好端不补（两端皆悬空的孤立线段不生成补全杆）
4. 坏端离网格太近不重复补（两端均在网格容差内，由 reanchor 处理，本算子不重复补）
5. dict 结构输入支持与 metadata 标记 (geometry_origin, geometry_class, source_extractor 等)
6. 细层位排除保护 (fine_levels 不作为对角线端点层位)
7. 空输入安全退化
"""

import math
import pytest
from traceability.intake.centerline_extract import complete_truncated_diags


@pytest.fixture
def sample_legs():
    # 典型主腿：从 z=17000 到 24000
    # 左腿：x = 0.06454 * z - 2637 (z=17000 时 x=-1540.5, z=19400 时 x=-1384.9, z=24000 时 x=-1089.0)
    # 右腿：x = -0.06454 * z + 2637 (z=17000 时 x=1540.5, z=19400 时 x=1384.9, z=24000 时 x=1089.0)
    l_leg = (-1540.5, 17000.0, -1089.0, 24000.0)
    r_leg = (1540.5, 17000.0, 1089.0, 24000.0)
    return [l_leg, r_leg]


@pytest.fixture
def sample_levels():
    return [17000.0, 18000.0, 19000.0, 19400.0, 20700.0, 21900.0, 24000.0]


def test_single_truncated_diag_extension(sample_legs, sample_levels):
    """用例 1：单截断线延长补全。
    起点靠近左腿×17000 (-1540.0, 17020.0)，终点悬空在 (-800.0, 17620.0)。
    沿线方向延长应与右腿交于 z≈19390，吸附到层位 19400。
    输出 1 根完整对角线 (-1540.5, 17000) -> (1384.9, 19400)。
    """
    trunc = (-1540.0, 17020.0, -800.0, 17620.0)
    out = complete_truncated_diags([trunc], sample_legs, sample_levels, pair_x=False)
    assert len(out) == 1
    x1, z1, x2, z2 = out[0]
    assert math.isclose(z1, 17000.0, abs_tol=1e-3)
    assert math.isclose(x1, -1540.5, abs_tol=1.0)
    assert math.isclose(z2, 19400.0, abs_tol=1e-3)
    assert math.isclose(x2, 1384.9, abs_tol=1.0)


def test_x_bracing_pair_completion(sample_legs, sample_levels):
    """用例 2：X 成对补全。
    面板 17000-19400 内存在两根截断斜杆：
    d1: 左下到中心 (-1540.0, 17020.0) -> (-800.0, 17620.0) (dx/dz > 0)
    d2: 右下到中心 ( 1540.0, 17020.0) -> ( 800.0, 17620.0) (dx/dz < 0)
    成对补全应输出该面板的两根完整 X 对角线。
    """
    d1 = (-1540.0, 17020.0, -800.0, 17620.0)
    d2 = (1540.0, 17020.0, 800.0, 17620.0)
    out = complete_truncated_diags([d1, d2], sample_legs, sample_levels, pair_x=True)
    assert len(out) == 2

    # 排序以方便断言：第一根左下到右上，第二根右下到左上
    out.sort(key=lambda s: s[0])
    # 杆 1: (-1540.5, 17000) -> (1384.9, 19400)
    assert math.isclose(out[0][1], 17000.0, abs_tol=1e-3)
    assert math.isclose(out[0][0], -1540.5, abs_tol=1.0)
    assert math.isclose(out[0][3], 19400.0, abs_tol=1e-3)
    assert math.isclose(out[0][2], 1384.9, abs_tol=1.0)

    # 杆 2: (-1384.9, 19400) -> (1540.5, 17000) (规整化后 z1=17000, x1=1540.5)
    # 注意 canonical 规整化 (z1 < z2): (1540.5, 17000) -> (-1384.9, 19400)
    assert math.isclose(out[1][1], 17000.0, abs_tol=1e-3)
    assert math.isclose(out[1][0], 1540.5, abs_tol=1.0)
    assert math.isclose(out[1][3], 19400.0, abs_tol=1e-3)
    assert math.isclose(out[1][2], -1384.9, abs_tol=1.0)


def test_x_bracing_pair_with_both_ends_bad_companion(sample_legs, sample_levels):
    """用例 2b：X 成对补全中伴随杆两端皆截断。
    d1 有好端在左腿×17000，d2 悬空在面板中间（两端皆截断）但斜率为负（反向画线证据）。
    因面板内存在反向证据，pair_x 应成对补全两根 X 撑。
    """
    d1 = (-1540.0, 17020.0, -800.0, 17620.0)
    d2 = (800.0, 17620.0, 200.0, 18220.0)  # slope = -600/600 = -1.0
    out = complete_truncated_diags([d1, d2], sample_legs, sample_levels, pair_x=True)
    assert len(out) == 2


def test_no_good_end_untouched(sample_legs, sample_levels):
    """用例 3：无好端不补。
    线段两端皆远离腿×层位网格节点（距离均 > 400mm），无确定性锚固基准，不作补全。
    """
    floating = (-500.0, 18000.0, 500.0, 18800.0)
    out = complete_truncated_diags([floating], sample_legs, sample_levels)
    assert len(out) == 0


def test_bad_end_too_close_untouched(sample_legs, sample_levels):
    """用例 4：坏端离网格太近不重复补。
    线段两端均已在网格节点容差内（<300mm），属于已有 reanchor 的处理范畴，
    不属于截断线，complete_truncated_diags 保持幂等不重复补。
    """
    nearly_complete = (-1530.0, 17020.0, 1375.0, 19380.0)
    out = complete_truncated_diags([nearly_complete], sample_legs, sample_levels)
    assert len(out) == 0


def test_dict_input_and_metadata(sample_legs, sample_levels):
    """用例 5：dict 输入支持及通道 metadata 标记。"""
    seg_dict = {
        "start": (-1540.0, 17020.0),
        "end": (-800.0, 17620.0),
        "layer": "4",
        "handle": "D_TRUNC_01",
    }
    out = complete_truncated_diags([seg_dict], sample_legs, sample_levels, pair_x=False)
    assert len(out) == 1
    res = out[0]
    assert isinstance(res, dict)
    assert res["geometry_origin"] == "diag_complete"
    assert res["geometry_class"] == "recognized"
    assert res["evidence_status"] == "reconstructed"
    assert res["source_extractor"] == "centerline_extract"
    assert math.isclose(res["start"][1], 17000.0, abs_tol=1e-3)
    assert math.isclose(res["end"][1], 19400.0, abs_tol=1e-3)


def test_fine_levels_exclusion(sample_legs):
    """用例 6：细层位排除保护。
    若某层位在 fine_levels 中声明，不作为吸附目标。
    """
    levels = [17000.0, 18000.0, 19400.0, 24000.0]
    # 假设 19400 被设为 fine_level，射线交点 19390 应避开 19400，吸附到 18000 或 24000
    trunc = (-1540.0, 17020.0, -800.0, 17620.0)
    out = complete_truncated_diags(
        [trunc], sample_legs, levels, fine_levels=[19400.0], pair_x=False, max_snap_err=1500.0
    )
    if out:
        assert not math.isclose(out[0][3], 19400.0, abs_tol=1e-3)


def test_empty_inputs():
    """用例 7：空输入安全退化。"""
    assert complete_truncated_diags([], [], []) == []
    assert complete_truncated_diags([(-1000, 17000, 0, 18000)], [], [17000, 18000]) == []
    assert complete_truncated_diags([(-1000, 17000, 0, 18000)], [(-1000, 17000, -800, 24000), (1000, 17000, 800, 24000)], []) == []
