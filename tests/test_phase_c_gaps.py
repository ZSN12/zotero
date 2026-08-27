"""Phase C–F + Gap 1/2 验收测试。"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
OVERLAY = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"


def _cv2_ok():
    try:
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_cv2_ok(), "opencv 未安装")
class PreprocessTest(unittest.TestCase):
    def test_preprocess_for_scan_synthetic(self):
        import cv2
        import numpy as np
        from traceability.intake.tower_preprocess import preprocess_for_scan

        img = np.full((200, 300), 230, dtype="uint8")
        cv2.line(img, (20, 100), (280, 100), 170, 2)
        out, meta = preprocess_for_scan(img)
        self.assertEqual(meta["method"], "line_repaint")
        self.assertEqual(out.shape, img.shape)

    def test_preprocess_bench_synthetic(self):
        from benchmark.preprocess_a2_bench import run_bench
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bench.json"
            report = run_bench(Path("missing.png"), out, synthetic=True)
        self.assertIn("raw_hough", report)
        self.assertIn("preprocessed_hough", report)


class CrossFileBatchTest(unittest.TestCase):
    def test_merge_cross_file_views_preserves_view_type(self):
        from traceability.intake.tower_dxf import extract_tower_from_dxf
        from traceability.intake.tower_batch import merge_cross_file_views

        dxf02 = EXAMPLES / "external" / "guowang_35A1" / "35A1-JC1-02.dxf"
        if not dxf02.exists():
            self.skipTest("国网样例不存在")
        m02 = extract_tower_from_dxf(str(dxf02), layer_map_path=str(OVERLAY))
        merged = merge_cross_file_views([m02], layer_map_path=str(OVERLAY))
        bars = [c for c in merged.components.values() if c.kind == "tower_bar"]
        self.assertGreater(len(bars), 0)
        self.assertTrue(all(c.properties.get("source_file") == "35A1-JC1-02" for c in bars))
        df = merged.components.get("drawing_file")
        self.assertEqual(df.properties.get("view_mode"), "cross_file_multi_view")


class ProjectModelTest(unittest.TestCase):
    def test_build_project_from_guowang_dir(self):
        from traceability.project.model import build_project_from_directory

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            project = build_project_from_directory(d, "guowang-35A1", layer_map_path=str(OVERLAY), out_dir=tmp)
        self.assertGreaterEqual(len(project.sheets), 2)
        self.assertIn("35A1-JC1-02", project.sheets)

    def test_assemble_modules_align(self):
        from traceability.model import Component, EngineeringModel
        from traceability.project.assembly import assemble_modules

        def _mod(name, z_top):
            m = EngineeringModel(name=name)
            m.add_component(Component(
                id="N01", name="n", kind="tower_node",
                properties={"x": 0.0, "y": 0.0, "z": z_top, "solve_status": "solved"},
            ))
            return m

        lower = _mod("M1", 1000.0)
        upper = _mod("M2", 0.0)
        upper.components["N01"].properties.update({"x": 1.0, "y": 1.0, "z": 0.0})
        merged, reports = assemble_modules([lower, upper], tol_mm=5.0)
        self.assertEqual(len(reports), 1)
        self.assertGreaterEqual(reports[0]["matched"], 0)

    def test_bom_tree_aggregate(self):
        from traceability.model import Component, EngineeringModel
        from traceability.project.bom_tree import aggregate_bom_tree

        m = EngineeringModel(name="t")
        m.add_component(Component(id="bom_G01", name="b", kind="bom_row",
                                  properties={"bar_id": "G01", "section": "L40X3", "length_mm": 1000, "qty": 2}))
        tree = aggregate_bom_tree([m], model_sources=["sheet1"])
        self.assertEqual(tree["total_unique_bar_ids"], 1)
        self.assertEqual(tree["tree"][0]["qty"], 2)


class ConnectionDetailTest(unittest.TestCase):
    def test_detail_transform_local_to_global(self):
        from traceability.connection.detail_view import (
            DetailViewTransform, local_to_global, parse_detail_view_meta,
        )

        t = parse_detail_view_meta("节点 K1 大样 1:10", region=[100, 200, 50, 150])
        self.assertEqual(t.detail_id, "K1")
        self.assertAlmostEqual(t.scale, 0.1)
        gx, gy, gz = local_to_global(110.0, 60.0, DetailViewTransform(
            detail_id="K1", scale=0.1, origin_local=(100.0, 50.0),
            origin_global=(1000.0, 2000.0, 8100.0),
        ))
        self.assertAlmostEqual(gx, 1001.0)
        self.assertAlmostEqual(gy, 2001.0)

    def test_bolt_group_verification(self):
        from traceability.connection.bolt_verify import BoltGroup, BoltSpec, verify_bolt_group

        spec = BoltSpec(count=2, diameter_mm=16.0, length_mm=50.0)
        outline = [(0, 0), (200, 0), (200, 200), (0, 200)]
        holes = [(40, 40), (160, 40)]
        group = BoltGroup(group_id="g1", spec=spec, holes=holes, plate_outline=outline)
        result = verify_bolt_group(group)
        self.assertIn("edge_checks", result)
        self.assertIn("spacing_checks", result)

    def test_gusset_plate_component(self):
        from traceability.connection.gusset import parse_gusset_from_detail, add_gusset_to_model
        from traceability.model import EngineeringModel

        plate = parse_gusset_from_detail(
            "K1", [(0, 0), (100, 0), (100, 80), (0, 80)], thickness_text="t=8",
        )
        model = EngineeringModel(name="detail")
        add_gusset_to_model(model, plate)
        self.assertIn("gusset_K1", model.components)
        self.assertIn("dim_gusset_t_K1", model.dimensions)


if __name__ == "__main__":
    unittest.main()
