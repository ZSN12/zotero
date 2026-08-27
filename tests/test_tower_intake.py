"""铁塔 Phase 1 单元测试：demo DXF 抽取 + 编号关联 + 拓扑。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from traceability.intake.tower_dxf import make_demo_tower_dxf, extract_tower_from_dxf
from traceability.solve.tower_solver import solve_tower, SolveError
from traceability.io import save_model, load_model


class TowerIntakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dxf = make_demo_tower_dxf(Path(self.tmp.name) / "tower_demo.dxf")

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_counts(self):
        # 完整版 demo：16 物理节点 / 26 杆件
        # 正立面图画 12 根（Y<=0 侧），平面图画全部 26 根
        # Phase 1 按投影抽取：38 根杆件投影
        model = extract_tower_from_dxf(self.dxf)
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        nodes = [c for c in model.components.values() if c.kind == "tower_node"]
        self.assertEqual(len(bars), 38)
        self.assertGreaterEqual(len(nodes), 12)  # 投影节点

    def test_every_bar_has_source(self):
        model = extract_tower_from_dxf(self.dxf)
        for c in model.components.values():
            if c.kind == "tower_bar":
                self.assertIsNotNone(c.source)

    def test_bar_label_association(self):
        model = extract_tower_from_dxf(self.dxf)
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        labeled = [b for b in bars if not b.properties["bar_id"].startswith("UNLABELED")]
        # MVP 验收线：编号关联率 ≥ 80%
        rate = len(labeled) / len(bars)
        self.assertGreaterEqual(rate, 0.8)

    def test_topology_references_existing_nodes(self):
        model = extract_tower_from_dxf(self.dxf)
        node_ids = {c.id for c in model.components.values() if c.kind == "tower_node"}
        for c in model.components.values():
            if c.kind == "tower_bar":
                self.assertIn(c.properties["from_node"], node_ids)
                self.assertIn(c.properties["to_node"], node_ids)

    def test_roundtrip_json(self):
        model = extract_tower_from_dxf(self.dxf)
        out = Path(self.tmp.name) / "tower_model.json"
        save_model(model, out)
        loaded = load_model(out)
        self.assertEqual(len(loaded.components), len(model.components))


class TowerSolverTest(unittest.TestCase):
    def test_solve_blocks_on_missing_z(self):
        """投影节点未做跨视图合并 → Z 缺失 → 严格模式拒绝导出。"""
        with tempfile.TemporaryDirectory() as d:
            dxf = make_demo_tower_dxf(Path(d) / "t.dxf")
            model = extract_tower_from_dxf(dxf)
            nodes, problems = solve_tower(model)
            # 所有投影节点都缺 Z（Phase 2 跨视图合并后才补齐）
            z_missing = [p for p in problems if p.endswith(".z")]
            self.assertGreater(len(z_missing), 0)
            with self.assertRaises(SolveError):
                from traceability.solve.tower_solver import export_tower_obj
                export_tower_obj(model, Path(d) / "out.obj", strict=True)

    def test_solve_export_force(self):
        """--force 可导出线框（仅供预览）。"""
        with tempfile.TemporaryDirectory() as d:
            dxf = make_demo_tower_dxf(Path(d) / "t.dxf")
            model = extract_tower_from_dxf(dxf)
            from traceability.solve.tower_solver import export_tower_obj
            content = export_tower_obj(model, Path(d) / "out.obj", strict=False)
            self.assertIn("v ", content)
            self.assertIn("l ", content)


if __name__ == "__main__":
    unittest.main()
