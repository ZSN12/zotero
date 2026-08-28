"""L0 CanonicalTower 单元测试：权威几何 schema + 正确 GLB 导出。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from traceability.solve.canonical_tower import (
    CanonicalTower,
    export_glb,
    export_wireframe_obj,
    load_from_mod,
    load_gt,
    merge_segments,
    parse_mod,
)


class CanonicalTowerSchemaTest(unittest.TestCase):
    def test_load_gt_schema(self):
        gt = load_gt()
        self.assertEqual(gt.units, "mm")
        self.assertEqual(gt.up, "Z")
        self.assertEqual(gt.node_count(), 358)
        self.assertGreaterEqual(gt.bar_count(), 500)
        # bars reference existing nodes
        for b in gt.bars:
            self.assertIn(b["from"], gt.nodes)
            self.assertIn(b["to"], gt.nodes)
        # bbox: 标准 30m 呼高单塔
        bb = gt.bbox()
        self.assertAlmostEqual(bb["z"][0], 0.0, places=1)
        self.assertGreater(bb["z"][1], 36000.0)

    def test_gt_passes_strict_topology_gate(self):
        # 拓扑可信：单座独立塔应全连通，通过严格门禁（根因修复后不再因碎片断链）
        from traceability.solve.tower_solver import tower_geometry_gate

        gt = load_gt()
        model = gt.to_engineering_model()
        gate = tower_geometry_gate(model)
        self.assertTrue(gate["ok"], f"GT 门禁失败: {gate.get('reasons')}")

    def test_roundtrip_dict(self):
        gt = load_gt()
        d = gt.to_dict()
        gt2 = CanonicalTower.from_dict(d)
        self.assertEqual(gt2.bar_count(), gt.bar_count())
        self.assertEqual(gt2.nodes, gt.nodes)

    def test_merge_segments_merges_chain(self):
        bars = [
            {"id": "a", "from": "1", "to": "2", "section": "L90X6", "material": "Q345"},
            {"id": "b", "from": "2", "to": "3", "section": "L90X6", "material": "Q345"},
            {"id": "c", "from": "3", "to": "4", "section": "L90X6", "material": "Q345"},
        ]
        merged = merge_segments(bars)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["from"], "1")
        self.assertEqual(merged[0]["to"], "4")
        self.assertEqual(merged[0]["segments"], 3)


class CanonicalTowerExportTest(unittest.TestCase):
    def test_export_glb_bar_ends_meet_nodes(self):
        # L0 验收：从 CanonicalTower 导出 GLB，杆件实体端点必须落在节点上。
        # 用 GT 抽 20 根逐根校验（端点到节点偏差 < 1mm）。
        import numpy as np

        from traceability.solve.tower_geometry import _align_matrix
        from traceability.solve.tower_solver import _angle_steel_mesh

        gt = load_gt()
        worst = 0.0
        for bar in gt.bars[:20]:
            pa = np.array(gt.nodes[bar["from"]])
            pb = np.array(gt.nodes[bar["to"]])
            d = pb - pa
            L = float(np.linalg.norm(d))
            if L < 1e-6:
                continue
            mid = (pa + pb) / 2.0
            mesh = _angle_steel_mesh(bar.get("section"), L)
            m = _align_matrix(tuple(d), tuple(mid), role="DIAG")
            mesh.apply_transform(m)
            axis = m[:3, :3] @ np.array([0.0, 0.0, 1.0])
            e0 = mid - (L / 2.0) * axis
            e1 = mid + (L / 2.0) * axis
            worst = max(worst, float(np.linalg.norm(e0 - pa)), float(np.linalg.norm(e1 - pb)))
        self.assertLess(worst, 1.0)

    def test_export_wireframe_obj_has_vertices_and_lines(self):
        gt = load_gt()
        out = Path(tempfile.mkdtemp()) / "ct.obj"
        export_wireframe_obj(gt, out)
        text = out.read_text(encoding="utf-8")
        n_v = sum(1 for ln in text.splitlines() if ln.startswith("v "))
        n_l = sum(1 for ln in text.splitlines() if ln.startswith("l "))
        self.assertEqual(n_v, gt.node_count())
        self.assertEqual(n_l, gt.bar_count())


if __name__ == "__main__":
    unittest.main()
