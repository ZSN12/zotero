# -*- coding: utf-8 -*-
"""subdivide_diag_at_levels（斜材层位打断，diag_synth 第一半）单元测试。

层位表与 05 册一致（z-only 设计常数，纪律允许）：
[17000, 18000, 19000, 19400, 20700, 21000, 21500, 21900, 22000, 22800, 24000]
"""

import math

import pytest

from traceability.intake.centerline_extract import subdivide_diag_at_levels

LEVELS = [17000, 18000, 19000, 19400, 20700, 21000, 21500, 21900, 22000,
          22800, 24000]


def _d(x1, z1, x2, z2, handle="H1", **kw):
    s = {"start": (x1, z1), "end": (x2, z2), "handle": handle}
    s.update(kw)
    return s


class TestLevelMemberGuard:
    """两端点都在层位上 → 层位杆/跨层连续杆，绝不错切。"""

    def test_single_panel_level_member_untouched(self):
        # 层位杆：17000 → 18000，端点恰在层位
        segs = [_d(-1500.0, 17000.0, -800.0, 18000.0)]
        out = subdivide_diag_at_levels(segs, LEVELS)
        assert out == segs  # 原样透传

    def test_pm0668_like_through_member_untouched(self):
        # PM_0668 型：z 17000→19400 的 2400mm 通长斜材，GT 里就是一根。
        # 虽然穿过 18000/19000 两个层位，但两端点都在层位上 → 不打断。
        segs = [_d(-1400.0, 17000.0, 1400.0, 19400.0)]
        out = subdivide_diag_at_levels(segs, LEVELS)
        assert len(out) == 1
        assert out[0] is segs[0]

    def test_both_endpoints_within_tol_untouched(self):
        # 端点距层位 ≤200mm 也算「落在层位」（画到节点板边缘的偏差）
        segs = [_d(-1400.0, 17150.0, 1400.0, 19220.0)]
        out = subdivide_diag_at_levels(segs, LEVELS)
        assert len(out) == 1


