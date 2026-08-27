"""测试阶段 1（intake）、阶段 3（harness）与交付导出。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from traceability.intake.dwg import extract_from_dxf, make_demo_dxf
from traceability.intake.ocr import extract_dimensions_from_image
from traceability.harness.harness import run_harness, summarize
from traceability.harness.validators import validate_pressure_rating
from traceability.export.exporters import export_cypher, export_gexf, export_report
from traceability.model import ValidationStatus


class IntakeTest(unittest.TestCase):
    def test_dxf_demo_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            dxf = make_demo_dxf(Path(d) / "demo.dxf")
            model = extract_from_dxf(dxf)
        self.assertGreater(len(model.components), 1)
        # 每个构件都有来源
        for c in model.components.values():
            self.assertIsNotNone(c.source)

    def test_scan_intake_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "scan.png"
            from PIL import Image
            Image.new("RGB", (100, 50), "white").save(img)
            model = extract_dimensions_from_image(str(img))
        self.assertIn("dim_placeholder_scan", model.dimensions)
        self.assertEqual(model.dimensions["dim_placeholder_scan"].origin.value, "placeholder")


class HarnessTest(unittest.TestCase):
    def test_builtin_validator_pending_on_missing_data(self):
        from traceability.model import Component, EngineeringModel
        m = EngineeringModel(name="t")
        m.add_component(Component(id="a", name="A", kind="pipe"))
        m.add_component(Component(id="b", name="B", kind="pipe"))
        m.add_connection(__import__("traceability.model", fromlist=["Connection"]).Connection(
            id="conn", from_component="a", to_component="b", rule_ids=["r_pressure_rating"]
        ))
        r = validate_pressure_rating(m, "r_pressure_rating")
        self.assertEqual(r.status, ValidationStatus.PENDING)  # 数据不足，不猜

    def test_harness_full(self):
        # 加载示例模型，跑 harness，应至少有一条规则能验证
        from traceability.io import load_model
        model = load_model("examples/pipe_network.json")
        results = run_harness(model)
        self.assertGreater(len(results), 0)
        report = summarize(results)
        self.assertIn("验证摘要", report)


class ExportTest(unittest.TestCase):
    def test_exports(self):
        from traceability.io import load_model
        model = load_model("examples/pipe_network.json")
        cypher = export_cypher(model)
        self.assertIn("MERGE", cypher)
        gexf = export_gexf(model)
        self.assertIn("<gexf", gexf)
        rep = export_report(model)
        self.assertIn("工程交付报告", rep)


if __name__ == "__main__":
    unittest.main()