"""阶段 1.1'/1.3 验收：来源段门禁（enforce_source_segment_gate）。

覆盖（JC1 单塔修复计划 阶段1.4）：
    * 06 段杆连到 29m（04 段范围）→ fail-closed 剔除，报告计数
    * 段内杆保留；边界容差内的漂移杆保留（吸收 ≈734mm 累积漂移，非评测容差）
    * interface_bar=true 豁免；derived/helper 不参与门禁
    * 全部物理杆写 source_sheet / source_z_range / interface_bar 溯源属性
    * 违规杆的 connections / rules / dimensions 悬空引用同步清理
    * overlay 可覆盖 module_z_ranges
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.intake.tower_views import (  # noqa: E402
    DEFAULT_MODULE_Z_RANGES,
    enforce_source_segment_gate,
)
from traceability.model import (  # noqa: E402
    Component,
    Connection,
    Dimension,
    EngineeringModel,
    Rule,
    SourceRef,
    SourceType,
)

DXF = "35A1-JC1-06.dxf"


def _node(model, nid, z, x=0.0):
    model.add_component(Component(
        id=nid, name=nid, kind="tower_node",
        source=SourceRef(SourceType.DRAWING, DXF),
        properties={"view_type": "front", "x": x, "y": 0.0, "z": z,
                    "solve_status": "solved", "drawing_view": "35A1-JC1-06"},
    ))


def _bar(model, bid, fn, tn, *, geometry_class="recognized", **extra):
    model.add_component(Component(
        id=bid, name=bid, kind="tower_bar",
        source=SourceRef(SourceType.DRAWING, DXF, detail="view=front"),
        properties={"bar_id": bid, "from_node": fn, "to_node": tn,
                    "source_file": "35A1-JC1-06", "drawing_view": "35A1-JC1-06",
                    "geometry_class": geometry_class, **extra},
    ))


def _model():
    m = EngineeringModel(name="seg-gate-test")
    m.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, DXF),
        properties={"view_kinds": ["front"]},
    ))
    # z=12000/13000 在 06 段(11000-16000)内；z=29000 在 04 段（跨段污染）
    _node(m, "n_ok1", 12000.0)
    _node(m, "n_ok2", 13000.0)
    _node(m, "n_bad", 29000.0)
    _node(m, "n_drift", 16800.0)   # 16000+800：边界容差内（漂移）
    _node(m, "n_far", 17600.0)     # 16000+1600：超出容差（剔除）
    return m


class SegmentGateTest(unittest.TestCase):
    def test_cross_segment_bar_removed_in_range_bar_kept(self):
        m = _model()
        _bar(m, "bar_good", "n_ok1", "n_ok2")
        _bar(m, "bar_bad", "n_ok1", "n_bad")        # 06 → 29m 跨段
        report = enforce_source_segment_gate(m)
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["removed"], 1)
        self.assertIn("bar_bad", report["removed_ids"])
        self.assertNotIn("bar_bad", m.components)
        self.assertIn("bar_good", m.components)
        self.assertEqual(report["removed_by_sheet"], {"35A1-JC1-06": 1})

    def test_boundary_drift_within_tol_kept_beyond_removed(self):
        m = _model()
        _bar(m, "bar_drift", "n_ok1", "n_drift")    # 16800 ≤ 16000+1000 → 保留
        _bar(m, "bar_far", "n_ok2", "n_far")        # 17600 > 17000 → 剔除
        report = enforce_source_segment_gate(m)
        self.assertIn("bar_drift", m.components)
        self.assertNotIn("bar_far", m.components)
        self.assertEqual(report["removed"], 1)

    def test_interface_bar_exempt(self):
        m = _model()
        _bar(m, "bar_iface", "n_ok1", "n_bad", interface_bar=True)
        report = enforce_source_segment_gate(m)
        self.assertNotIn("bar_iface", report["removed_ids"])
        self.assertIn("bar_iface", m.components)
        self.assertEqual(report["kept_interface"], 1)

    def test_derived_bars_not_gated(self):
        m = _model()
        _bar(m, "bar_diag", "n_ok1", "n_bad", geometry_class="derived")
        report = enforce_source_segment_gate(m)
        self.assertIn("bar_diag", m.components)     # derived 不进 P/R 也不进门禁
        self.assertEqual(report["checked"], 0)

    def test_source_attributes_written(self):
        m = _model()
        _bar(m, "bar_a", "n_ok1", "n_ok2")
        enforce_source_segment_gate(m)
        p = m.components["bar_a"].properties
        self.assertEqual(p["source_sheet"], "35A1-JC1-06")
        self.assertEqual(p["source_z_range"], [11000.0, 16000.0])
        self.assertFalse(p["interface_bar"])

    def test_dangling_refs_cleaned(self):
        m = _model()
        _bar(m, "bar_bad", "n_ok1", "n_bad")
        m.add_connection(Connection(id="c1", from_component="bar_bad", to_component="n_ok1"))
        m.add_connection(Connection(id="c2", from_component="n_ok1", to_component="n_ok2"))
        m.add_rule(Rule(id="r1", name="len", applies_to=["bar_bad"],
                        description="len>0"))
        m.add_dimension(Dimension(id="d1", name="len", value=1000.0, unit="mm",
                                  applies_to="bar_bad"))
        enforce_source_segment_gate(m)
        self.assertNotIn("bar_bad", m.components)
        self.assertNotIn("c1", m.connections)       # 引用被删杆 → 清理
        self.assertIn("c2", m.connections)          # 正常引用 → 保留
        self.assertNotIn("r1", m.rules)
        self.assertNotIn("d1", m.dimensions)
        from traceability.io import validate_references
        self.assertEqual(validate_references(m), [])

    def test_overlay_overrides_ranges(self):
        m = _model()
        _bar(m, "bar_bad", "n_ok1", "n_bad")
        overlay = {"module_z_ranges": {"35A1-JC1-06": [0.0, 40000.0]}}
        report = enforce_source_segment_gate(m, overlay=overlay)
        self.assertEqual(report["removed"], 0)      # 覆盖后 29m 也在"段内"
        p = m.components["bar_bad"].properties
        self.assertEqual(p["source_z_range"], [0.0, 40000.0])

    def test_default_ranges_are_jc1_six_segments(self):
        self.assertEqual(DEFAULT_MODULE_Z_RANGES["35A1-JC1-40"], (0.0, 5500.0))
        self.assertEqual(DEFAULT_MODULE_Z_RANGES["35A1-JC1-02"], (30000.0, 36600.0))
        self.assertEqual(len(DEFAULT_MODULE_Z_RANGES), 6)


if __name__ == "__main__":
    unittest.main()
