"""工程追溯引擎的单元测试：重点验证「来源追溯 + 变更作废」。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from traceability.graph import ancestors, descendants, invalidate, stale_report
from traceability.io import load_model, save_model, validate_references
from traceability.model import (
    Component,
    Dimension,
    DimensionOrigin,
    EngineeringModel,
    SourceRef,
    SourceType,
    Staleness,
)


def build_model() -> EngineeringModel:
    """构造一个最小但完整的管线模型，便于测试。"""
    m = EngineeringModel(name="test-pipe", version="1")
    m.add_component(Component(id="c_pump", name="泵", kind="pump"))
    m.add_component(Component(id="c_pipe", name="管", kind="pipe"))
    m.add_dimension(Dimension(
        id="d_od", name="外径", value=114.3, unit="mm",
        origin=DimensionOrigin.ASSUMED,
        source=SourceRef(SourceType.ASSUMPTION, "工程师假设", confidence=0.6),
        applies_to="c_pipe",
    ))
    m.add_dimension(Dimension(
        id="d_flow", name="流量", value=30, unit="m3/h",
        origin=DimensionOrigin.DERIVED,
        source=SourceRef(SourceType.DERIVED, "由外径计算", confidence=0.8),
        applies_to="c_pump",
    ))
    # 依赖：流量由泵 + 外径派生
    m.depend("d_flow", "c_pump", "d_od")
    return m


class ModelTest(unittest.TestCase):
    def test_model_roundtrip(self):
        m = build_model()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.json"
            save_model(m, path)
            loaded = load_model(path)
        self.assertEqual(loaded.name, "test-pipe")
        self.assertEqual(loaded.dimensions["d_od"].origin, DimensionOrigin.ASSUMED)
        self.assertIn("d_od", loaded.dependencies["d_flow"])

    def test_validate_references_clean(self):
        m = build_model()
        self.assertEqual(validate_references(m), [])

    def test_validate_references_detects_broken(self):
        m = build_model()
        m.dimensions["d_od"].applies_to = "c_nonexistent"
        problems = validate_references(m)
        self.assertTrue(any("c_nonexistent" in p for p in problems))


class GraphTest(unittest.TestCase):
    def test_ancestors_and_descendants(self):
        m = build_model()
        self.assertEqual(ancestors(m, "d_flow"), {"c_pump", "d_od"})
        self.assertEqual(descendants(m, "d_od"), {"d_flow"})

    def test_invalidate_propagates(self):
        m = build_model()
        stale = invalidate(m, ["d_od"])
        self.assertIn("d_od", stale)
        self.assertIn("d_flow", stale)          # 下游自动作废
        self.assertNotIn("c_pump", stale)       # 上游不受影响
        self.assertEqual(m.staleness["d_flow"], Staleness.STALE)

    def test_stale_report(self):
        m = build_model()
        invalidate(m, ["d_od"])
        report = stale_report(m)
        self.assertIn("d_flow", report["dimensions"])

    def test_refresh_after_verify(self):
        m = build_model()
        invalidate(m, ["d_od"])
        m.refresh({"d_od", "d_flow"})
        self.assertEqual(m.staleness["d_flow"], Staleness.CURRENT)


if __name__ == "__main__":
    unittest.main()
