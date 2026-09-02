# -*- coding: utf-8 -*-
"""P2.4j side_horiz_synth（侧立面横杆直读通道）单元测试。

体段侧立面横杆画线被通用管线节点聚类劈碎（96~367mm），本通道直读
长水平线（≥800mm）→ 双线合并 → z_anchors 层位映射 → 半宽深度吸附 →
side_reads 冻结表追加。全程零 GT 坐标注入（z 常数来自 overlay 层位表，
半宽锥由 front 腿节点拟合）。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest

from traceability.model import Component, EngineeringModel


# ---------------------------------------------------------------- 构造工具

def _mk_dxf(tmp_path: Path, lines, layers=("S-CSTR",)):
    """极简 DXF：指定 (x1,y1,x2,y2[,layer]) 画 LINE。"""
    import ezdxf
    doc = ezdxf.new("R2010")
    for ly in layers:
        doc.layers.add(ly)
    msp = doc.modelspace()
    for ln in lines:
        x1, y1, x2, y2 = ln[0], ln[1], ln[2], ln[3]
        layer = ln[4] if len(ln) > 4 else layers[0]
        msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer})
    p = tmp_path / "tower.dxf"
    doc.saveas(p)
    return p


def _mk_model_with_front_legs(z_levels, hw_bottom, hw_top):
    """front 视图腿节点 + 腿杆（供 taper 半宽拟合）。"""
    model = EngineeringModel(name="m")
    nodes = {}
    for i, z in enumerate(z_levels):
        hw = hw_bottom + (hw_top - hw_bottom) * i / max(1, len(z_levels) - 1)
        for sx, nid in ((-1, f"l{i}"), (1, f"r{i}")):
            nodes[nid] = (sx * hw, 0.0, float(z))
    for nid, (x, y, z) in nodes.items():
        model.add_component(Component(
            id=nid, name=nid, kind="tower_node", source=None,
            properties={"x": x, "y": y, "z": z, "node_id": nid,
                        "view_type": "front"},
        ))
    for side in ("l", "r"):
        sids = [f"{side}{i}" for i in range(len(z_levels))]
        for a, b in zip(sids, sids[1:]):
            model.add_component(Component(
                id=f"leg_{a}_{b}", name=f"leg_{a}_{b}", kind="tower_bar",
                source=None,
                properties={"from_node": a, "to_node": b, "view_type": "front"},
            ))
    # drawing_file 容器（side_reads 冻结表宿主）
    model.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file", source=None,
        properties={"path": "tower"},
    ))
    return model


def _mk_overlay(tmp_path: Path, dxf: Path, region, z_anchors, levels):
    ov = {
        "side_horiz_synth": True,
        "centerline_extract": {
            "tower": {
                "side_horiz_synth": True,
                "side_horiz_synth_region": {
                    "region": region,
                    "origin": [ (region[0] + region[1]) / 2.0, region[2] ],
                    "scale_x": 20.0,
                    "z_anchors": z_anchors,
                },
                "beam_marker_levels_mm": levels,
            }
        },
    }
    p = tmp_path / "overlay.json"
    p.write_text(json.dumps(ov), encoding="utf-8")
    return p


# ---------------------------------------------------------------- 测试用例

def test_full_span_band_splits_into_two_half_bars(tmp_path):
    """全跨画线（≈2×hw）→ 拆两根半跨 y_member 读取。"""
    from traceability.intake.tower_views import side_horiz_synth
    # z=19000 层：hw=1443（上下层位线性）。锚链 y -9802↔19000。
    # 全跨线：y -9802, x [34665,34797]（跨 132u = 2640mm ≈ 2×hw）
    dxf = _mk_dxf(tmp_path, [
        (34665, -9802, 34797, -9802),
        (34665, -9803, 34797, -9803),   # 双线（≤4u 平行对 → 合并）
    ])
    model = _mk_model_with_front_legs([18000, 19000, 20000], 1500, 1400)
    ovp = _mk_overlay(tmp_path, dxf, [34600, 34870, -9922, -9538],
                      [[-9868, 17000], [-9802, 19000], [-9736, 21000]],
                      [19000, 21000])
    n = side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    assert n == 2, f"全跨应拆 2 根半跨，实际 {n}"
    reads = model.components["drawing_file"].properties["side_reads"]
    zs = {r["from"][2] for r in reads[-2:]}
    assert zs == {19000.0}
    # 两根半跨：[-hw,0] 与 [0,+hw]
    spans = sorted((r["from"][1], r["to"][1]) for r in reads[-2:])
    hw = (1500 + 1400) / 2  # 19000 层位插值半宽
    assert abs(spans[0][0] + hw) < 1.0
    assert abs(spans[0][1]) < 0.01
    assert abs(spans[1][0]) < 0.01
    assert abs(spans[1][1] - hw) < 1.0


def test_half_span_band_direct_read(tmp_path):
    """半跨画线（≈hw，端点贴中心）→ 一根半跨读取。"""
    from traceability.intake.tower_views import side_horiz_synth
    # 半跨线：y -9802, x [34735,34795]（60u=1200mm≈hw），贴右侧
    ox = 34735.0  # origin x = 中心 → 深度 [0,1200]
    dxf = _mk_dxf(tmp_path, [
        (34735, -9802, 34795, -9802),
        (34735, -9803, 34795, -9803),
    ])
    model = _mk_model_with_front_legs([18000, 19000, 20000], 1500, 1400)
    ovp = _mk_overlay(
        tmp_path, dxf, [34600, 34870, -9922, -9538],
        [[-9868, 17000], [-9802, 19000], [-9736, 21000]],
        [19000, 21000])
    # origin 设为线左端 → 深度 [0, 1200]
    ov = json.loads(ovp.read_text(encoding="utf-8"))
    ov["centerline_extract"]["tower"]["side_horiz_synth_region"]["origin"] = \
        [ox, -9917.54]
    ovp.write_text(json.dumps(ov), encoding="utf-8")
    n = side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    assert n == 1, f"半跨应 1 根，实际 {n}"
    r = model.components["drawing_file"].properties["side_reads"][-1]
    assert r["from"][1] == 0.0
    assert r["to"][1] > 1000  # ≈ hw


def test_non_level_band_rejected(tmp_path):
    """非层位（snap 失败）水平线弃——标注线/尺寸线不进表。"""
    from traceability.intake.tower_views import side_horiz_synth
    # y -9736 锚定 21000，但线放 y -9700（→z~21650，离 21000 差 650>300）
    dxf = _mk_dxf(tmp_path, [
        (34665, -9700, 34797, -9700),
        (34665, -9701, 34797, -9701),
    ])
    model = _mk_model_with_front_legs([18000, 19000, 20000], 1500, 1400)
    ovp = _mk_overlay(tmp_path, dxf, [34600, 34870, -9922, -9538],
                      [[-9868, 17000], [-9802, 19000], [-9736, 21000]],
                      [19000, 21000])
    n = side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    assert n == 0, f"非层位线应弃，实际追加了 {n}"


def test_depth_mismatch_rejected(tmp_path):
    """跨度不匹配半宽拓扑的线弃（如尺寸线长度）。"""
    from traceability.intake.tower_views import side_horiz_synth
    # 奇怪跨度：50u=1000mm，既非 2×hw(~2886) 也非 hw(~1443)
    dxf = _mk_dxf(tmp_path, [
        (34685, -9802, 34735, -9802),
    ])
    model = _mk_model_with_front_legs([18000, 19000, 20000], 1500, 1400)
    ovp = _mk_overlay(tmp_path, dxf, [34600, 34870, -9922, -9538],
                      [[-9868, 17000], [-9802, 19000], [-9736, 21000]],
                      [19000, 21000])
    n = side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    assert n == 0, f"跨度不合拓扑应弃，实际 {n}"


def test_dedup_same_level_same_span(tmp_path):
    """同层位同跨度重复读取去重（多册/多线不重复进表）。"""
    from traceability.intake.tower_views import side_horiz_synth
    dxf = _mk_dxf(tmp_path, [
        (34665, -9802, 34797, -9802),
        (34665, -9803, 34797, -9803),
    ])
    model = _mk_model_with_front_legs([18000, 19000, 20000], 1500, 1400)
    ovp = _mk_overlay(tmp_path, dxf, [34600, 34870, -9922, -9538],
                      [[-9868, 17000], [-9802, 19000], [-9736, 21000]],
                      [19000, 21000])
    n1 = side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    n2 = side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    assert n1 == 2
    assert n2 == 0, "第二次调用应全部去重"


def test_off_by_default_without_overlay_flag(tmp_path):
    """overlay 未开 side_horiz_synth → 通道不动（默认关）。"""
    from traceability.intake.tower_views import side_horiz_synth
    dxf = _mk_dxf(tmp_path, [(34665, -9802, 34797, -9802)])
    model = _mk_model_with_front_legs([18000, 19000, 20000], 1500, 1400)
    ov = {
        "centerline_extract": {
            "tower": {
                "side_horiz_synth": True,
                "side_horiz_synth_region": {
                    "region": [34600, 34870, -9922, -9538],
                    "origin": [34731, -9917.54], "scale_x": 20.0,
                    "z_anchors": [[-9868, 17000], [-9802, 19000]],
                },
                "beam_marker_levels_mm": [19000],
            }
        },
    }
    p = tmp_path / "ov_off.json"
    p.write_text(json.dumps(ov), encoding="utf-8")
    n = side_horiz_synth(model, str(p), {"tower": str(dxf)})
    assert n == 0, "顶层开关未开应返回 0"


def test_per_stem_flag_required(tmp_path):
    """册级 side_horiz_synth 未开 → 该册跳过（防误启用）。"""
    from traceability.intake.tower_views import side_horiz_synth
    dxf = _mk_dxf(tmp_path, [
        (34665, -9802, 34797, -9802),
        (34665, -9803, 34797, -9803),
    ])
    model = _mk_model_with_front_legs([18000, 19000, 20000], 1500, 1400)
    ovp = _mk_overlay(tmp_path, dxf, [34600, 34870, -9922, -9538],
                      [[-9868, 17000], [-9802, 19000], [-9736, 21000]],
                      [19000, 21000])
    ov = json.loads(ovp.read_text(encoding="utf-8"))
    ov["side_horiz_synth"] = True  # 顶层开
    ov["centerline_extract"]["tower"]["side_horiz_synth"] = False  # 册级关
    ovp.write_text(json.dumps(ov), encoding="utf-8")
    n = side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    assert n == 0, "册级开关未开应返回 0"


def test_read_shape_conforms_to_side_reads_schema(tmp_path):
    """追加读取符合 side_reads 冻结表 schema（apply_side_reads 可消费）。"""
    from traceability.intake.tower_views import side_horiz_synth
    dxf = _mk_dxf(tmp_path, [
        (34665, -9802, 34797, -9802),
        (34665, -9803, 34797, -9803),
    ])
    model = _mk_model_with_front_legs([18000, 19000, 20000], 1500, 1400)
    ovp = _mk_overlay(tmp_path, dxf, [34600, 34870, -9922, -9538],
                      [[-9868, 17000], [-9802, 19000], [-9736, 21000]],
                      [19000, 21000])
    side_horiz_synth(model, str(ovp), {"tower": str(dxf)})
    r = model.components["drawing_file"].properties["side_reads"][-1]
    for k in ("from", "to", "x_source", "z_snapped", "source_file",
              "geometry_origin", "geometry_class", "source_extractor"):
        assert k in r, f"缺字段 {k}"
    assert r["x_source"] == "face_plane"
    assert r["z_snapped"] is True
    assert r["source_extractor"] == "centerline_extract"
    assert len(r["from"]) == 3 and len(r["to"]) == 3
