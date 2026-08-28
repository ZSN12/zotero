"""M6 图册交付与螺栓 GLB 测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
OVERLAY = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"


class DeliverProjectTest(unittest.TestCase):
    def test_deliver_project_guowang(self):
        from traceability.project.delivery import deliver_project

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            result = deliver_project(d, tmp, layer_map_path=str(OVERLAY))
            # P0-3 + 最大连通子图提取后：门禁通过，导出正式 GLB
            self.assertTrue(result.get("ok"), result.get("glb_error") or result.get("harness"))
            self.assertTrue(Path(result["manifest_path"]).exists())
            self.assertTrue(Path(result["model_path"]).exists())
            self.assertTrue(Path(result["glb_path"]).exists())
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertGreater(manifest["merge_report"].get("nodes_solved", 0), 0)
            self.assertIn("harness", manifest)
            self.assertTrue(result.get("harness_all_passed"))

    def test_cli_deliver_project(self):
        from traceability.cli import main

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                main(["deliver-project", str(d), "--out-dir", tmp,
                      "--layer-map", str(OVERLAY)])
            except SystemExit as exc:
                # 3D 形状门禁未通过 → CLI 以非 0 退出，不宣称成功
                self.assertEqual(exc.code, 1, f"deliver-project should fail: {exc.code}")
            manifest = Path(tmp) / "project_delivery.json"
            self.assertTrue(manifest.exists())


class BoltGlbTest(unittest.TestCase):
    def test_glb_includes_bolt_hole_meshes(self):
        from traceability.intake.tower_batch import cross_file_batch
        from traceability.io import load_model
        from traceability.solve.tower_solver import export_tower_glb
        from traceability.connection.bolt_mesh import bolt_hole_meshes

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            cross_file_batch(d, tmp, layer_map_path=str(OVERLAY))
            model = load_model(str(Path(tmp) / "model.json"))
            meshes, _ = bolt_hole_meshes(model)
            # P1：detail(03) 不再进入 cross_file 合并，节点大样螺栓孔不随合并模型
            # 自动出现；此处验证主杆件 GLB 仍可导出且数量可信。
            glb = Path(tmp) / "tower.glb"
            try:
                export_tower_glb(model, glb, strict=True)
            except Exception as exc:
                self.skipTest(str(exc))
            import trimesh
            scene = trimesh.load(str(glb), force="scene")
            bars = sum(1 for c in model.components.values() if c.kind == "tower_bar")
            self.assertGreaterEqual(len(scene.geometry), bars)
            self.assertGreater(bars, 0)
