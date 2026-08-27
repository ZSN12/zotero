"""110kV 铁塔端到端集成测试（P0/P1 验收）。

覆盖：
    * tower_110kv.dxf 解析 >0 杆件（LEG/HORIZ/DIAG/CROSS/HEAD/KNEE 图层）
    * BOM 交叉核验引用完整性（无悬空 applies_to / 依赖）
    * intake-tower 自动注入五条铁塔规则
    * 跨视图坐标合并：85 个节点全部得到三轴坐标
    * 求解结果与 tower_110kv_golden.json 对齐，偏差 <2% / <50mm
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from traceability.intake.tower_dxf import extract_tower_from_dxf
from traceability.intake.tower_bom import parse_bom_csv, cross_check_bom
from traceability.intake.tower_views import merge_view_coordinates, merge_view_bars
from traceability.harness.tower_validators import inject_tower_rules
from traceability.harness.harness import run_harness
from traceability.io import validate_references
from traceability.model import ValidationStatus
from traceability.solve.tower_solver import solve_tower, compare_to_golden

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def tower_components(model, kind):
    return [c for c in model.components.values() if c.kind == kind]


class Tower110kVIntakeTest(unittest.TestCase):
    def setUp(self):
        self.dxf = EXAMPLES / "tower_110kv.dxf"
        self.bom = EXAMPLES / "tower_110kv_bom.csv"
        self.golden = EXAMPLES / "tower_110kv_golden.json"

    def test_parse_110kv_produces_bars_and_nodes(self):
        model = extract_tower_from_dxf(self.dxf)
        bars = tower_components(model, "tower_bar")
        nodes = tower_components(model, "tower_node")
        self.assertGreater(len(bars), 0)
        self.assertGreater(len(nodes), 0)
        # 图层映射生效：至少能抽到主材
        layers = {b.properties.get("layer") for b in bars}
        self.assertIn("LEG", layers)
        # 编号正则支持 M\d{4}
        labeled = [b for b in bars if not b.properties["bar_id"].startswith("UNLABELED")]
        self.assertGreater(len(labeled), 0)

    def test_bom_cross_check_has_no_broken_references(self):
        model = extract_tower_from_dxf(self.dxf)
        model = cross_check_bom(model, parse_bom_csv(self.bom))
        problems = validate_references(model)
        self.assertEqual(problems, [])

    def test_intake_injects_five_tower_rules(self):
        model = extract_tower_from_dxf(self.dxf)
        inject_tower_rules(model)
        expected = {
            "r_topology_closed", "r_bom_length_match", "r_bom_section_match",
            "r_node_fully_solved", "r_no_duplicate_bar_id",
        }
        self.assertTrue(expected.issubset(set(model.rules)))

    def test_merge_solves_all_85_nodes(self):
        model = extract_tower_from_dxf(self.dxf)
        merged = merge_view_coordinates(model)
        front = [c for c in tower_components(model, "tower_node")
                 if c.properties.get("view_type") == "front"]
        self.assertEqual(len(front), 85)
        solved = [c for c in front
                  if None not in (c.properties.get("x"), c.properties.get("y"),
                                  c.properties.get("z"))]
        self.assertEqual(len(solved), 85)

    def test_merged_bars_pass_all_rules(self):
        model = extract_tower_from_dxf(self.dxf)
        merge_view_coordinates(model)
        model = merge_view_bars(model)
        model = cross_check_bom(model, parse_bom_csv(self.bom))
        inject_tower_rules(model)
        self.assertEqual(validate_references(model), [])

        results = run_harness(model)
        statuses = {r.target_id: r.status for r in results}
        self.assertEqual(len(results), 5)
        for rid in ("r_topology_closed", "r_bom_length_match", "r_bom_section_match",
                    "r_node_fully_solved", "r_no_duplicate_bar_id"):
            self.assertIn(rid, statuses)
            self.assertNotEqual(statuses[rid], ValidationStatus.FAILED, rid)

    def test_solved_coordinates_match_golden(self):
        model = extract_tower_from_dxf(self.dxf)
        merge_view_coordinates(model)
        model = merge_view_bars(model)
        nodes, problems = solve_tower(model)
        self.assertEqual(problems, [])
        report = compare_to_golden(nodes, self.golden)
        self.assertTrue(report["passed"], report)
        self.assertLessEqual(report["max_dev_mm"], 50.0)
        self.assertLessEqual(report["max_rel"], 0.02)

    def test_model_matches_json_schema(self):
        from traceability.io import validate_against_schema
        model = extract_tower_from_dxf(self.dxf)
        merge_view_coordinates(model)
        model = merge_view_bars(model)
        model = cross_check_bom(model, parse_bom_csv(self.bom))
        inject_tower_rules(model)
        self.assertEqual(validate_against_schema(model), [])

    def test_generated_dxf_shares_layer_spec(self):
        import ezdxf
        from traceability.intake.tower_real_dxf import make_real_tower_dxf
        with tempfile.TemporaryDirectory() as d:
            dxf = make_real_tower_dxf(Path(d) / "tower_110kv.dxf")
            doc = ezdxf.readfile(dxf)
            layer_names = {layer.dxf.name for layer in doc.layers}
            self.assertTrue({"LEG", "HORIZ", "DIAG", "CROSS", "HEAD", "KNEE"}
                            .issubset(layer_names))

    def test_compile_drawing_tower_chain(self):
        from traceability.cli import main
        from traceability.io import load_model
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "compiled.json"
            main(["compile-drawing", str(self.dxf), "--tower", "--bom", str(self.bom),
                  "--merge", "--golden", str(self.golden), "--out", str(out)])
            model = load_model(out)
            self.assertEqual(len(model.rules), 5)
            bars = tower_components(model, "tower_bar")
            nodes = tower_components(model, "tower_node")
            self.assertEqual(len(bars), 316)
            self.assertEqual(len(nodes), 85)

    def test_glb_export(self):
        try:
            import trimesh  # noqa: F401
        except ImportError:
            self.skipTest("trimesh 未安装")
        from traceability.solve.tower_solver import export_tower_glb
        model = extract_tower_from_dxf(self.dxf)
        merge_view_coordinates(model)
        model = merge_view_bars(model)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "tower.glb"
            path = export_tower_glb(model, out)
            self.assertEqual(path, str(out))
            self.assertGreater(out.stat().st_size, 1000)

    def test_cli_end_to_end_chain(self):
        from traceability.cli import main
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "model.json"
            obj = Path(d) / "tower.obj"
            main(["intake-tower", str(self.dxf), "--bom", str(self.bom),
                  "--merge", "--out", str(out)])
            main(["validate", str(out)])
            main(["harness", str(out)])
            main(["solve-tower", str(out), "--out", str(obj),
                  "--golden", str(self.golden)])
            self.assertTrue(out.exists())
            self.assertTrue(obj.exists())


if __name__ == "__main__":
    unittest.main()
