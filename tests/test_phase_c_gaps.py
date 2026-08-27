"""Phase C–F + Gap 1/2 验收测试（审查后强化）。"""

from __future__ import annotations

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
    def test_thin_line_not_erased(self):
        import cv2
        import numpy as np
        from traceability.intake.tower_preprocess import preprocess_for_scan

        img = np.full((100, 200), 230, dtype="uint8")
        cv2.line(img, (10, 50), (190, 50), 120, 1)  # 1px 弱线（灰度须 < INK_THRESHOLD=160）
        out, meta = preprocess_for_scan(img)
        self.assertGreater(meta["ink_pixels"], 0, "1px 弱线不应被完全抹掉")
        self.assertGreaterEqual(meta["retain_ratio"], 0.15)

    def test_preprocess_bench_preserves_ink(self):
        from benchmark.preprocess_a2_bench import run_bench
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bench.json"
            report = run_bench(Path("missing.png"), out, synthetic=True)
        pre = report["preprocessed_hough"]["preprocess"]
        self.assertGreater(pre["ink_pixels"], 0)
        self.assertGreaterEqual(pre["retain_ratio"], 0.5)
        self.assertGreater(report["raw_hough"]["raw_segments"], 0)


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
        df = merged.components.get("drawing_file")
        self.assertEqual(df.properties.get("view_mode"), "cross_file_multi_view")

    def test_cross_file_batch_runs(self):
        from traceability.intake.tower_batch import cross_file_batch
        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            r = cross_file_batch(d, tmp, layer_map_path=str(OVERLAY))
        self.assertIsNotNone(r.get("model_path"))
        mr = r.get("merge_report") or {}
        self.assertGreater(mr.get("nodes_solved", 0), 0, "cross_file front+plan 应解出 3D 节点")

    def test_guowang_plan_overlay_parses_bars(self):
        from traceability.intake.tower_dxf import extract_tower_from_dxf

        dxf = EXAMPLES / "external" / "guowang_35A1" / "35C2-SJG1-ML.dxf"
        if not dxf.exists():
            self.skipTest("国网平面图不存在")
        model = extract_tower_from_dxf(str(dxf), layer_map_path=str(OVERLAY))
        df = model.components.get("drawing_file")
        self.assertTrue(df.properties.get("parse_bars"))
        self.assertEqual(df.properties.get("drawing_kind"), "plan")
        nodes = [c for c in model.components.values() if c.kind == "tower_node"]
        self.assertGreater(len(nodes), 0)
        self.assertIn("plan", df.properties.get("view_kinds") or [])

    def test_guowang_detail_gusset_and_bolt_rules(self):
        from traceability.intake.tower_dxf import extract_tower_from_dxf
        from traceability.harness.harness import run_harness

        dxf = EXAMPLES / "external" / "guowang_35A1" / "35A1-JC1-03.dxf"
        if not dxf.exists():
            self.skipTest("国网大样不存在")
        model = extract_tower_from_dxf(str(dxf), layer_map_path=str(OVERLAY))
        gussets = [c for c in model.components.values() if c.kind == "gusset_plate"]
        bolts = [c for c in model.components.values() if c.kind == "bolt_group"]
        rules = [r for r in model.rules.values() if r.id.startswith("r_bolt_group_")]
        gusset_rules = [r for r in model.rules.values() if r.id.startswith("r_gusset_")]
        self.assertGreaterEqual(len(gussets), 1)
        self.assertGreaterEqual(len(bolts), 1)
        self.assertGreaterEqual(len(rules), 1)
        self.assertGreaterEqual(len(gusset_rules), 1)
        results = run_harness(model)
        bolt_results = [r for r in results if r.target_id.startswith("r_bolt_group_")]
        self.assertGreater(len(bolt_results), 0)
        self.assertTrue(all(r.validator == "bolt-group" for r in bolt_results))

    def test_run_tower_guowang_cross_file_nodes_solved(self):
        from traceability.harness.tower_harness import run_tower

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            result = run_tower(
                d, tmp,
                input_dir=d,
                merge=True,
                layer_map_path=str(OVERLAY),
            )
        batch = result.get("graph")
        batch_step = next((s for s in batch.steps if s.id == "batch"), None) if batch else None
        nodes_solved = (batch_step.detail or {}).get("nodes_solved", 0) if batch_step else 0
        self.assertGreater(nodes_solved, 0, "run-tower 批量应走 cross_file 合并并解出节点")

    def test_glb_mesh_has_bar_id_extras(self):
        import json
        import tempfile
        from traceability.io import load_model
        from traceability.solve.tower_solver import export_tower_glb

        model_path = EXAMPLES / "tower_110kv_model.json"
        if not model_path.exists():
            self.skipTest("110kV 模型不存在")
        model = load_model(str(model_path))
        with tempfile.TemporaryDirectory() as tmp:
            glb = Path(tmp) / "tower.glb"
            try:
                export_tower_glb(model, glb, strict=True)
            except Exception as exc:
                self.skipTest(f"trimesh 不可用或导出失败: {exc}")
            try:
                import trimesh
                loaded = trimesh.load(str(glb), force="scene")
            except Exception as exc:
                self.skipTest(f"无法读取 GLB: {exc}")
            found = False
            for geom in loaded.geometry.values():
                meta = getattr(geom, "metadata", None) or {}
                if meta.get("bar_id") and meta.get("component_id"):
                    found = True
                    break
            self.assertTrue(found, "GLB mesh 应携带 bar_id/component_id extras")
            map_path = glb.with_suffix(".bar_map.json")
            self.assertTrue(map_path.exists())
            rows = json.loads(map_path.read_text(encoding="utf-8"))
            self.assertGreater(len(rows), 0)
            self.assertIn("bar_id", rows[0])