class TestThroughLineSplit:
    """端点不在层位的跨层通长线 → 在穿过的层位处参数化打断。"""

    def test_cross_five_levels_split_into_six(self):
        # 05 册典型：z 19786→22265 通长斜线（x 随 z 线性）。
        # 端点 19786/22265 均不在层位 ±200 内；穿过
        # 20700/21000/21500/21900/22000 五个层位 → 6 子段。
        x1, z1, x2, z2 = -1200.0, 19786.0, 1200.0, 22265.0
        segs = [_d(x1, z1, x2, z2, handle="L3555")]
        out = subdivide_diag_at_levels(segs, LEVELS)
        assert len(out) == 6
        # 子段端点：起点 → 各层位（x 沿杆插值）→ 终点
        crossed = [20700, 21000, 21500, 21900, 22000]
        exp_pts = [(x1, z1)]
        for lv in crossed:
            t = (lv - z1) / (z2 - z1)
            exp_pts.append((x1 + t * (x2 - x1), float(lv)))
        exp_pts.append((x2, z2))
        for j, child in enumerate(out):
            assert child["start"] == pytest.approx(exp_pts[j], abs=1e-6)
            assert child["end"] == pytest.approx(exp_pts[j + 1], abs=1e-6)
            # handle 溯源：#d{j} 后缀 + split_from
            assert child["handle"] == f"L3555#d{j}"
            assert child["split_from"] == "L3555"
            assert child["split_levels_z"] == [float(v) for v in crossed]
        # 子段 z 严格接力、无重叠无缝隙
        for a, b in zip(out, out[1:]):
            assert a["end"] == pytest.approx(b["start"], abs=1e-9)

    def test_cross_three_levels_split_into_four(self):
        # 一端在层位（19000）、一端不在（21250，距 21000/21500 均 >200）。
        # 严格落在杆内的层位：19400/20700/21000 三层（19400 距起点 400mm，
        # 超出 edge_clearance，也算穿过）→ 4 子段。
        segs = [_d(0.0, 19000.0, 900.0, 21250.0)]
        out = subdivide_diag_at_levels(segs, LEVELS)
        assert len(out) == 4
        zs = sorted(p[1] for c in out for p in (c["start"], c["end"]))
        assert zs == pytest.approx(
            [19000, 19400, 19400, 20700, 20700, 21000, 21000, 21250],
            abs=1e-6)
        # 首个打断层位（19400）的 x 沿杆线性插值
        t1 = (19400 - 19000) / (21250 - 19000)
        first = [c for c in out if abs(c["start"][1] - 19000) < 1e-6][0]
        assert first["end"] == pytest.approx((900.0 * t1, 19400.0), abs=1e-6)
        # 打断点 z 精确等于层位整值（无舍入漂移）
        split_zs = sorted({p[1] for c in out for p in (c["start"], c["end"])
                           if p[1] not in (19000.0, 21250.0)})
        assert split_zs == [19400.0, 20700.0, 21000.0]

    def test_cross_single_level_not_split(self):
        # 只跨 1 个层位（20700）→ 不打断（min_levels_crossed=2 防碎化）。
        # 端点 20300 距任何层位 >200；20950 距 21000 仅 50（≤200 也无关，
        # 因为 crossed 不足 2 就透传）。
        segs = [_d(0.0, 20300.0, 500.0, 20950.0)]
        out = subdivide_diag_at_levels(segs, LEVELS)
        assert out == segs

    def test_descending_z_line_splits_in_parametric_order(self):
        # 降 z 走向（z1 > z2）的通长线：打断点必须按沿杆参数 t 排序，
        # 否则子段回折重叠（05 册 D9/D24/D28 曾产出 z 21097→20700→21000
        # 的锯齿段）。z 21097→19601，穿过 20700/21000 → 3 子段。
        segs = [_d(-673.0, 21097.0, 751.0, 19601.0)]
        out = subdivide_diag_at_levels(segs, LEVELS)
        assert len(out) == 3
        # 子段沿杆接力：z 单调递减 21097 → 21000 → 20700 → 19601
        assert out[0]["start"] == pytest.approx((-673.0, 21097.0))
        assert out[0]["end"][1] == 21000.0
        assert out[1]["start"] == pytest.approx(out[0]["end"])
        assert out[1]["end"][1] == 20700.0
        assert out[2]["start"] == pytest.approx(out[1]["end"])
        assert out[2]["end"] == pytest.approx((751.0, 19601.0))
        # 每个子段 z 单调、无回折
        for c in out:
            assert c["start"][1] > c["end"][1]


class TestGuards:
    def test_vertical_and_horizontal_untouched(self):
        # 竖直主材/水平横杆不是本函数职责（避免越权打断）
        segs = [
            _d(-1500.0, 19500.0, -1500.0, 22300.0, handle="V"),  # 竖直
            _d(-1500.0, 20000.0, 1500.0, 20000.0, handle="H"),   # 水平
        ]
        out = subdivide_diag_at_levels(segs, LEVELS)
        assert out == segs

    def test_tuple_in_tuple_out(self):
        # 4 元组形态：输出保持 4 元组
        segs = [(-1200.0, 19786.0, 1200.0, 22265.0)]
        out = subdivide_diag_at_levels(segs, LEVELS)
        assert len(out) == 6
        assert all(isinstance(s, tuple) and len(s) == 4 for s in out)
        assert out[0][:2] == pytest.approx((-1200.0, 19786.0))
        assert out[-1][2:] == pytest.approx((1200.0, 22265.0))

    def test_empty_and_no_levels(self):
        assert subdivide_diag_at_levels([], LEVELS) == []
        seg = _d(0.0, 19786.0, 900.0, 22265.0)
        assert subdivide_diag_at_levels([seg], []) == [seg]

    def test_total_length_preserved(self):
        # 打断前后总长守恒（参数化打断不引入几何漂移）
        x1, z1, x2, z2 = -1200.0, 19786.0, 1200.0, 22265.0
        segs = [_d(x1, z1, x2, z2)]
        out = subdivide_diag_at_levels(segs, LEVELS)
        total = sum(math.hypot(c["end"][0] - c["start"][0],
                               c["end"][1] - c["start"][1]) for c in out)
        assert total == pytest.approx(math.hypot(x2 - x1, z2 - z1), rel=1e-9)
