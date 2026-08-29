"""比例尺自动标定模块单元测试。

覆盖：
1. 大尺寸（1:100）与小尺寸节点板（1:20）共存时，准确识别主视图 scale=100；
2. 横向 / 竖向独立标定；
3. 无样本、非数字文本、零距离等平滑兜底；
4. 空 regions / 全图 region 匹配。
"""

from __future__ import annotations

import ezdxf

from traceability.intake.scale_calibration import (
    DimSample,
    extract_dim_samples,
    calibrate_region_scales,
)


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def _sample(text_value, measured_distance, dx=None, dy=None, midpoint=None):
    """构造一个 DimSample。dx/dy 默认按给定距离构造，便于控制方向。"""
    if dx is None and dy is None:
        # 默认横向
        dx, dy = measured_distance, 0.0
    if midpoint is None:
        midpoint = (dx / 2.0, dy / 2.0) if (dx is not None and dy is not None) else (0.0, 0.0)
    return DimSample(
        text_value=float(text_value),
        measured_distance=float(measured_distance),
        dx=float(dx),
        dy=float(dy),
        midpoint=(float(midpoint[0]), float(midpoint[1])),
    )


def _region(x0, x1, y0, y1, **extra):
    d = {"region": [x0, x1, y0, y1], "scale_x": 20.0, "scale_y": 20.0}
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# 1. 大尺寸(1:100) 与 小尺寸节点板(1:20) 共存 → 识别主视图 100
# ---------------------------------------------------------------------------

def test_main_view_100_overrides_gusset_20():
    # 主视图：3 条大尺寸横向标注，文字 5800/1900/1212，实测距离对应 scale≈100
    main = [
        _sample(5800.0, 58.0, dx=58.0, dy=0.0, midpoint=(30, 0)),
        _sample(1900.0, 19.0, dx=19.0, dy=0.0, midpoint=(60, 0)),
        _sample(1212.0, 12.12, dx=12.12, dy=0.0, midpoint=(90, 0)),
    ]
    # 节点板局部大样：文字 380/400，实测距离对应 scale≈20
    gusset = [
        _sample(380.0, 19.0, dx=19.0, dy=0.0, midpoint=(200, 0)),
        _sample(400.0, 20.0, dx=20.0, dy=0.0, midpoint=(220, 0)),
    ]
    samples = main + gusset
    regions = [_region(-1000, 1000, -1000, 1000)]

    result = calibrate_region_scales(samples, regions)

    assert len(result) == 1
    r = result[0]
    assert r["scale_x"] == 100.0
    assert r["scale_y"] == 20.0  # 竖向无样本 → 保持 overlay 原值
    assert r["_scale_x_calibrated"] is True
    assert "_scale_y_calibrated" not in r
    assert r["_scale_origin"] == "dimension_calibration"


def test_main_view_100_scale_x_only_when_no_vertical():
    # 仅有横向大尺寸标注时，只标定 scale_x，scale_y 保持原值
    samples = [
        _sample(5800.0, 58.0, dx=58.0, dy=0.0, midpoint=(30, 0)),
        _sample(1900.0, 19.0, dx=19.0, dy=0.0, midpoint=(60, 0)),
    ]
    regions = [_region(-100, 100, -100, 100)]

    result = calibrate_region_scales(samples, regions)

    r = result[0]
    assert r["scale_x"] == 100.0
    assert r["_scale_x_calibrated"] is True
    assert r["scale_y"] == 20.0  # 未标定，保持 overlay 原值
    assert "_scale_y_calibrated" not in r


# ---------------------------------------------------------------------------
# 2. 横向 / 竖向独立标定
# ---------------------------------------------------------------------------

def test_independent_x_y_scales():
    # 横向 scale≈100，竖向 scale≈50，应分别标定
    samples = [
        # 横向
        _sample(5800.0, 58.0, dx=58.0, dy=0.0, midpoint=(30, 10)),
        _sample(1900.0, 19.0, dx=19.0, dy=0.0, midpoint=(60, 10)),
        # 竖向
        _sample(5000.0, 100.0, dx=0.0, dy=100.0, midpoint=(30, 200)),
        _sample(2500.0, 50.0, dx=0.0, dy=50.0, midpoint=(60, 200)),
    ]
    regions = [_region(-1000, 1000, -1000, 1000)]

    result = calibrate_region_scales(samples, regions)

    r = result[0]
    assert r["scale_x"] == 100.0
    assert r["scale_y"] == 50.0
    assert r["_scale_x_calibrated"] is True
    assert r["_scale_y_calibrated"] is True


def test_direction_split_diagonal():
    # 对角线样本：|dx| > |dy| 归横向，|dy| >= |dx| 归竖向
    h = DimSample(text_value=100.0, measured_distance=10.0,
                  dx=8.0, dy=6.0, midpoint=(0, 0))   # |dx|>|dy| → 横向
    v = DimSample(text_value=100.0, measured_distance=10.0,
                  dx=6.0, dy=8.0, midpoint=(0, 0))   # |dy|>|dx| → 竖向
    regions = [_region(-10, 10, -10, 10)]
    # 横向与竖向都各自只有一个样本，scale 各 10
    result = calibrate_region_scales([h, v], regions)
    r = result[0]
    assert r["scale_x"] == 10.0
    assert r["scale_y"] == 10.0


# ---------------------------------------------------------------------------
# 3. 平滑兜底：无样本 / 非数字 / 零距离
# ---------------------------------------------------------------------------

