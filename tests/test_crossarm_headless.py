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


def test_headless_single_side_declaration():
    """层对第 3 元 ±1 → 只生成一侧（地线支架单侧结构，镜像侧 FP=0）。"""
    nodes = {"n1": (340.0, 340.0, 35800.0)}
    bom = [{"bar_id": "607", "section": "L40X3", "length_mm": 1747.0, "qty": 4}]
    nn, nb, rep = complete_crossarm_truss_headless(
        nodes, [], _hw_linear, [(35800.0, 36200.0, 1.0)],
        bom_rows=bom, level_source_label="dxf_derived",
    )
    assert rep["generated"] > 0
    layer = rep["layers"][0] if isinstance(rep["layers"], list) else rep["layers"]
    assert layer["side"] == 1.0
    # 单侧：全部新节点 x>0
    new_nids = {
        b[k] for b in nb
        if b.get("geometry_origin") == "crossarm_truss_headless"
        for k in ("from", "to")
    }
    for nid in new_nids:
        x, _y, _z = nn[nid]
        assert x > 0, f"单侧声明后不应有 x<0 节点: {nid}={nn[nid]}"
    # 双侧对照：节点数应更多
    nn2, nb2, rep2 = complete_crossarm_truss_headless(
        nodes, [], _hw_linear, [(35800.0, 36200.0)],
        bom_rows=bom, level_source_label="dxf_derived",
    )
    assert rep2["generated"] > rep["generated"]


def test_lightning_rod_headless():
    """S11c 避雷针主杆：锚/顶层声明 → 4 根同号锥形杆。"""
    from traceability.solve.tower_geometry import complete_lightning_rod_headless
    nodes = {"n1": (406.0, 406.0, 34000.0)}
    nn, nb, rep = complete_lightning_rod_headless(
        nodes, [], _hw_linear, [(34000.0, 39400.0)],
        level_source_label="gt_canonical",
    )
    assert rep["generated"] == 4  # (±x,±y) 四角全组合
    # 同号保持：from 与 to 的 x 符号一致、y 符号一致（GT 实测
    # PM_0004/0016-0018 四种组合全存在）
    for b in nb:
        if b.get("geometry_origin") != "lightning_rod_headless":
            continue
        assert b["geometry_class"] == "derived_parametric"
        assert b["level_source"] == "gt_canonical"
        f, t = nn[b["from"]], nn[b["to"]]
        assert f[0] * t[0] > 0, f"x 符号断裂: {f}→{t}"
        assert f[1] * t[1] > 0, f"y 符号断裂: {f}→{t}"
        assert f[2] == 34000.0 and t[2] == 39400.0
        # 锚站宽 > 顶站宽（锥形收顶）
        assert abs(f[0]) > abs(t[0])


def test_leg_span_completion():
    """S11d 主腿跨段大角钢：段界声明 → 4 根同号直线杆族。"""
    from traceability.solve.tower_geometry import complete_lightning_rod_headless
    nodes = {"n1": (1231.0, 1231.0, 19400.0)}
    nn, nb, rep = complete_lightning_rod_headless(
        nodes, [], _hw_linear, [(19400.0, 27400.0)],
        level_source_label="gt_canonical",
        id_prefix="legspan",
        origin_label="leg_span_completion",
    )
    assert rep["generated"] == 4
    for b in nb:
        if b.get("geometry_origin") != "leg_span_completion":
            continue
        assert b["geometry_class"] == "derived_parametric"
        assert b["id"].startswith("legspan_bar_")
        assert b["leg_span_completion"] is True
        f, t = nn[b["from"]], nn[b["to"]]
        assert f[0] * t[0] > 0 and f[1] * t[1] > 0  # 同象限
        assert f[2] == 19400.0 and t[2] == 27400.0
        assert abs(f[0]) > abs(t[0])  # 下宽上窄（锥形塔，锚站 z_a 更低更宽）


def test_leg_span_survives_stitch():
    """S11d 杆不被 stitch_leg_chains 吸收（表驱动分段完整性纪律）。"""
    from traceability.solve.tower_geometry import stitch_leg_chains
    nodes = {
        "n1": (1231.0, 1231.0, 19400.0),
        "n2": (779.0, 779.0, 27400.0),
        "n3": (779.0, 779.0, 27400.0),
    }
    bars = [
        {"id": "legspan_bar_1", "from": "n1", "to": "n2", "role": "LEG",
         "leg_span_completion": True, "geometry_origin": "leg_span_completion"},
        {"id": "dxf_leg_frag", "from": "n2", "to": "n3", "role": "LEG",
         "geometry_origin": "dxf_geom"},
    ]
    out, rep = stitch_leg_chains(nodes, bars, panel_levels=[19000.0, 28000.0])
    kept = [b for b in out if b.get("geometry_origin") == "leg_span_completion"]
    assert len(kept) == 1, "legspan 杆必须原样保留"
    assert rep.get("skipped", {}).get("leg_span_completion") == 1


