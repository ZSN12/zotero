"""R7 直读端点页内层位带吸附单测（阶段五任务二）。

背景（2026-09-04 ZC1 实测归因）：
  * 近失直读 FP（front 500mm 口径 cost 1000~4000）114 根 =
    44 刚性页窗平移（标定残差，纯图纸不可收敛）+ 37 单端错位 + 33 混合；
  * 长度截短不显著（len diff 中位 -105mm）——stitch 容差不是主因；
  * 阶梯结点/页窗网格吸附受窗标定残差污染（净 +1）；页内自洽聚类
    （同册直读端点 z 的 1D 带聚类）是唯一无绝对标定依赖的净化路径。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.solve.evidence_prune import (  # noqa: E402
    _apply_direct_snap_layers,
    apply_evidence_prune,
)


class _Node:
    def __init__(self, nid, x, y, z):
        self.id = nid
        self.kind = "tower_node"
        self.properties = {"x": x, "y": y, "z": z}


class _Bar:
    def __init__(self, bid, a, b, stem, cls="recognized"):
        self.id = bid
        self.kind = "tower_bar"
        self.source = {"source_type": "drawing", "reference": f"scope/{stem}.dxf"}
        self.properties = {
            "from_node": a, "to_node": b, "geometry_class": cls,
        }


class _Model:
    def __init__(self, comps):
        self.components = comps


def _make_model(stem, z_pairs, cls="recognized"):
    comps: dict = {}
    for i, (z1, z2) in enumerate(z_pairs):
        an, bn = f"N{stem}{i}a", f"N{stem}{i}b"
        comps[an] = _Node(an, 0, 0, z1)
        comps[bn] = _Node(bn, 100, 0, z2)
        comps[f"B{stem}{i}"] = _Bar(f"B{stem}{i}", an, bn, stem, cls)
    return _Model(comps)


class TestDirectSnapLayers:

    def test_outlier_endpoint_snapped_to_band(self):
        """离群端点（1000mm 外）吸附到层位带中心。"""
        # 带 1: z≈1000（4 端点），带 2: z≈2000（4 端点），离群 3500
        m = _make_model("T-05", [(1000, 2000), (1000, 2000), (1000, 3500), (2000, 2000)])
        rep = _apply_direct_snap_layers(m, {})
        assert rep["snapped_endpoints"] >= 1
        # 3500 端点被拉回带 2 中心 2000
        node = [c for c in m.components.values()
                if getattr(c, "kind", "") == "tower_node"
                and abs(c.properties["z"] - 3500) < 1e-9]
        assert not node, "离群端点应已被吸附（不再保持原 z）"

    def test_in_band_endpoints_untouched(self):
        """带内端点（距带中心 ≤ gap）不动。"""
        m = _make_model("T-05", [(1000, 2000), (1000, 2000), (1010, 1990), (1000, 2000)])
        before = {n.id: n.properties["z"] for n in m.components.values()
                  if getattr(n, "kind", "") == "tower_node"}
        rep = _apply_direct_snap_layers(m, {})
        after = {n.id: n.properties["z"] for n in m.components.values()
                 if getattr(n, "kind", "") == "tower_node"}
        assert before == after
        assert rep["snapped_endpoints"] == 0

    def test_only_recognized_bars_participate(self):
        """非 recognized（模板/推断）杆的端点不参与聚类与吸附。"""
        m = _make_model("T-05", [(1000, 2000), (1000, 2000), (1000, 2000),
                                 (1000, 2000)], cls="reconstructed")
        # 稀疏带（<4 端点/册）也应跳过；reconstructed 不进 by_sheet
        rep = _apply_direct_snap_layers(m, {})
        assert rep["snapped_endpoints"] == 0

    def test_min_band_count_guard(self):
        """端点太少（<4）无法成带——零行为。"""
        m = _make_model("T-05", [(1000, 2000), (3000, 4000)])
        rep = _apply_direct_snap_layers(m, {})
        assert rep["snapped_endpoints"] == 0

    def test_provenance_written(self):
        """被吸附端点所属杆写 snapped_by provenance。"""
        m = _make_model("T-05", [(1000, 2000), (1000, 2000), (1000, 5000), (2000, 2000)])
        _apply_direct_snap_layers(m, {})
        bars = [c for c in m.components.values()
                if getattr(c, "kind", "") == "tower_bar"]
        assert any(b.properties.get("snapped_by") == "R7_direct_snap_layers"
                   for b in bars)

    def test_disabled_by_default(self):
        """overlay 无 direct_snap_layers 键 → apply_evidence_prune 零行为。"""
        m = _make_model("T-05", [(1000, 2000), (1000, 5000), (1000, 2000), (2000, 2000)])
        rep = apply_evidence_prune(m, {"evidence_prune": {}})
        assert rep.get("enabled") is False
        assert "direct_snap_layers" not in rep

    def test_enabled_via_config(self):
        """evidence_prune.direct_snap_layers=true 走 R7。"""
        m = _make_model("T-05", [(1000, 2000), (1000, 2000), (1000, 5000), (2000, 2000)])
        rep = apply_evidence_prune(
            m, {"evidence_prune": {"direct_snap_layers": True}})
        assert rep["direct_snap_layers"]["snapped_endpoints"] >= 1
