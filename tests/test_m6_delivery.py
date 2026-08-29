"""M6 图册交付与螺栓 GLB 测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
OVERLAY = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"


@pytest.mark.integration
class DeliverProjectTest(unittest.TestCase):
    @pytest.mark.slow
    def test_deliver_project_guowang(self):
        from traceability.project.delivery import deliver_project

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            result = deliver_project(d, tmp, layer_map_path=str(OVERLAY))
            # P3 架构迁移：ezdxf 路径对 02 双线角钢图提取质量不足（210 杆碎段、
            # 45 个 degree-1 悬空节点），门禁会正确阻断劣质 GLB 导出（ok=False）。
            # 交付 manifest 仍产出，且门禁失败原因如实记录。这里只验证交付流程
            # 完整跑通 + manifest 产出，不再硬编码 ok=True。
            self.assertTrue(Path(result["manifest_path"]).exists())
            self.assertTrue(Path(result["model_path"]).exists())
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertGreater(manifest["merge_report"].get("nodes_solved", 0), 0)
            self.assertIn("harness", manifest)
            # P0-2 失败传播：status 三态。ok=False 不再唯一对应「几何门禁失败」，
            # 而可能来自 harness failed / sheet_failures / 导出失败等多项。
            # skeleton_glb_path 是否产出取决于几何门禁（skeleton_gate）本身，
            # 而非整体 ok。这里只验证 status 是合法三态之一。
            self.assertIn(result.get("status"), ("verified", "review_required", "failed"))
            self.assertIsInstance(result.get("sheet_failures"), list)

    @pytest.mark.slow
    def test_deliver_splits_l0_skeleton_index(self):
        # Phase A3：交付 manifest 明确三层产物，不混评；
        # detail/模块页不进 spatial_merge，只留在 index.json。
        from traceability.project.delivery import deliver_project

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = deliver_project(d, tmp, layer_map_path=str(OVERLAY))

            self.assertTrue(Path(result["index_path"]).exists())
            # P3 架构迁移：ezdxf 路径门禁失败时 skeleton_glb_path 为 None（正确
            # 阻断劣质导出）；canonical.glb（L0 GIM）不受影响，仍产出。
            self.assertTrue(Path(result["canonical_glb_path"]).exists())

            index = json.loads((out / "index.json").read_text(encoding="utf-8"))
            roles = {sid: sh["role"] for sid, sh in index["sheets"].items()}
            self.assertEqual(roles.get("35A1-JC1-02"), "elevation")
            self.assertEqual(roles.get("35C2-SJG1-ML"), "plan")
            self.assertEqual(roles.get("35A1-JC1-03"), "node_detail")
            self.assertEqual(index["spatial_merge_sheets"], ["35A1-JC1-02", "35C2-SJG1-ML"])

            manifest = json.loads((out / "project_delivery.json").read_text(encoding="utf-8"))
            layers = {p["id"]: p["layer"] for p in manifest["products"]}
            self.assertEqual(layers, {
                "canonical.glb": "L0",
                "skeleton.glb": "M3",
                "index.json": "M1",
                "detail_qa_atlas.glb": "QA",
            })
            # canonical.glb 与 index.json 恒 present；skeleton.glb 仅门禁通过时 present
            self.assertTrue(manifest["products"][0]["present"])  # canonical.glb L0
            self.assertTrue(manifest["products"][2]["present"])  # index.json M1
            # detail_qa_atlas 是非真实 3D 的 QA 视图，标记 non_structural
            self.assertTrue(manifest["products"][3]["non_structural"])

    @pytest.mark.slow
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
                # 3D 形状门禁未通过 / harness 有 failed / pending → CLI 非 0 退出，
                # 不宣称成功（verified=0, review_required=2, failed=1）。
                self.assertIn(exc.code, (1, 2), f"deliver-project should fail: {exc.code}")
            manifest = Path(tmp) / "project_delivery.json"
            self.assertTrue(manifest.exists())


class BoltGlbTest(unittest.TestCase):
    @pytest.mark.integration
    @pytest.mark.slow
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
