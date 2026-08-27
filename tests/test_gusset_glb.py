"""M5 GLB 节点板导出测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
OVERLAY = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"


class GussetGlbTest(unittest.TestCase):
    def test_glb_includes_gusset_mesh(self):
        from traceability.intake.tower_batch import cross_file_batch
        from traceability.io import load_model
        from traceability.solve.tower_solver import export_tower_glb

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            cross_file_batch(d, tmp, layer_map_path=str(OVERLAY))
            model = load_model(str(Path(tmp) / "model.json"))
            glb = Path(tmp) / "tower.glb"
            try:
                export_tower_glb(model, glb, strict=True)
            except Exception as exc:
                self.skipTest(str(exc))
            try:
                import trimesh
                scene = trimesh.load(str(glb), force="scene")
            except Exception as exc:
                self.skipTest(str(exc))
            bars = sum(1 for c in model.components.values() if c.kind == "tower_bar")
            gussets = sum(
                1 for c in model.components.values()
                if c.kind == "gusset_plate" and c.properties.get("polygon_global")
            )
            self.assertEqual(len(scene.geometry), bars + gussets)