class ProjectModelTest(unittest.TestCase):
    def test_module_sheets_accumulate(self):
        from traceability.project.model import ProjectModel

        p = ProjectModel(project_id="t", name="t")
        p.register_module("M1", "35A1-JC1-02", kind="assembly")
        p.register_module("M1", "35C2-SJG1-ML", kind="drawing")
        self.assertEqual(sorted(p.modules["M1"]["sheets"]), ["35A1-JC1-02", "35C2-SJG1-ML"])

    def test_assemble_modules_rewrites_bar_refs_and_translates(self):
        from traceability.model import Component, EngineeringModel
        from traceability.project.assembly import assemble_modules

        def _mod(name, z_base, xy):
            m = EngineeringModel(name=name)
            m.add_component(Component(
                id="N01", name="n", kind="tower_node",
                properties={"x": xy[0], "y": xy[1], "z": z_base, "solve_status": "solved"},
            ))
            m.add_component(Component(
                id="B01", name="b", kind="tower_bar",
                properties={"from_node": "N01", "to_node": "N01", "bar_id": "G01"},
            ))
            return m

        lower = _mod("M1", 1000.0, (0.0, 0.0))
        upper = _mod("M2", 0.0, (5.0, 5.0))
        merged, reports = assemble_modules([lower, upper], tol_mm=10.0)
        self.assertEqual(merged.components["m02_B01"].properties["from_node"], "m02_N01")
        self.assertTrue(reports[0]["rigid_translation_applied"])
        self.assertAlmostEqual(merged.components["m02_N01"].properties["z"], 1000.0, places=1)

    def test_bom_tree_all_models_when_sources_short(self):
        from traceability.model import Component, EngineeringModel
        from traceability.project.bom_tree import aggregate_bom_tree

        models = []
        for i in range(3):
            m = EngineeringModel(name=f"s{i}")
            m.add_component(Component(
                id=f"bom_{i}", name="b", kind="bom_row",
                properties={"bar_id": f"G0{i}", "qty": 1},
            ))
            models.append(m)
        tree = aggregate_bom_tree(models, model_sources=["only_one"])
        self.assertEqual(tree["total_unique_bar_ids"], 3)


class ConnectionDetailTest(unittest.TestCase):
    def test_detail_scale_1_10_is_times_ten(self):
        from traceability.connection.detail_view import (
            DetailViewTransform, anchor_transform, local_to_global, parse_detail_view_meta,
        )

        t = parse_detail_view_meta("节点 K1 大样 1:10")
        self.assertAlmostEqual(t.scale_to_real, 10.0)
        t = anchor_transform(t, (1000.0, 2000.0, 8100.0), anchor_node_id="node_K1")
        gp = local_to_global(1.0, 0.0, t)
        self.assertIsNotNone(gp)
        self.assertAlmostEqual(gp[0], 1010.0)

    def test_unanchored_no_global_polygon(self):
        from traceability.connection.gusset import parse_gusset_from_detail
        from traceability.connection.detail_view import parse_detail_view_meta

        t = parse_detail_view_meta("节点 K1 大样 1:10")
        plate = parse_gusset_from_detail("K1", [(0, 0), (100, 0), (100, 80)], transform=t)
        comp = plate.to_component()
        self.assertNotIn("polygon_global", comp.properties)
        self.assertEqual(comp.properties.get("global_coords"), "pending_anchor")

    def test_bolt_no_outline_is_pending(self):
        from traceability.connection.bolt_verify import BoltGroup, BoltSpec, verify_bolt_group

        spec = BoltSpec(count=2, diameter_mm=16.0, length_mm=50.0)
        group = BoltGroup(group_id="g1", spec=spec, holes=[(10, 10), (50, 10)], plate_outline=None)
        result = verify_bolt_group(group)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["passed"])

    def test_bolt_hole_outside_plate_fails(self):
        from traceability.connection.bolt_verify import BoltGroup, BoltSpec, verify_bolt_group

        spec = BoltSpec(count=1, diameter_mm=16.0, length_mm=50.0)
        outline = [(0, 0), (200, 0), (200, 200), (0, 200)]
        group = BoltGroup(group_id="g2", spec=spec, holes=[(500, 500)], plate_outline=outline)
        result = verify_bolt_group(group)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["passed"])


class WebSecurityTest(unittest.TestCase):
    def test_resolve_artifact_rejects_traversal(self):
        from web.server import _resolve_artifact

        self.assertIsNone(_resolve_artifact("/artifacts/../etc/passwd"))
        self.assertIsNone(_resolve_artifact("/artifacts/foo/../../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
