"""scripts/eval_segment_candidates.py 纯函数单测。

覆盖三个曾造成实测事故的回归点：
1. 反向重复线（同杆两图层各画一遍、方向相反）→ 不得产出零长中心线
   （06 册 X 撑整条消失的根因）；
2. 双线配对的中心线几何（平行偏移对 → 中线）；
3. 覆盖率口径：通长线覆盖节间子段、角度超差不覆盖；
4. 横杆合成：全断点对组合必须含「中心↔外腿」跨断点段
   （06/07 每层漏 4 根环杆投影的根因）。
"""
import importlib.util
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "eval_segment_candidates", REPO / "scripts" / "eval_segment_candidates.py")
esc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(esc)


def _ang_ok(a, b, tol=0.5):
    return abs(math.hypot(a[0] - b[0], a[1] - b[1])) < tol and \
        abs(math.hypot(a[2] - b[2], a[3] - b[3])) < tol


def test_pair_double_lines_reversed_duplicate_keeps_one():
    # 同一条 X 撑在两个图层各画一遍且方向相反（06 册实例）
    segs = [
        (34635.0, -10362.1, 34450.5, -10488.9, "4"),
        (34452.1, -10491.2, 34636.6, -10364.4, "1"),
    ]
    out = esc.pair_double_lines(segs)
    assert len(out) == 1, f"反向重复线应合并为一条，得到 {len(out)}"
    # 保留线方向与第一条一致（归一化）
    assert out[0][0] > out[0][2] and out[0][1] > out[0][3]
    assert abs(esc._len(out[0]) - 223.0) < 5.0


def test_pair_double_lines_parallel_offset_centerline():
    # 平行偏移对（腿双线角钢）→ 中心线
    segs = [
        (100.0, 0.0, 100.0, 200.0, "4"),
        (103.0, 0.0, 103.0, 200.0, "1"),
    ]
    out = esc.pair_double_lines(segs)
    assert len(out) == 1
    c = out[0]
    assert abs(c[0] - 101.5) < 0.1 and abs(c[2] - 101.5) < 0.1
    assert abs(c[1]) < 0.1 and abs(c[3] - 200.0) < 0.1


def test_pair_double_lines_no_false_pair_crossing():
    # 交叉线（角度不同）不得配对
    segs = [
        (0.0, 0.0, 100.0, 100.0, "0"),
        (0.0, 100.0, 100.0, 0.0, "0"),
    ]
    out = esc.pair_double_lines(segs)
    assert len(out) == 2


def test_coverage_match_long_line_covers_subsegment():
    # 通长线覆盖节间子段（覆盖率口径核心语义）
    cands = [(-2000.0, 13000.0, 2000.0, 15000.0)]
    gt = [(0.0, 13500.0, 1800.0, 14800.0)]  # 子段，角度接近
    hits, misses = esc.coverage_match(gt, cands, 500.0)
    assert len(hits) == 1, "通长线应覆盖子段"


def test_coverage_match_angle_mismatch():
    # 角度超差（>15°）不覆盖
    cands = [(-2000.0, 13000.0, 2000.0, 15000.0)]  # ~27°
    gt = [(0.0, 16000.0, 1852.0, 13000.0)]  # ~58° K 撑
    hits, _ = esc.coverage_match(gt, cands, 500.0)
    assert len(hits) == 0, "角度差 >15° 的 K 撑不应被 X 撑覆盖"


def test_coverage_match_endpoint_far():
    # 端点超 500mm 不覆盖
    cands = [(0.0, 14000.0, 1000.0, 14000.0)]
    gt = [(0.0, 14000.0, 1800.0, 14000.0)]  # 终点 800mm 外
    hits, _ = esc.coverage_match(gt, cands, 500.0)
    assert len(hits) == 0


def test_synth_beams_all_breakpoint_pairs():
    # 全断点对：必须含「中心↔外腿」跨断点段
    # 图纸域：塔中心 34500，内腿 ±40u、外腿 ±80u，x 比例 19mm/u
    leg_x = [34500 - 80.0, 34500 - 40.0, 34500 + 40.0, 34500 + 80.0]
    z_of_y = lambda y: y
    x_of_u = lambda u: (u - 34500.0) * 19.0
    cands = esc.synth_beams([100.0], leg_x, z_of_y, x_of_u, 34500.0)
    spans = {(round(c[0]), round(c[2])) for c in cands}
    # 中心↔外腿（0↔±1520）跨两断点的段必须存在（06/07 曾每层漏 4 根）
    assert (0, 1520) in spans, f"缺中心↔外腿段: {sorted(spans)}"
    # 内腿↔外腿（±760↔±1520）相邻段也要在
    assert (760, 1520) in spans
    # 中心↔内腿（0↔±760）
    assert (0, 760) in spans


def test_calibrate_filters_spurious_levels():
    # 5 个检出层位混入 3 个假层位，间距一致性过滤应挑出真层位
    marker_levels = [-10463.7, -10423.8, -10298.2, -10260.9, -10241.9]
    z_of_y = esc.calibrate(marker_levels, [14000.0, 16000.0], 300.0, 5000.0)
    assert z_of_y is not None
    assert abs(z_of_y(-10423.8) - 14000.0) < 1.0
    assert abs(z_of_y(-10298.2) - 16000.0) < 1.0
    # 假层位不得当锚点（若 -10463.7 被选为 14000 锚，斜率会错）
    assert abs(z_of_y(-10241.9) - 16300.0) > 500  # -10241.9 不是 16000 层


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all passed")
