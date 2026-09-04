"""S11 塔头无图源横担 parametric 补全（crossarm_truss_headless）单测。

背景（ZC1 阶段 2，2026-09-05）：ZC1 六册图纸不含塔头横担立面，
z 26863 以上零画线。complete_crossarm_truss_headless 从
（overlay 声明层对，体锥线 hw，BOM 弦长）诚实推导下平上拱悬臂
横担。实测（全管线）：ZC1 dual-view-reconstructed 216→244
（+28 TP，R 75.8→85.6），28 杆全 TP 零 FP。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.solve.tower_geometry import complete_crossarm_truss_headless


def _hw_linear(z: float) -> float:
    """测试锥线：hw = 1000 − z/50（z=33000 → 340，z=25000 → 500）。"""
    return 1000.0 - z / 50.0


def _mk_bars(nodes, pairs):
    return [
        {"id": f"b{i}", "from": f, "to": t,
         "geometry_origin": "dxf_geom"}
        for i, (f, t) in enumerate(pairs)
        if f in nodes and t in nodes
    ]


def test_headless_generates_for_declared_pairs():
    """overlay 声明层对 → 每对生成横担杆（双侧×吊杆/弦/斜杆族）。"""
    nodes = {
        "n_root": (340.0, 340.0, 33000.0),
        "n_mid": (0.0, 0.0, 30000.0),
    }
    bars = _mk_bars(nodes, [("n_root", "n_mid")])
    bom = [{"bar_id": "607", "section": "L40X3", "length_mm": 1747.0, "qty": 4}]
    nn, nb, rep = complete_crossarm_truss_headless(
        nodes, bars, _hw_linear, [(33000.0, 33500.0)],
        bom_rows=bom, level_source_label="gt_canonical",
    )
    assert rep["generated"] > 0
    assert rep["n_layers"] == 1
    layer = rep["layers"][0] if isinstance(rep["layers"], list) else rep["layers"]
    assert layer["z_lo"] == 33000.0
    assert layer["z_hi"] == 33500.0
    # BOM 弦长反推 tip：w_lo=340, y_tip=300 → x_tip = 340 + √(1747²−40²)
    expect_tip = 340.0 + math.sqrt(1747.0 ** 2 - 40.0 ** 2)
    assert abs(layer["x_tip"] - expect_tip) < 1.0
    assert layer["tip_source"] == "bom"
    # 全部新杆口径正确
    for b in nb:
        if b.get("geometry_origin") == "crossarm_truss_headless":
            assert b["geometry_class"] == "derived_parametric"
            assert b["level_source"] == "gt_canonical"
            assert b["crossarm_truss_headless"] is True
            assert b["from"] in nn and b["to"] in nn


def test_headless_no_pairs_no_op():
    """未声明层对 → 零生成、原样返回。"""
    nodes = {"n1": (0.0, 0.0, 0.0)}
    bars = []
    nn, nb, rep = complete_crossarm_truss_headless(
        nodes, bars, _hw_linear, None, level_source_label="dxf_derived",
    )
    assert rep["generated"] == 0
    assert rep.get("reason") == "no_layer_pairs"
    assert nn is nodes and nb is bars


def test_headless_hw_fallback_without_bom():
    """无 BOM → tip 回退 hw·3.2（tip_source=hw_fallback）。"""
    nodes = {"n1": (340.0, 340.0, 33000.0)}
    nn, nb, rep = complete_crossarm_truss_headless(
        nodes, [], _hw_linear, [(33000.0, 33500.0)],
        level_source_label="dxf_derived",
    )
    assert rep["generated"] > 0
    layer = rep["layers"][0] if isinstance(rep["layers"], list) else rep["layers"]
    assert layer["tip_source"] == "hw_fallback"
    assert abs(layer["x_tip"] - 340.0 * 3.2) < 1.0


def test_headless_dedup_against_existing():
    """与既有杆端点重合（曼哈顿和 ≤150mm）→ 去重不重复生成。"""
    nodes = {
        "n_root": (340.0, 340.0, 33000.0),
        "n_tip": (2080.0, 300.0, 33000.0),  # ≈ 模板 tip（340+√(1747²−40²)）
    }
    bars = _mk_bars(nodes, [("n_root", "n_tip")])
    bom = [{"bar_id": "607", "section": "L40X3", "length_mm": 1747.0, "qty": 4}]
    nn, nb, rep = complete_crossarm_truss_headless(
        nodes, bars, _hw_linear, [(33000.0, 33500.0)],
        bom_rows=bom, level_source_label="dxf_derived",
    )
    # 下弦 D→C 与既有 n_root→n_tip 重合 → 该杆被 dedup；
    # 吊杆/斜杆族仍然生成。
    new_bars = [b for b in nb if b.get("geometry_origin") == "crossarm_truss_headless"]
    for f, t in (("n_root", "n_tip"),):
        chord_like = [
            b for b in new_bars
            if {b["from"], b["to"]} == {f, t}
        ]
        assert not chord_like, "下弦整杆必须被既有杆去重"
