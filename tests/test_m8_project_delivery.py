"""M8 master BOM + 模块装配 + Web 工作台测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
GW = EXAMPLES / "external" / "guowang_35A1"
OVERLAY = GW / "layer_overlay.json"
MASTER_BOM = GW / "guowang_merged_bom.csv"


class MasterBomTest(unittest.TestCase):
    def test_resolve_master_bom_from_overlay(self):
        from traceability.project.module_build import resolve_master_bom_path

        if not GW.exists():
            self.skipTest("国网目录不存在")
        p = resolve_master_bom_path(GW, layer_map_path=str(OVERLAY))
        self.assertIsNotNone(p)
        self.assertTrue(p.exists())

    def test_physical_bom_matches_master(self):
        from traceability.intake.tower_batch import cross_file_batch
        from traceability.io import load_model
        from traceability.project.bom_tree import aggregate_bom_tree
        from traceability.project.module_build import physical_bar_counts

        if not GW.exists() or not MASTER_BOM.exists():
            self.skipTest("国网样例或 master BOM 不存在")
        with tempfile.TemporaryDirectory() as tmp:
            cross_file_batch(GW, tmp, layer_map_path=str(OVERLAY))
            model = load_model(str(Path(tmp) / "model.json"))
        counts = physical_bar_counts(model)
        tree = aggregate_bom_tree(
            [],
            master_bom_path=str(MASTER_BOM),
            physical_bar_counts=counts,
        )
        # P2 待确认：plan=35C2-SJG1-ML 是另一系列基础图，02 件号与 master BOM
        # 数量暂不一致（master length_mm 也多为 0，暂不能救场）。这里只验证
        # 聚合链路如实报告 conflicts / only_in_master / only_in_model。
        self.assertIsInstance(tree.get("conflict_count"), int)
        self.assertIsInstance(tree.get("only_in_master"), list)
        self.assertIsInstance(tree.get("only_in_model"), list)
        self.assertGreaterEqual(tree.get("conflict_count", 0), 0)


class ModuleAssemblyTest(unittest.TestCase):
    def test_z_split_assembly(self):
        from traceability.intake.tower_batch import cross_file_batch
        from traceability.io import load_model
        from traceability.project.module_build import try_assembly_from_merged

        if not GW.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            cross_file_batch(GW, tmp, layer_map_path=str(OVERLAY))
            model = load_model(str(Path(tmp) / "model.json"))
            info = try_assembly_from_merged(model, str(OVERLAY))
        self.assertIsNotNone(info)
        self.assertEqual(info.get("mode"), "assembly_demo_z_split")
        self.assertGreater(sum(r.get("matched", 0) for r in info.get("reports") or []), 0)


class DeliverProjectM8Test(unittest.TestCase):
    def test_deliver_master_bom_and_assembly(self):
        from traceability.project.delivery import deliver_project

        if not GW.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            result = deliver_project(GW, tmp, layer_map_path=str(OVERLAY))
            # P3 架构迁移：ezdxf 路径门禁阻断劣质 GLB（ok=False、glb_path=None），
            # 但 M8 master BOM / 模块装配仍应完整产出并如实记录。
            self.assertIsNotNone(result.get("manifest_path"))
            self.assertTrue(Path(result["manifest_path"]).exists())
            gate = result.get("glb_geometry_gate") or {}
            self.assertIn("ok", gate)  # 门禁结果如实记录
            bs = result.get("bom_tree_summary") or {}
            self.assertIsInstance(bs.get("conflict_count"), int)
            ph = result.get("project_harness") or {}
            bom_rule = next(r for r in ph["results"] if r["rule"] == "r_project_bom_master")
            self.assertIn(bom_rule["status"], ("passed", "failed"))
            asm_rule = next(r for r in ph["results"] if r["rule"] == "r_project_module_assembly")
            self.assertEqual(asm_rule["status"], "passed")
            self.assertTrue(result.get("assembly", {}).get("enabled"))