def test_no_samples_keeps_region():
    regions = [_region(0, 10, 0, 10)]
    result = calibrate_region_scales([], regions)
    assert result[0]["scale_x"] == 20.0
    assert result[0]["scale_y"] == 20.0
    assert "_scale_origin" not in result[0]


def test_no_samples_in_region_keeps_region():
    samples = [_sample(5800.0, 58.0, dx=58.0, dy=0.0, midpoint=(500, 500))]
    regions = [_region(0, 10, 0, 10)]  # sample 在区域外
    result = calibrate_region_scales(samples, regions)
    assert result[0]["scale_x"] == 20.0
    assert "_scale_origin" not in result[0]


def test_empty_regions_returns_empty():
    result = calibrate_region_scales([], [])
    assert result == []
    result2 = calibrate_region_scales(
        [_sample(5800.0, 58.0, dx=58.0, dy=0.0, midpoint=(0, 0))], [])
    assert result2 == []


def test_extract_skips_non_numeric_text():
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    msp.add_linear_dim(base=(0, 0), p1=(0, 0), p2=(10, 0), text="ABC").render()
    msp.add_linear_dim(base=(0, 20), p1=(0, 20), p2=(10, 20), text="").render()
    samples = extract_dim_samples(msp)
    assert samples == []


def test_extract_skips_zero_distance():
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    # p1 == p2 → defpoint2/defpoint3 重合 → 距离 0
    msp.add_linear_dim(base=(0, 0), p1=(5, 5), p2=(5, 5), text="100").render()
    samples = extract_dim_samples(msp)
    assert samples == []


def test_extract_valid_dimension():
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    msp.add_linear_dim(base=(0, 0), p1=(0, 0), p2=(58.0, 0), text="5800").render()
    samples = extract_dim_samples(msp)
    assert len(samples) == 1
    s = samples[0]
    assert s.text_value == 5800.0
    assert abs(s.measured_distance - 58.0) < 1e-6
    assert s.dx == 58.0
    assert s.dy == 0.0


def test_extract_tolerates_non_numeric_text_then_valid():
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    msp.add_linear_dim(base=(0, 0), p1=(0, 0), p2=(10, 0), text="N/A").render()
    msp.add_linear_dim(base=(0, 20), p1=(0, 20), p2=(19.0, 20), text="1900").render()
    samples = extract_dim_samples(msp)
    assert len(samples) == 1
    assert samples[0].text_value == 1900.0


def test_parse_text_variants():
    from traceability.intake.scale_calibration import _parse_text
    assert _parse_text("5800") == 5800.0
    assert _parse_text("1212.5") == 1212.5
    assert _parse_text("1,212") == 1212.0
    assert _parse_text("1 900") == 1900.0
    assert _parse_text("1900mm") == 1900.0
    assert _parse_text("") is None
    assert _parse_text(None) is None
    assert _parse_text("ABC") is None


# ---------------------------------------------------------------------------
# 4. 空 regions / 全图 region 匹配
# ---------------------------------------------------------------------------

def test_whole_figure_region_matches_all():
    samples = [
        _sample(5800.0, 58.0, dx=58.0, dy=0.0, midpoint=(30, 0)),
        _sample(1900.0, 19.0, dx=19.0, dy=0.0, midpoint=(60, 0)),
    ]
    # region 覆盖全部样本
    regions = [_region(-1000, 1000, -1000, 1000)]
    result = calibrate_region_scales(samples, regions)
    assert result[0]["scale_x"] == 100.0


def test_region_without_region_key_matches_all():
    # 缺失 region 键 → 视为全图匹配（防御）
    samples = [_sample(5800.0, 58.0, dx=58.0, dy=0.0, midpoint=(30, 0))]
    regions = [{"scale_x": 20.0, "scale_y": 20.0}]
    result = calibrate_region_scales(samples, regions)
    assert result[0]["scale_x"] == 100.0


def test_does_not_mutate_input_regions():
    regions = [_region(0, 10, 0, 10)]
    original = dict(regions[0])
    samples = [_sample(5800.0, 58.0, dx=58.0, dy=0.0, midpoint=(5, 5))]
    result = calibrate_region_scales(samples, regions)
    # 原 regions 未变
    assert regions[0]["scale_x"] == 20.0
    assert "_scale_origin" not in regions[0]
    # 结果 region 是独立拷贝
    assert result[0]["scale_x"] == 100.0
    assert result[0] is not regions[0]


def test_multiple_regions_selective():
    # 两个 region，样本只落在第一个
    samples = [
        _sample(5800.0, 58.0, dx=58.0, dy=0.0, midpoint=(5, 5)),
    ]
    regions = [
        _region(0, 10, 0, 10),
        _region(100, 200, 100, 200),
    ]
    result = calibrate_region_scales(samples, regions)
    assert result[0]["scale_x"] == 100.0
    assert result[1]["scale_x"] == 20.0  # 无样本 → 保持原值
    assert "_scale_origin" not in result[1]


def test_gusset_only_region_yields_20():
    # 若某 region 只有节点板大样（小文字值），应标定为 20 而非 100
    samples = [
        _sample(380.0, 19.0, dx=19.0, dy=0.0, midpoint=(5, 5)),
        _sample(400.0, 20.0, dx=20.0, dy=0.0, midpoint=(7, 5)),
    ]
    regions = [_region(0, 10, 0, 10)]
    result = calibrate_region_scales(samples, regions)
    assert result[0]["scale_x"] == 20.0
