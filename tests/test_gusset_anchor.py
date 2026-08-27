"""M4：大样节点板全局锚定测试。"""

from __future__ import annotations

import unittest

from traceability.connection.detail_view import parse_detail_view_meta
from traceability.connection.gusset_anchor import (
    _detail_center_vx_vy,
    anchor_gusset_to_node,
    auto_anchor_gussets,
)
from traceability.model import Component, EngineeringModel


class GussetAnchorTest(unittest.TestCase):
    def test_detail_center_maps_to_front_local(self):
        detail = {
            "region": [34383.0, 34717.0, -8332.0, -8006.0],
            "origin": [34383.0, -8006.0],
        }
        front = {"origin": [34350.0, -7244.0]}
        vx, vy = _detail_center_vx_vy(detail, front)
        self.assertAlmostEqual(vx, 200.0)
        self.assertAlmostEqual(vy, -925.0)

    def test_anchor_writes_polygon_global(self):
        from traceability.connection.gusset import parse_gusset_from_detail

        model = EngineeringModel(name="t")
        t = parse_detail_view_meta("节点 K1 大样 1:10")
        plate = parse_gusset_from_detail(
            "K1", [(0, 0), (100, 0), (100, 80), (0, 80)], transform=t,
        )
        model.add_component(plate.to_component())
        model.add_component(Component(
            id="node_A", name="n", kind="tower_node",
            properties={
                "x": 1000.0, "y": 2000.0, "z": 8100.0,
                "view_type": "front", "view_x": 50.0, "view_y": 100.0,
                "solve_status": "solved",
            },
        ))
        ok = anchor_gusset_to_node(model, "gusset_K1", "node_A", anchor_origin="test")
        self.assertTrue(ok)
        g = model.components["gusset_K1"]
        self.assertEqual(g.properties.get("solve_status"), "verified")
        self.assertGreaterEqual(len(g.properties.get("polygon_global") or []), 3)

    def test_auto_anchor_respects_overlay_explicit(self):
        from traceability.connection.gusset import parse_gusset_from_detail
        from traceability.connection.detail_view import parse_detail_view_meta

        model = EngineeringModel(name="t")
        t = parse_detail_view_meta("detail")
        plate = parse_gusset_from_detail("D1", [(0, 0), (50, 0), (50, 50)], transform=t)
        comp = plate.to_component()
        comp.properties["source_file"] = "sheet03"
        model.add_component(comp)
        model.add_component(Component(
            id="node_target", name="n", kind="tower_node",
            properties={"x": 1.0, "y": 2.0, "z": 3.0, "solve_status": "solved", "view_type": "front"},
        ))
        overlay = {
            "gusset_anchors": {"gusset_D1": "node_target"},
        }
        rep = auto_anchor_gussets(model, overlay=overlay)
        self.assertEqual(len(rep["anchored"]), 1)
        self.assertEqual(rep["anchored"][0]["node"], "node_target")
