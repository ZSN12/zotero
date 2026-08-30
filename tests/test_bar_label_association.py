"""件号关联 P0/P1 验收测试。

覆盖：
    * P1 正则排除：材质 Q235/Q345/Q420、截面 L40X3、螺栓 1M16X40
    * P0 bar -> 最近合法 text + 一对一贪心（文字不重复用）
    * P1 重复件号「一號多杆」报告 + bar_id_primary 消歧标记
    * 闲鱼国网 35A1-JC1-02 验收：bars>0、association_rate >= 0.30
"""

from __future__ import annotations

import unittest
from pathlib import Path

import pytest

from traceability.intake.tower_dxf import (
    _compile_bar_id_re,
    _extract_bar_label,
    extract_tower_from_dxf,
)

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
GUOWANG_DXF = EXAMPLES / "external" / "guowang_35A1" / "35A1-JC1-02.dxf"
GUOWANG_OVERLAY = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"


class BarLabelRegexTest(unittest.TestCase):
    def setUp(self):
        self.re = _compile_bar_id_re([
            r"\d{1,5}", r"[A-Za-z]{0,3}\d{1,5}", r"M\d{4}", r"[GSB]\d{1,4}",
        ])

    def label(self, text):
        return _extract_bar_label(text, self.re)

    def test_material_excluded(self):
        self.assertIsNone(self.label("Q345"))
        self.assertIsNone(self.label("Q235"))
        self.assertIsNone(self.label("Q420"))
        self.assertIsNone(self.label("Q345-6"))
        self.assertIsNone(self.label("Q345L63X5"))

    def test_section_excluded(self):
        self.assertIsNone(self.label("L40X3"))
        self.assertIsNone(self.label("L50X4"))
        self.assertIsNone(self.label("L100x7"))

    def test_bolt_excluded(self):
        self.assertIsNone(self.label("M16X40"))
        self.assertIsNone(self.label("1M16X40"))
        self.assertIsNone(self.label("2M16X50"))
        self.assertIsNone(self.label("13M16X40"))

    def test_valid_bar_ids_kept(self):
        self.assertEqual(self.label("885"), "885")
        self.assertEqual(self.label("M0001"), "M0001")
        self.assertEqual(self.label("G01"), "G01")


@pytest.mark.integration
@pytest.mark.slow
class GuowangAssociationTest(unittest.TestCase):
    def test_parse_report_meets_acceptance(self):
        if not GUOWANG_DXF.exists():
            self.skipTest("35A1-JC1-02.dxf 未就绪")
        model = extract_tower_from_dxf(GUOWANG_DXF, layer_map_path=GUOWANG_OVERLAY)
        df = model.components["drawing_file"]
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        self.assertGreater(len(bars), 0)
        rate = df.properties.get("association_rate", 0.0)
        # P0 验收：>= 0.30（实现目标 0.40+）
        self.assertGreaterEqual(rate, 0.30)

    def test_duplicate_report_present_with_primary(self):
        if not GUOWANG_DXF.exists():
            self.skipTest("35A1-JC1-02.dxf 未就绪")
        model = extract_tower_from_dxf(GUOWANG_DXF, layer_map_path=GUOWANG_OVERLAY)
        df = model.components["drawing_file"]
        self.assertGreaterEqual(df.properties.get("duplicate_bar_id_groups", 0), 1)
        detail = df.properties.get("duplicate_bar_id_detail", [])
        self.assertTrue(detail)
        # 每个重复组都有 primary 消歧
        for group in detail[:10]:
            self.assertIn("primary", group)
            self.assertGreaterEqual(group["count"], 2)
        # 存在 bar_id_primary 标记的杆件
        primaries = [c for c in model.components.values()
                     if c.kind == "tower_bar" and c.properties.get("bar_id_primary")]
        self.assertGreater(len(primaries), 0)

    def test_topology_still_closed(self):
        if not GUOWANG_DXF.exists():
            self.skipTest("35A1-JC1-02.dxf 未就绪")
        from traceability.harness.tower_validators import validate_topology_closed
        from traceability.harness.tower_validators import inject_tower_rules
        from traceability.model import ValidationStatus
        model = extract_tower_from_dxf(GUOWANG_DXF, layer_map_path=GUOWANG_OVERLAY)
        inject_tower_rules(model)
        result = validate_topology_closed(model, "r_topology_closed")
        self.assertEqual(result.status, ValidationStatus.PASSED)


class OneToOneGreedyUnitTest(unittest.TestCase):
    def test_greedy_direction_assigns_both_bars(self):
        """两个文字都更靠近第一根杆，但第二根杆也在 snap 内：
        旧逻辑（text -> 最近 bar + 同 handle 去重）只会给第一根贴号；
        新逻辑（bar -> 最近 text + 一对一贪心）应让两根杆都有号。"""
        import ezdxf
        import tempfile
        d = tempfile.mkdtemp()
        p = Path(d) / "t.dxf"
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "1"})
        msp.add_line((200, 0), (300, 0), dxfattribs={"layer": "1"})
        msp.add_text("M0001", dxfattribs={"layer": "0", "height": 10}).set_placement((50, 0))
        msp.add_text("M0002", dxfattribs={"layer": "0", "height": 10}).set_placement((80, 0))
        doc.saveas(p)

        model = extract_tower_from_dxf(p, layer_map={
            "bar_layers": ["1"], "text_layers": ["0"],
            "node_layers": ["1"], "dim_layers": [],
        })
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        self.assertEqual(len(bars), 2)
        labeled = {b.properties.get("bar_id") for b in bars}
        self.assertEqual(labeled, {"M0001", "M0002"})


if __name__ == "__main__":
    unittest.main()
