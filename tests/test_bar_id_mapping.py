"""图纸件号 ↔ 计算模型件号映射（BOM 数字件号 → GT PM_XXXX）的单元测试。"""

from __future__ import annotations

import unittest

from traceability.project.bar_id_mapping import (
    _normalize_section,
    _gt_bars_by_sec_len,
    build_bar_id_mapping,
    mapping_to_bar_map,
)


class NormalizeSectionTest(unittest.TestCase):
    def test_strips_material_prefix(self):
        self.assertEqual(_normalize_section("Q345L63X5"), "L63X5")
        self.assertEqual(_normalize_section("Q235L40X3"), "L40X3")
        self.assertEqual(_normalize_section("L40X3"), "L40X3")

    def test_normalizes_case_and_space(self):
        self.assertEqual(_normalize_section(" l40x3 "), "L40X3")
        self.assertEqual(_normalize_section("Q345 L63X5"), "L63X5")


class BuildBarIdMappingTest(unittest.TestCase):
    def _gt(self):
        """构造最小 GT：2 根 L40X3 杆（长度 1384/1013），1 根 L50X4（1618）。"""
        return {
            "nodes": {
                "n1": [0, 0, 0], "n2": [1384, 0, 0],
                "n3": [0, 0, 100], "n4": [1013, 0, 100],
                "n5": [0, 0, 200], "n6": [1618, 0, 200],
            },
            "bars": [
                {"id": "PM_0001", "from": "n1", "to": "n2", "section": "L40X3"},
                {"id": "PM_0002", "from": "n3", "to": "n4", "section": "L40X3"},
                {"id": "PM_0003", "from": "n5", "to": "n6", "section": "L50X4"},
            ],
        }

    def test_angle_section_mapped_by_length(self):
        gt = self._gt()
        bom = [
            {"bar_id": "105", "section": "Q345L40X3", "length_mm": "1436", "qty": "1"},
            {"bar_id": "110", "section": "L40X3", "length_mm": "1023", "qty": "1"},
            {"bar_id": "122", "section": "Q345L50X4", "length_mm": "1609", "qty": "1"},
        ]
        r = build_bar_id_mapping(gt, bom)
        self.assertEqual(r["assigned"], 3)
        # 105 -> 1384mm（差 52 <= 60）
        self.assertEqual(r["mapping"]["105"]["gt_ids"], ["PM_0001"])
        self.assertEqual(r["mapping"]["110"]["gt_ids"], ["PM_0002"])
        self.assertEqual(r["mapping"]["122"]["gt_ids"], ["PM_0003"])

    def test_plate_section_excluded(self):
        gt = self._gt()
        bom = [{"bar_id": "126", "section": "-6X207", "length_mm": "320", "qty": "8"}]
        r = build_bar_id_mapping(gt, bom)
        self.assertEqual(r["assigned"], 0)
        self.assertEqual(len(r["unassigned"]), 1)
        self.assertIn("非角钢", r["unassigned"][0]["reason"])

    def test_unmatched_angle_reported(self):
        gt = self._gt()
        bom = [{"bar_id": "109", "section": "L63X5", "length_mm": "836", "qty": "4"}]
        r = build_bar_id_mapping(gt, bom)
        self.assertEqual(r["assigned"], 0)
        self.assertEqual(len(r["unassigned"]), 1)


class MappingToBarMapTest(unittest.TestCase):
    def test_flattens_to_bar_map(self):
        result = {
            "mapping": {
                "105": {"section": "L40X3", "gt_ids": ["PM_0001", "PM_0002"]},
            },
            "assigned": 1,
            "unassigned": [],
            "total": 1,
        }
        bm = mapping_to_bar_map(result)
        self.assertEqual(len(bm), 2)
        self.assertEqual(bm[0]["bar_id"], "105")
        self.assertEqual(bm[0]["gt_id"], "PM_0001")
        self.assertEqual(bm[0]["component_id"], "gt_bar_PM_0001")


if __name__ == "__main__":
    unittest.main()