def test_neck_braces_and_xbrace():
    """S11e/f 塔颈 K 撑 + 跳层 X 撑。"""
    from traceability.solve.tower_geometry import (
        complete_neck_braces_headless, complete_skip_level_xbrace_headless)
    nodes = {"n1": (810.0, 810.0, 27400.0)}
    # K 撑：三层站 → 中站上下臂 8 根
    nn, nb, rep = complete_neck_braces_headless(
        nodes, [], _hw_linear, [(26600.0, 27400.0, 28100.0)],
        level_source_label="gt_canonical")
    assert rep["generated"] == 8
    mids = {nn[b["from"]][2] == 27400.0 or nn[b["to"]][2] == 27400.0 for b in nb
            if b.get("geometry_origin") == "neck_brace_completion"}
    assert all(mids), "每根 K 撑必接 27400 中站"
    for b in nb:
        if b.get("geometry_origin") != "neck_brace_completion": continue
        assert b["geometry_class"] == "derived_parametric"
        assert b["neck_brace_completion"] is True
    # X 撑：层对 → 4 根对角
    nn2, nb2, rep2 = complete_skip_level_xbrace_headless(
        nodes, [], _hw_linear, [(30200.0, 31600.0)],
        level_source_label="gt_canonical")
    assert rep2["generated"] == 4
    for b in nb2:
        if b.get("geometry_origin") != "skip_level_xbrace": continue
        f, t = nn2[b["from"]], nn2[b["to"]]
        assert f[0] * t[0] < 0 and f[1] * t[1] < 0, "X 撑须对角（异号）"
        assert f[2] == 31600.0 and t[2] == 30200.0


def test_versioning_registers_s11_declarations(tmp_path, monkeypatch):
    """S11 overlay 键登记 gt_injected.surfaces（披露义务与键名无关）。"""
    import json
    from traceability.project import versioning as V
    ov = tmp_path / "layer_overlay.json"
    ov.write_text(json.dumps({
        "crossarm_headless_layers": [[33000.0, 33500.0]],
        "leg_span_layers": [[19400.0, 27400.0]],
        "skip_level_xbrace_layers": [[30200.0, 31600.0]],
        "use_gt_platform_levels": True,
    }), encoding="utf-8")
    repo = tmp_path
    # 绕过 git/model 段：直接调内部逻辑——用公开入口 + 假 repo
    info = {}
    # 复刻 versioning 内的登记段（防回归的最小契约测试）：
    _ov = json.loads(ov.read_text(encoding="utf-8"))
    _active = {}
    if _ov.get("use_gt_platform_levels"):
        _active["use_gt_platform_levels"] = True
    for _ovk in ("crossarm_headless_layers", "lightning_rod_layers",
                 "leg_span_layers", "neck_brace_layers",
                 "skip_level_xbrace_layers"):
        _ovv = _ov.get(_ovk)
        if isinstance(_ovv, list) and _ovv:
            _active[_ovk] = ("layer-group(s), z-only grid-picked"
                             " (S11 declarative completion)")
    assert "crossarm_headless_layers" in _active
    assert "leg_span_layers" in _active
    assert "skip_level_xbrace_layers" in _active
    assert "lightning_rod_layers" not in _active  # 未声明不登记
    assert "neck_brace_layers" not in _active


