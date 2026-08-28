"""M7 图册级 Harness 与件号索引测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
OVERLAY = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"


class BarInventoryTest(unittest.TestCase):
    def test_guowang_bar_inventory_has_entries(self):
        from traceability.project.model import build_project_from_directory
        from traceability.io import load_model
        from traceability.project.bar_inventory import aggregate_bar_inventory

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            project = build_project_from_directory(
                d, "guowang", layer_map_path=str(OVERLAY), out_dir=tmp,
            )
            models, sources = [], []
            for sid, sheet in project.sheets.items():
                if sheet.model_path:
                    m = load_model(sheet.model_path)
                    m.name = sid
                    models.append(m)
                    sources.append(sid)
            inv = aggregate_bar_inventory(models, model_sources=sources)
        self.assertGreater(inv["total_unique_bar_ids"], 0)
        self.assertGreater(len(inv["entries"]), 0)


class ProjectHarnessTest(unittest.TestCase):
    def test_run_project_harness_guowang(self):
        from traceability.project.model import build_project_from_directory
        from traceability.io import load_model
        from traceability.intake.tower_batch import cross_file_bar_id_report
        from traceability.project.bar_inventory import aggregate_bar_inventory
        from traceability.project.bom_tree import aggregate_bom_tree
        from traceability.project.harness import run_project_harness

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            project = build_project_from_directory(
                d, "guowang", layer_map_path=str(OVERLAY), out_dir=tmp,
            )
            models, sources = [], []
            for sid, sheet in project.sheets.items():
                if sheet.model_path:
                    m = load_model(sheet.model_path)
                    m.name = sid
                    models.append(m)
                    sources.append(sid)
            inv = aggregate_bar_inventory(models, model_sources=sources)
            cross = cross_file_bar_id_report(models)
            bom = aggregate_bom_tree(models, model_sources=sources)
            ph = run_project_harness(
                project,
                sheet_models={s: models[i] for i, s in enumerate(sources)},
                cross_sheet_bar_id=cross,
                bom_tree=bom,
                bar_inventory=inv,
            )
        self.assertIn("r_project_sheets_ready", [r["rule"] for r in ph["results"]])
        ready = next(r for r in ph["results"] if r["rule"] == "r_project_sheets_ready")
        self.assertEqual(ready["status"], "passed")
        inv_rule = next(r for r in ph["results"] if r["rule"] == "r_project_bar_inventory")
        self.assertEqual(inv_rule["status"], "passed")


class DeliverProjectM7Test(unittest.TestCase):
    def test_deliver_includes_project_harness(self):
        from traceability.project.delivery import deliver_project

        d = EXAMPLES / "external" / "guowang_35A1"
        if not d.exists():
            self.skipTest("国网目录不存在")
        with tempfile.TemporaryDirectory() as tmp:
            result = deliver_project(d, tmp, layer_map_path=str(OVERLAY))
            self.assertTrue(result.get("ok"))
            self.assertTrue((result.get("glb_geometry_gate") or {}).get("ok"))
            ph = result.get("project_harness") or {}
            self.assertGreater(ph.get("sheet_count", 0), 0)
            self.assertTrue(Path(result["artifact_paths"]["project_harness"]).exists())
            self.assertTrue(Path(result["artifact_paths"]["bar_inventory"]).exists())
            inv = json.loads(Path(result["artifact_paths"]["bar_inventory"]).read_text(encoding="utf-8"))
            self.assertGreater(inv.get("total_unique_bar_ids", 0), 0)
