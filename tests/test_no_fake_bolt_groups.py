"""回归测试：立面图（无 detail 区域）不得做节点大样提取（2026-08-31 假 bolt_group 事故）。

背景：04/05/06/07 分段立面图按文件名规则（-04 → 03+ 序号）被判 node_detail，
旧版 extract_detail_connections 的 fallback `or list(regions)` 会把整个
front 区域（含材料表 BOM）当节点大样处理：
- BOM 表中的螺栓条目（'9M16X40' 等）被当作孔位标注
- 表格符号圆（r=4，密集排布）被抓为螺栓孔
- 产生 113 个必然失败的假 bolt_group 验算规则（孔间距 2.5mm、孔在轮廓外）

修复后：仅 kind="detail" 区域参与提取；无 detail 区域 → 空报告，不注入任何规则。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from traceability.intake.tower_detail import extract_detail_connections


def _fake_msp_with_circles_and_texts(circles, texts):
    """构造带 CIRCLE/TEXT 实体的假 modelspace。"""
    msp = MagicMock()
    entities = []
    for cx, cy, r in circles:
        e = MagicMock()
        e.dxftype.return_value = "CIRCLE"
        e.dxf.center.x = cx
        e.dxf.center.y = cy
        e.dxf.radius = r
        entities.append(e)
    for tx, ty, t in texts:
        e = MagicMock()
        e.dxftype.return_value = "TEXT"
        e.dxf.text = t
        e.dxf.insert.x = tx
        e.dxf.insert.y = ty
        entities.append(e)
    msp.__iter__ = lambda self: iter(entities)
    return msp


class TestNoDetailRegionNoExtraction(unittest.TestCase):
    """立面图（front region）不触发节点大样提取。"""

    def test_front_only_regions_yield_empty_report(self):
        """只有 front 区域（立面图）时：零提取、零规则注入。"""
        model = MagicMock()
        model.components = {"drawing_file": MagicMock(properties={})}
        model.add_component = MagicMock()
        model.add_rule = MagicMock()
        model.rules = {}

        # 模拟 BOM 表场景：密集圆 + 螺栓条目文本（旧 bug 的触发条件）
        circles = [(i * 2.5, 300.0, 4.0) for i in range(20)]
        texts = [(10.0, 320.0, "9M16X40"), (50.0, 320.0, "7M20X45")]
        msp = _fake_msp_with_circles_and_texts(circles, texts)

        front_region = {
            "kind": "front",
            "title": "正立面",
            "region": [0.0, 1000.0, 0.0, 1000.0],
            "origin": [0.0, 0.0],
        }
        report = extract_detail_connections(
            model, msp, [front_region], "35A1-JC1-04", "fake.dxf",
        )

        self.assertEqual(report["plates"], 0)
        self.assertEqual(report["bolt_groups"], 0)
        self.assertEqual(report["rules"], [])
        self.assertTrue(report.get("skipped_no_detail_region"))
        model.add_component.assert_not_called()
        model.add_rule.assert_not_called()

    def test_empty_regions_yield_empty_report(self):
        """区域列表为空：零提取。"""
        model = MagicMock()
        report = extract_detail_connections(
            model, MagicMock(), [], "35A1-JC1-04", "fake.dxf",
        )
        self.assertEqual(report["bolt_groups"], 0)
        self.assertTrue(report.get("skipped_no_detail_region"))

    def test_detail_region_still_extracts(self):
        """kind=detail 区域存在时：提取照常（BOM 表圆+文本也会被尝试关联）。"""
        model = MagicMock()
        model.components = {"drawing_file": MagicMock(properties={})}
        model.add_component = MagicMock()
        model.add_rule = MagicMock()
        model.rules = {}

        circles = [(100.0, 100.0, 8.75), (160.0, 100.0, 8.75)]
        texts = [(120.0, 130.0, "2M16X40")]
        msp = _fake_msp_with_circles_and_texts(circles, texts)

        detail_region = {
            "kind": "detail",
            "title": "节点大样",
            "region": [0.0, 300.0, 0.0, 300.0],
            "origin": [0.0, 0.0],
        }
        report = extract_detail_connections(
            model, msp, [detail_region], "35A1-JC1-03", "fake.dxf",
        )
        # detail 区域有螺栓标注 + 孔圆 → 应产生 bolt_group
        self.assertGreaterEqual(report["bolt_groups"], 1)
        self.assertFalse(report.get("skipped_no_detail_region"))
        model.add_component.assert_called()


if __name__ == "__main__":
    unittest.main()