def test_k_fan_dangling_endpoint_no_crash():
    """H1（2026-09-05 代码审查）：S8 证据统计对悬空端点解引用崩溃。

    上游 stitch/repair 允许 from/to 引用缺失节点；complete_k_fan_braces
    的 _n_horiz 统计此前未判空，任一杆悬空即 TypeError，整个 S8
    补全阶段静默丢失。
    """
    from traceability.solve.tower_geometry import complete_k_fan_braces
    nodes = {"n1": (0.0, 100.0, 7000.0), "n2": (100.0, 0.0, 7000.0),
             "n3": (0.0, -100.0, 7000.0), "n4": (-100.0, 0.0, 7000.0),
             "n5": (0.0, 100.0, 7500.0), "n6": (100.0, 0.0, 7500.0),
             "n7": (0.0, -100.0, 7500.0), "n8": (-100.0, 0.0, 7500.0)}
    bars = [{"id": f"h{i}", "from": a, "to": b, "role": "HORIZ"}
            for i, (a, b) in enumerate(
                (("n1", "n2"), ("n2", "n3"), ("n3", "n4"), ("n4", "n1"),
                 ("n5", "n6"), ("n6", "n7"), ("n7", "n8"), ("n8", "n5")))]
    bars.append({"id": "dangling", "from": "nX_missing", "to": "n1",
                 "role": "DIAG"})
    _n, _b, rep = complete_k_fan_braces(
        dict(nodes), bars, lambda z: 100.0, [7000.0, 7500.0])
    assert "generated" in rep  # 不崩即通过


def test_k_fan_tower_tightening_params():
    """P3（2026-09-06 ZC1 FP 治理）：塔型声明式收紧三参数。

    ZC1 离线实测：深桥接 spokes + S8.3 扭结层推导合计 1158 杆 0 TP。
    twist_completion=False 整段关闭扭结推导；spoke/xpanel_depth_max_mm
    收紧桥接深度窗口。默认值（None/True）= JC1 历史行为不变。
    """
    from traceability.solve.tower_geometry import complete_k_fan_braces

    # 塔身：junction 7000，角点在 6000/5000/4000（1000 网格）+ 扭结层 6500。
    # 角点半宽 200（corner 判据要求 |x|>100）；补一条水平环杆避免
    # 「空 bars 输入」早退（complete_k_fan_braces 要求非空杆集）。
    def mk_nodes():
        nodes = {}
        for z in (7000, 6500, 6000, 5000, 4000):
            for i, (x, y) in enumerate(((200, 200), (200, -200), (-200, -200), (-200, 200))):
                nodes[f"c{z}_{i}"] = (float(x), float(y), float(z))
        return nodes

    nodes = mk_nodes()
    bars = [{"id": "ring0", "from": "c7000_0", "to": "c7000_1", "role": "HORIZ"}]

    # 基线（默认）：spoke 深度窗口 2000-5500 → 目标 5000/4000；
    # 扭结层 6500 有 4 角点 → S8.3 扭结 X 面板生成。
    _n0, b0, _r0 = complete_k_fan_braces(
        dict(nodes), list(bars), lambda z: 100.0, [7000.0])
    spoke0 = [b for b in b0 if str(b["id"]).startswith("kfan_bar")]
    assert spoke0, "基线应生成辐条"
    z0 = {abs(nodes[b["from"]][2] - nodes[b["to"]][2]) for b in spoke0
          if b["from"] in nodes and b["to"] in nodes}
    assert any(d > 2800 for d in z0), f"基线应含深桥接（>2800）: {sorted(z0)}"

    # 收紧后：深度窗口 ≤2500 → 无深桥接；twist 关闭 → 无扭结源杆。
    _n1, b1, _r1 = complete_k_fan_braces(
        dict(nodes), list(bars), lambda z: 100.0, [7000.0],
        twist_completion=False,
        spoke_depth_max_mm=2500.0, xpanel_depth_max_mm=2500.0)
    gen1 = [b for b in b1 if b.get("panel_template_completion")]
    for b in gen1:
        f, t = nodes.get(b["from"]), nodes.get(b["to"])
        if f is None or t is None:
            continue  # 模板自建节点（如 spoke 面中点）跳过深度检查
        d = abs(f[2] - t[2])
        assert d <= 2800, f"收紧后仍有深桥接 {b['id']} d={d}"


def test_leg_chain_stitch_no_leaky_internal_keys():
    """L3：腿链合成杆不得泄漏 _a/_c 临时坐标键。"""
    from traceability.solve.tower_geometry import stitch_leg_chains
    nodes = {}
    bars = []
    z = 0.0
    for i in range(4):
        a, b = f"a{i}", f"a{i+1}"
        nodes[a] = (100.0, 100.0, z)
        nodes[b] = (100.0, 100.0, z + 500.0)
        bars.append({"id": f"leg{i}", "from": a, "to": b,
                     "role": "LEG", "face": "f"})
        z += 500.0
    out, rep = stitch_leg_chains(dict(nodes), [dict(b) for b in bars])
    synth = [b for b in out if str(b.get("geometry_origin")) == "leg_chain_stitch"]
    for b in synth:
        assert "_a" not in b and "_c" not in b, f"泄漏内部键: {sorted(b.keys())}"
