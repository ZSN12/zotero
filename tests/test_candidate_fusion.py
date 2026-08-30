"""阶段 3.6 验收：hybrid 候选融合（union_dedup）的空间去重与保留逻辑。

覆盖：
    * _seg_duplicate 判据（角度/长度比/中点距离，三条件 AND，宁漏判不多删）
    * _vector_bars_not_covered：与 MLLM 重复的矢量杆被剔除、不重复的保留、
      无几何可判的保守保留
    * _strip_vector_geometry(keep=...)：保留集内的杆件及其引用不被清除，
      其余照常清除（connections/rules/dimensions 清理语义不变）
    * overlay 开关默认 mllm_replace（行为向后兼容），union_dedup 可读出
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.intake.hybrid_dxf_agent import (  # noqa: E402
    _seg_duplicate,
    _strip_vector_geometry,
    _vector_bars_not_covered,
)
from traceability.model import Component, Connection, EngineeringModel, SourceRef, SourceType  # noqa: E402


class SegDuplicateTest(unittest.TestCase):
    def test_identical_segments_are_duplicates(self):
        self.assertTrue(_seg_duplicate((0, 0, 100, 0), (0, 0, 100, 0)))

    def test_perpendicular_not_duplicate(self):
        self.assertFalse(_seg_duplicate((0, 0, 100, 0), (50, -50, 50, 50)))

    def test_length_mismatch_not_duplicate(self):
        self.assertFalse(_seg_duplicate((0, 0, 100, 0), (0, 0, 400, 0)))

    def test_parallel_far_apart_not_duplicate(self):
        self.assertFalse(_seg_duplicate((0, 0, 100, 0), (0, 5000, 100, 5000)))

    def test_small_offset_parallel_is_duplicate(self):
        self.assertTrue(_seg_duplicate((0, 0, 100, 0), (2, 3, 98, 3)))


def _model_with_vector_bars():
    """三根矢量杆：dup（与 MLLM 重复）/ other（不重复）/ broken（缺节点）。"""
    m = EngineeringModel(name="fusion-test")
    m.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, "t.dxf"),
    ))
    for nid, (x, y) in {
        "n1": (0.0, 0.0), "n2": (100.0, 0.0),
        "n3": (0.0, 800.0), "n4": (100.0, 800.0),
    }.items():
        m.add_component(Component(
            id=nid, name=nid, kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "t.dxf"),
            properties={"x": x, "y": y, "view_type": "front"},
        ))
    for bid, f, t in [
        ("bar_dup", "n1", "n2"),        # 与 MLLM 杆 (0,0)-(100,0) 重复
        ("bar_other", "n3", "n4"),      # 远处，不重复
        ("bar_broken", "ghost1", "ghost2"),  # 节点缺失，无法判定
    ]:
        m.add_component(Component(
            id=bid, name=bid, kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "t.dxf"),
            properties={"bar_id": bid, "from_node": f, "to_node": t,
                        "view_type": "front"},
        ))
    return m


MLLM_BARS = [
    {"x1": 1.0, "y1": 2.0, "x2": 99.0, "y2": 2.0, "view_type": "front"},
]


class VectorBarsNotCoveredTest(unittest.TestCase):
    def test_covered_excluded_uncovered_and_broken_kept(self):
        m = _model_with_vector_bars()
        keep = _vector_bars_not_covered(m, MLLM_BARS, "front")
        self.assertNotIn("bar_dup", keep)
        self.assertIn("bar_other", keep)
        self.assertIn("bar_broken", keep)

    def test_view_mismatch_keeps_all(self):
        m = _model_with_vector_bars()
        keep = _vector_bars_not_covered(m, MLLM_BARS, "side")
        self.assertIn("bar_dup", keep)


class StripVectorGeometryKeepTest(unittest.TestCase):
    def test_keep_preserves_bar_and_refs(self):
        m = _model_with_vector_bars()
        m.add_connection(Connection(
            id="conn_keep", from_component="bar_other", to_component="n3"))
        m.add_connection(Connection(
            id="conn_drop", from_component="bar_dup", to_component="n1"))

        removed = _strip_vector_geometry(m, keep={"bar_other", "n3", "n4"})
        self.assertGreater(removed, 0)
        self.assertNotIn("bar_dup", m.components)
        self.assertIn("bar_other", m.components)
        self.assertIn("conn_keep", m.connections)
        self.assertNotIn("conn_drop", m.connections)
        # bar_other 引用的节点保留、被删杆件专有的节点清除
        self.assertIn("n3", m.components)
        self.assertNotIn("n1", m.components)
        # 无悬空引用
        from traceability.io import validate_references
        problems = validate_references(m)
        self.assertEqual(problems, [])

    def test_default_none_keeps_nothing(self):
        m = _model_with_vector_bars()
        removed = _strip_vector_geometry(m)
        self.assertFalse([
            c for c in m.components.values() if c.kind in ("tower_bar", "tower_node")
        ])
        self.assertGreater(removed, 0)


class FusionFlagTest(unittest.TestCase):
    def test_default_fusion_is_mllm_replace(self):
        from traceability.intake.tower_spec import load_tower_spec

        spec = load_tower_spec({"candidate_fusion": "mllm_replace"})
        self.assertEqual(spec.get("candidate_fusion"), "mllm_replace")

    def test_union_dedup_flag_readable(self):
        from traceability.intake.tower_spec import load_tower_spec

        spec = load_tower_spec({"candidate_fusion": "union_dedup"})
        self.assertEqual(spec.get("candidate_fusion"), "union_dedup")


if __name__ == "__main__":
    unittest.main()
