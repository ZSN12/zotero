"""Phase 4 扫描图最小可用版测试。

覆盖：版面分析 / 霍夫线检测 / 端点聚类 / placeholder 原则 /
CLI 两个入口（intake-scan --tower / compile-drawing --tower）。

扫描图产出是 pixel 坐标候选（confidence ≤ 0.6），不进终版 3D。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _cv2_available():
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_cv2_available(), "opencv-python 未安装")
@pytest.mark.slow
class TowerScanTest(unittest.TestCase):
    def setUp(self):
        self.front = EXAMPLES / "clear" / "tower_front_hd.png"

    def test_analyze_produces_candidates(self):
        from traceability.intake.tower_layout import analyze_tower_scan
        model = analyze_tower_scan(self.front)
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        nodes = [c for c in model.components.values() if c.kind == "tower_node"]
        regions = [c for c in model.components.values() if c.kind == "scan_region"]
        self.assertGreater(len(bars), 0)
        self.assertGreater(len(nodes), 0)
        self.assertGreater(len(regions), 0)

    def test_confidence_capped_and_sources_present(self):
        from traceability.intake.tower_layout import analyze_tower_scan
        model = analyze_tower_scan(self.front)
        for c in model.components.values():
            if c.kind in ("tower_bar", "tower_node", "scan_region"):
                self.assertIsNotNone(c.source)
                self.assertLessEqual(c.source.confidence, 0.6)

    def test_placeholder_dimension_present(self):
        from traceability.intake.tower_layout import analyze_tower_scan
        from traceability.model import DimensionOrigin
        model = analyze_tower_scan(self.front)
        dim = model.dimensions.get("dim_placeholder_scan")
        self.assertIsNotNone(dim)
        self.assertEqual(dim.origin, DimensionOrigin.PLACEHOLDER)
        self.assertIsNone(dim.value)

    def test_roundtrip_and_reference_integrity(self):
        from traceability.intake.tower_layout import analyze_tower_scan
        from traceability.io import save_model, load_model, validate_references
        model = analyze_tower_scan(self.front)
        self.assertEqual(validate_references(model), [])
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "scan.json"
            save_model(model, out)
            loaded = load_model(out)
            self.assertEqual(len(loaded.components), len(model.components))

    def test_cli_intake_scan_tower(self):
        from traceability.cli import main
        from traceability.io import load_model
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "scan.json"
            main(["intake-scan", str(self.front), "--tower", "--out", str(out)])
            model = load_model(out)
            self.assertGreater(
                sum(1 for c in model.components.values() if c.kind == "tower_bar"), 0)

    def test_cli_compile_drawing_tower_scan(self):
        from traceability.cli import main
        from traceability.io import load_model
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "compiled.json"
            # 强制 rule-based-scan 确定性后端，隔离宿主 MLLM API key 干扰，
            # 避免 MLLM 服务不可用/返回 0 候选时本测试误失败。
            main(["compile-drawing", str(self.front), "--tower", "--out", str(out),
                  "--backend", "rule-based-scan"])
            model = load_model(out)
            self.assertGreater(len(model.rules), 0)
            self.assertGreater(
                sum(1 for c in model.components.values() if c.kind == "tower_bar"), 0)


if __name__ == "__main__":
    unittest.main()
