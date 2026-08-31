"""P5/P6 regression tests for LOD2 angle bars and LOD3 gusset/bolt solids."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REAL_MODEL = REPO / "out/35A1-JC1-full-deliver/model.json"
REAL_SHEET = REPO / "web/demo/35A1-JC1/latest_deliver/sheets/35A1-JC1-03.json"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lod2 = _load_script("generate_lod2_sample")
lod3 = _load_script("generate_lod3_sample")


def _synthetic_model() -> dict:
    components = {
        "n1": {"id": "n1", "kind": "tower_node", "properties": {"x": 0, "y": 0, "z": 14000}},
        "n2": {"id": "n2", "kind": "tower_node", "properties": {"x": 0, "y": 0, "z": 15000}},
        "n3": {"id": "n3", "kind": "tower_node", "properties": {"x": 800, "y": 200, "z": 16000}},
        "bar_a": {"id": "bar_a", "kind": "tower_bar", "properties": {
            "from_node": "n1", "to_node": "n2", "source_file": "35A1-JC1-06",
            "section": "Q345L100X7", "face": "f", "role": "LEG", "bar_id": "A"}},
        "bar_b": {"id": "bar_b", "kind": "tower_bar", "properties": {
            "from_node": "n2", "to_node": "n3", "source_file": "35A1-JC1-06",
            "section": None, "face": "f", "role": "DIAGONAL", "bar_id": "B"}},
    }
    return {"name": "synthetic", "version": "1", "components": components}


class TestLod2Sample(unittest.TestCase):
    def test_synthetic_two_bar_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            model = tmp / "model.json"
            model.write_text(json.dumps(_synthetic_model()), encoding="utf-8")
            out = tmp / "out"
            self.assertEqual(lod2.main(["--model", str(model), "--out-dir", str(out),
                                       "--z-lo", "14000", "--z-hi", "16000", "--verify"]), 0)
            self.assertTrue((out / "lod2_sample.glb").exists())
            check = lod2.verify_glb(out / "lod2_sample.glb")
            self.assertTrue(check["named"] and check["has_normal"])
            report = json.loads((out / "lod2_sample.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["bars"]), 2)
            self.assertEqual(report["bars"]["bar_b"]["section"], "L50X4")
            self.assertTrue(all(bar["volume_error"] < 0.02 for bar in report["bars"].values()))

    @unittest.skipUnless(REAL_MODEL.exists(), "真实 full-deliver model.json 不存在")
    def test_real_model(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "lod2"
            self.assertEqual(lod2.main(["--model", str(REAL_MODEL), "--out-dir", str(out),
                                       "--verify"]), 0)
            self.assertTrue((out / "lod2_sample.glb").exists())
            self.assertTrue((out / "lod2_sample.json").exists())
            self.assertTrue(lod2.verify_glb(out / "lod2_sample.glb")["has_normal"])
            report = json.loads((out / "lod2_sample.json").read_text(encoding="utf-8"))
            self.assertGreater(len(report["bars"]), 0)
            self.assertTrue(all(bar["volume_error"] < 0.02 for bar in report["bars"].values()))


class TestLod3Sample(unittest.TestCase):
    @unittest.skipUnless(REAL_SHEET.exists(), "真实 03 详图页不存在")
    def test_real_sheet(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "lod3"
            self.assertEqual(lod3.main(["--sheet", str(REAL_SHEET), "--out-dir", str(out),
                                       "--verify"]), 0)
            self.assertTrue((out / "lod3_sample.glb").exists())
            self.assertTrue((out / "lod3_sample.json").exists())
            self.assertTrue(lod3.verify_glb(out / "lod3_sample.glb")["has_normal"])
            report = json.loads((out / "lod3_sample.json").read_text(encoding="utf-8"))
            self.assertEqual(report["totals"]["n_groups"], 16)
            self.assertEqual(report["totals"]["n_bolts"],
                             sum(group["count"] for group in report["bolt_groups"]))


if __name__ == "__main__":
    unittest.main()
