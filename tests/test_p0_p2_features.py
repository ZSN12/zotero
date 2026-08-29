"""P0/P1/P2 新增交付物验收测试。

覆盖：
    P0-1 run-tower 多步编排
    P0-2 processing_graph steps.json
    P0-4 deliver-tower 一键交付包
    P0-5 .cursor skill 打包
    P1-1 铁塔 MLLM Prompt + Schema
    P1-2 tower+scan 后端策略
    P1-3 PDF 转图
    P1-5 layer overlay
    P1-6 长度约束求解（16 节点误差 <1mm）
    P1-7 L 型截面 GLB
    P1-8 DWG 转换层
    P2-1 噪声过滤（候选降 30%+ 且真实杆件不丢）
    P2-2 比例尺标定
    P2-3 OCR 件号关联
    P2-4 多视图扫描融合
    P2-5 扫描 → 终版 3D 闸门
    P2-6 扫描 PDF 样例
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def tower_components(model, kind):
    return [c for c in model.components.values() if c.kind == kind]


class ProcessingGraphTest(unittest.TestCase):
    def test_steps_json_export(self):
        from traceability.harness.processing_graph import ProcessingGraph, export_steps_json
        g = ProcessingGraph()
        g.start("intake", "图纸接入")
        g.finish(output="m.json")
        g.start("compile", "编译")
        g.fail("boom")
        with tempfile.TemporaryDirectory() as d:
            path = export_steps_json(g, Path(d) / "steps.json")
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual([s["id"] for s in data["steps"]], ["intake", "compile"])
        self.assertEqual(data["steps"][0]["status"], "passed")
        self.assertEqual(data["steps"][1]["status"], "failed")
        self.assertEqual(data["steps"][1]["error"], "boom")


class RunTowerTest(unittest.TestCase):
    @pytest.mark.slow
    @pytest.mark.integration
    def test_run_tower_full_chain(self):
        from traceability.cli import main
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            main(["run-tower", str(EXAMPLES / "tower_110kv.dxf"),
                  "--bom", str(EXAMPLES / "tower_110kv_bom.csv"),
                  "--merge", "--golden", str(EXAMPLES / "tower_110kv_golden.json"),
                  "--out-dir", str(out)])
            steps = json.loads((out / "steps.json").read_text(encoding="utf-8"))
            ids = [s["id"] for s in steps["steps"]]
            self.assertTrue({"intake", "compile", "cross_check", "verify", "solve", "export"}.issubset(ids))
            self.assertTrue((out / "model.json").exists())
            self.assertTrue((out / "harness_summary.json").exists())

    @pytest.mark.slow
    @pytest.mark.integration
    def test_deliver_tower(self):
        from traceability.cli import main
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            main(["deliver-tower", str(EXAMPLES / "tower_110kv.dxf"),
                  "--bom", str(EXAMPLES / "tower_110kv_bom.csv"),
                  "--merge", "--out-dir", str(out)])
            for f in ("model.json", "tower.glb", "report.md", "steps.json", "harness_summary.json"):
                self.assertTrue((out / f).exists(), f)


class SkillPackagingTest(unittest.TestCase):
    def test_skill_dir(self):
        skill = REPO / ".cursor" / "skills" / "engineering-traceability"
        self.assertTrue((skill / "SKILL.md").exists())
        self.assertTrue((skill / "contract.md").exists())
        self.assertTrue((skill / "examples.md").exists())
        front = (skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: engineering-traceability", front)
        self.assertIn("run-tower", front)


class TowerMllmPromptTest(unittest.TestCase):
    def test_schema_validates_output(self):
        from traceability.intake.mllm_tower_prompt import (
            TOWER_MLLM_SCHEMA, validate_tower_mllm_output, parse_tower_mllm_output,
        )
        good = {
            "objects": [{
                "obj_type": "component",
                "data": {"id": "bar_1", "kind": "tower_bar", "name": "杆件",
                         "properties": {"bar_id": "M0001", "section": "L100x8"}},
                "source": {"source_type": "drawing", "reference": "t.png", "confidence": 0.6},
                "confidence": 0.6,
            }],
        }
        self.assertEqual(validate_tower_mllm_output(good), [])
        objs, problems = parse_tower_mllm_output(good)
        self.assertEqual(problems, [])
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0].data["kind"], "tower_bar")

        # 硬约束（策略 A）：非法 kind 不整批拒，只丢弃该条 + parse_warnings
        from traceability.intake.mllm_tower_prompt import parse_tower_mllm_output_with_warnings
        bad = {"objects": [
            {"obj_type": "component", "data": {"id": "x", "kind": "tower"}},
            {"obj_type": "component", "data": {"id": "bar_1", "kind": "tower_bar",
             "properties": {"bar_id": "M0001"}}},
        ]}
        self.assertEqual(validate_tower_mllm_output(bad), [])
        objs, problems, warnings = parse_tower_mllm_output_with_warnings(bad)
        self.assertEqual(problems, [])
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0].data["kind"], "tower_bar")
        self.assertTrue(any("非法铁塔 kind 'tower'" in w for w in warnings))

    def test_backend_strategy_tower_scan(self):
        from traceability.intake.mllm_backend import (
            DrawingInput, MLLMBackend, TowerScanBackend, choose_backend,
        )
        no_api = MLLMBackend(api_key="")
        self.assertIsInstance(
            choose_backend(DrawingInput(path="t.png", kind="scan", tower=True), mllm=no_api),
            TowerScanBackend,
        )
        with_api = MLLMBackend(api_key="sk-test")
        self.assertIsInstance(
            choose_backend(DrawingInput(path="t.png", kind="scan", tower=True), mllm=with_api),
            MLLMBackend,
        )


class TowerMllmContractMockTest(unittest.TestCase):
    def test_tower_mllm_output_passes_contract(self):
        """P1-1 验收：MLLM mock 输出 → parse → contract.py 强制成 EngineeringModel。"""
        from traceability.intake.mllm_backend import DrawingInput, ModelCandidate
        from traceability.intake.mllm_tower_prompt import parse_tower_mllm_output
        from traceability.skill.contract import to_engineering_model
        from traceability.model import DimensionOrigin

        raw = {
            "objects": [
                {"obj_type": "component", "data": {
                    "id": "bar_1", "kind": "tower_bar", "name": "杆件 M0001",
                    "properties": {"bar_id": "M0001", "section": "L100x8",
                                   "from_node": "node_1", "to_node": "node_2",
                                   "solve_status": "pending_review"}},
                 "source": {"source_type": "drawing", "reference": "t.png",
                            "confidence": 0.6}, "confidence": 0.6},
                {"obj_type": "component", "data": {
                    "id": "node_1", "kind": "tower_node", "name": "节点",
                    "properties": {"x_px": 0, "y_px": 0}},
                 "source": {"source_type": "drawing", "reference": "t.png",
                            "confidence": 0.6}, "confidence": 0.6},
                {"obj_type": "dimension", "data": {
                    "id": "d_scale", "name": "比例尺", "value": None, "unit": "mm/px",
                    "origin": "measured"},
                 "source": {"source_type": "drawing", "reference": "t.png",
                            "confidence": 0.6}, "confidence": 0.6},
            ]
        }
        objs, problems = parse_tower_mllm_output(raw)
        self.assertEqual(problems, [])
        cand = ModelCandidate(input=DrawingInput(path="t.png", kind="scan", tower=True),
                              objects=objs, raw="mock", backend="mllm")
        model = to_engineering_model(cand)
        self.assertIn("bar_1", model.components)
        self.assertIn("node_1", model.components)
        self.assertEqual(model.dimensions["d_scale"].origin, DimensionOrigin.PLACEHOLDER)
        self.assertLessEqual(model.components["bar_1"].source.confidence, 0.6)


class PdfRasterTest(unittest.TestCase):
    def test_pdf_to_png(self):
        from traceability.intake.pdf_raster import rasterize_pdf_to_png
        pdf = EXAMPLES / "tower_scan.pdf"
        if not pdf.exists():
            self.skipTest("tower_scan.pdf 未生成")
        png = rasterize_pdf_to_png(pdf)
        self.assertTrue(Path(png).exists())
        self.assertGreater(Path(png).stat().st_size, 0)


class LayerOverlayTest(unittest.TestCase):
    def test_overlay_changes_layers_only(self):
        from traceability.intake.tower_dxf import extract_tower_from_dxf
        model = extract_tower_from_dxf(
            EXAMPLES / "external" / "tower_external_demo.dxf",
            layer_map_path=EXAMPLES / "external" / "layer_overlay.json",
        )
        bars = tower_components(model, "tower_bar")
        layers = {b.properties.get("layer") for b in bars}
        self.assertIn("TOWER_EXTRA", layers)


class LengthConstraintSolveTest(unittest.TestCase):
    def test_16_node_single_view_z_recovered(self):
        from traceability.model import Component, Dimension, DimensionOrigin, EngineeringModel
        from traceability.solve.tower_solver import solve_tower
        m = EngineeringModel(name="t16")
        coords = []
        for i in range(8):
            ang = i * math.pi / 4
            coords.append((round(2000 * math.cos(ang), 2), round(2000 * math.sin(ang), 2)))
        for i, (x, y) in enumerate(coords):
            m.add_component(Component(id=f"node_N{i+1:02d}", name="n", kind="tower_node",
                                      properties={"node_id": f"N{i+1:02d}", "x": x, "y": y,
                                                  "z": 0.0, "solve_status": "solved"}))
        for i, (x, y) in enumerate(coords):
            m.add_component(Component(id=f"node_N{i+9:02d}", name="n", kind="tower_node",
                                      properties={"node_id": f"N{i+9:02d}", "x": x, "y": y,
                                                  "z": None, "solve_status": "partial"}))
        for i in range(8):
            bid = f"M{i+1:04d}"
            m.add_component(Component(id=f"bar_{bid}", name=bid, kind="tower_bar",
                                      properties={"bar_id": bid,
                                                  "from_node": f"node_N{i+1:02d}",
                                                  "to_node": f"node_N{i+9:02d}"}))
            m.add_dimension(Dimension(id=f"dim_bom_length_{bid}", name="", value=3000.0,
                                      unit="mm", origin=DimensionOrigin.MEASURED,
                                      applies_to=f"bar_{bid}"))
        nodes, problems = solve_tower(m)
        self.assertEqual(problems, [])
        errs = [abs(nodes[f"node_N{i+9:02d}"]["z"] - 3000.0) for i in range(8)]
        self.assertLess(max(errs), 1.0)


class AngleSteelMeshTest(unittest.TestCase):
    def test_l_section_mesh(self):
        from traceability.solve.tower_solver import _angle_steel_mesh, _parse_section
        w, t = _parse_section("L100x8")
        self.assertEqual((w, t), (100.0, 8.0))
        mesh = _angle_steel_mesh("L100x8", 500)
        self.assertTrue(mesh.is_watertight)
        mesh2 = _angle_steel_mesh("L63x6", 500)
        # 截面规格不同 -> 顶点范围不同
        self.assertNotEqual(mesh.bounds.tolist(), mesh2.bounds.tolist())


class DwgSupportTest(unittest.TestCase):
    def test_ensure_dxf_passthrough(self):
        from traceability.intake.dwg import ensure_dxf
        self.assertEqual(ensure_dxf(EXAMPLES / "tower_demo.dxf"), str(EXAMPLES / "tower_demo.dxf"))

    def test_ensure_dxf_requires_converter(self):
        import shutil
        from traceability.intake.dwg import ensure_dxf
        if any(shutil.which(c) for c in ("ODAFileConverter", "dwg2dxf", "teigha")):
            self.skipTest("本机已有 DWG 转换器")
        with self.assertRaises(RuntimeError):
            ensure_dxf(EXAMPLES / "not_exist.dwg")


class ScanNoiseFilterTest(unittest.TestCase):
    def test_filter_drops_isolated_keeps_truss(self):
        from traceability.intake.tower_layout import filter_noise_segments
        # 三角形真实杆件（共点）
        truss = [(0.0, 0.0, 100.0, 0.0), (100.0, 0.0, 50.0, 100.0), (50.0, 100.0, 0.0, 0.0)]
        # dim/图例孤立线
        noise = [(200.0, 200.0, 400.0, 200.0), (200.0, 300.0, 400.0, 300.0),
                 (200.0, 400.0, 400.0, 400.0)]
        keep, removed = filter_noise_segments(truss + noise)
        self.assertEqual(len(keep), 3)
        self.assertEqual(len(removed), 3)
        # 候选数下降 50%（>=30%），真实杆件全部保留
        drop = (len(truss) + len(noise) - len(keep)) / (len(truss) + len(noise))
        self.assertGreaterEqual(drop, 0.3)
        for seg in truss:
            self.assertIn(seg, keep)


class ScaleCalibrationTest(unittest.TestCase):
    def test_calibrate_from_scale_text(self):
        from traceability.intake.tower_layout import calibrate_scale
        info = calibrate_scale("x.png", scale="1:50", dpi=150)
        self.assertAlmostEqual(info["mm_per_px"], 25.4 / 150 * 50, places=5)
        info2 = calibrate_scale("x.png", ocr_text="SCALE 1:100")
        self.assertAlmostEqual(info2["mm_per_px"], 25.4 / 150 * 100, places=5)
        self.assertEqual(info2["method"], "ocr")
        info3 = calibrate_scale("x.png")
        self.assertIsNone(info3["mm_per_px"])


class OcrAssociationTest(unittest.TestCase):
    def test_associate_ocr_labels(self):
        from traceability.intake.tower_layout import associate_ocr_labels
        from traceability.model import Component, EngineeringModel
        m = EngineeringModel(name="s")
        for nid, (x, y) in {"n1": (0.0, 0.0), "n2": (200.0, 0.0),
                            "n3": (400.0, 0.0), "n4": (600.0, 0.0)}.items():
            m.add_component(Component(id=nid, name=nid, kind="tower_node",
                                      properties={"x_px": x, "y_px": y}))
        m.add_component(Component(id="b1", name="b1", kind="tower_bar",
                                  properties={"bar_id": "SCAN_0001", "from_node": "n1", "to_node": "n2"}))
        m.add_component(Component(id="b2", name="b2", kind="tower_bar",
                                  properties={"bar_id": "SCAN_0002", "from_node": "n3", "to_node": "n4"}))
        boxes = [
            {"text": "M0001", "bbox": [90.0, 0.0, 110.0, 20.0]},
            {"text": "G01", "bbox": [490.0, 0.0, 510.0, 20.0]},
        ]
        n = associate_ocr_labels(m, boxes=boxes)
        self.assertEqual(n, 2)
        self.assertEqual(m.components["b1"].properties["bar_id"], "M0001")
        self.assertEqual(m.components["b2"].properties["bar_id"], "G01")


class ScanMergeTest(unittest.TestCase):
    def test_merge_front_side(self):
        from traceability.intake.tower_scan_merge import merge_scan_views
        from traceability.model import Component, EngineeringModel
        front = EngineeringModel(name="front")
        side = EngineeringModel(name="side")
        for nid, (x, z) in {"n1": (100.0, 0.0), "n2": (100.0, 100.0)}.items():
            front.add_component(Component(id=nid, name=nid, kind="tower_node",
                                          properties={"x_px": x, "y_px": z}))
        for nid, (y, z) in {"s1": (200.0, 0.0), "s2": (200.0, 100.0)}.items():
            side.add_component(Component(id=nid, name=nid, kind="tower_node",
                                         properties={"x_px": y, "y_px": z}))
        front.add_component(Component(id="bf", name="bf", kind="tower_bar",
                                      properties={"bar_id": "SCAN_0001", "from_node": "n1", "to_node": "n2"}))
        merged = merge_scan_views(front, side)
        nodes = tower_components(merged, "tower_node")
        self.assertEqual(len(nodes), 2)
        for c in nodes:
            self.assertIsNotNone(c.properties.get("x_px"))
            self.assertIsNotNone(c.properties.get("y_px"))
            self.assertIsNotNone(c.properties.get("z_px"))


class ScanGateTest(unittest.TestCase):
    def test_pending_review_blocks_strict_export(self):
        from traceability.intake.tower_layout import analyze_tower_scan, confirm_tower_scan
        from traceability.solve.tower_solver import solve_tower, SolveError, export_tower_obj
        m = analyze_tower_scan(EXAMPLES / "clear" / "tower_front_hd.png")
        _nodes, problems = solve_tower(m)
        self.assertTrue(any("pending_review" in p for p in problems))
        with self.assertRaises(SolveError):
            export_tower_obj(m, "/tmp/never.glb", strict=True)
        m = confirm_tower_scan(m)
        _nodes2, problems2 = solve_tower(m, allow_scan=True)
        self.assertFalse(any("pending_review" in p for p in problems2))


class ScanPdfTest(unittest.TestCase):
    @pytest.mark.slow
    def test_intake_scan_pdf(self):
        from traceability.cli import main
        from traceability.io import load_model
        pdf = EXAMPLES / "tower_scan.pdf"
        if not pdf.exists():
            self.skipTest("tower_scan.pdf 未生成")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "scan.json"
            main(["intake-scan", str(pdf), "--tower", "--out", str(out)])
            model = load_model(out)
            self.assertGreater(len(tower_components(model, "tower_bar")), 0)


if __name__ == "__main__":
    unittest.main()
