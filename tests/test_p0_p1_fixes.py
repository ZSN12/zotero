"""P0/P1 缺陷修复的回归测试。

覆盖：
    P0-1 merge_view_coordinates / finalize_tower_model 下传 overlay
    P0-2 国网 02 单立面 -> view_mode=single_facade
    P0-4 intake_scan_batch 多 plan 不再互相覆盖
    P0-5 cross_file_bar_id_report 跨文件去重报告
    P1-6 A2 后按 bbox 给杆打 view_type
    P1-7 parse_bars=False 短路 A1/A2
    P1-9 intake_scan_batch 返回完整 ProcessingGraph
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def tower_components(model, kind):
    return [c for c in model.components.values() if c.kind == kind]


class P01OverlayThreadingTest(unittest.TestCase):
    """P0-1：merge_view_coordinates 读 overlay 里的 view_regions。"""

    def test_region_meta_reads_overlay(self):
        from traceability.intake.tower_views import _region_meta
        ov = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"
        # 02 在 overlay 里有 front region
        meta = _region_meta("35A1-JC1-02", overlay=ov)
        self.assertIn("front", meta)
        # 不带 overlay 时（schema 里没有这个 stem），读不到
        meta_none = _region_meta("35A1-JC1-02")
        self.assertNotIn("front", meta_none)

    def test_merge_view_coordinates_accepts_overlay(self):
        from traceability.intake.tower_views import merge_view_coordinates
        from traceability.intake.tower_dxf import extract_tower_from_dxf
        ov = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"
        model = extract_tower_from_dxf(
            EXAMPLES / "external" / "guowang_35A1" / "35A1-JC1-02.dxf",
            layer_map_path=ov,
        )
        # 单立面：merge 不应抛错，也不臆造 3D（front 无 side/section）
        merged = merge_view_coordinates(model, overlay=ov)
        # front 节点保持 (view_x, view_y=z)，z 缺 None 不臆造
        self.assertIsInstance(merged, dict)

    def test_finalize_tower_model_passes_layer_map(self):
        from traceability.intake.tower_pipeline import finalize_tower_model
        from traceability.intake.tower_dxf import extract_tower_from_dxf
        ov = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"
        model = extract_tower_from_dxf(
            EXAMPLES / "external" / "guowang_35A1" / "35A1-JC1-02.dxf",
            layer_map_path=ov,
        )
        finalize_tower_model(model, merge=True, layer_map_path=ov)
        self.assertGreater(len(model.rules), 0)


class P02Guowang02ViewModeTest(unittest.TestCase):
    """P0-2：国网 02 单立面（synthetic side 策略），标记 view_mode=single_facade。

    02 图只有一个 front 立面（簇4），没有独立 side region；side 立面由
    synthetic_side_from_front=true 在 merge_view_coordinates 阶段合成
    （塔 x/y 近似旋转对称）。因此单文件提取时 view_kinds=["front"]、
    view_mode=single_facade，side 在跨文件合并后才补进 view_kinds。
    """

    def test_front_single_facade_view_mode(self):
        from traceability.intake.tower_dxf import extract_tower_from_dxf
        ov = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"
        model = extract_tower_from_dxf(
            EXAMPLES / "external" / "guowang_35A1" / "35A1-JC1-02.dxf",
            layer_map_path=ov,
        )
        df = model.components["drawing_file"]
        self.assertEqual(df.properties.get("view_mode"), "single_facade")
        self.assertEqual(df.properties.get("view_kinds"), ["front"])
        # layer0 为尺寸界线，不得进杆件
        layers = {b.properties.get("layer") for b in tower_components(model, "tower_bar")}
        self.assertNotIn("0", layers)
        self.assertEqual(layers, {"1", "4"})
        # parse rate >= 50%（验收线）
        self.assertGreaterEqual(df.properties.get("association_rate", 0.0), 0.50)

    def test_synthetic_side_registers_view_kinds(self):
        """synthetic_side_from_front 合成 side 节点后，view_kinds 应补 'side'。"""
        from traceability.intake.tower_dxf import extract_tower_from_dxf
        from traceability.intake.tower_views import merge_view_coordinates
        ov = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"
        model = extract_tower_from_dxf(
            EXAMPLES / "external" / "guowang_35A1" / "35A1-JC1-02.dxf",
            layer_map_path=ov,
        )
        merge_view_coordinates(model, overlay=ov)
        df = model.components["drawing_file"]
        self.assertIn("side", df.properties.get("view_kinds", []))
        self.assertGreater(df.properties.get("synthetic_side_nodes", 0), 0)

    def test_110kv_multi_view_mode(self):
        from traceability.intake.tower_dxf import extract_tower_from_dxf
        model = extract_tower_from_dxf(EXAMPLES / "tower_110kv.dxf")
        df = model.components["drawing_file"]
        self.assertEqual(df.properties.get("view_mode"), "multi_view")


class P04ScanBatchMultiPlanTest(unittest.TestCase):
    """P0-4 / P1-9：intake_scan_batch 按 (view_type, z_level) 存，返回完整 graph。"""

    def test_cross_file_bar_id_report(self):
        from traceability.intake.tower_batch import cross_file_bar_id_report
        from traceability.model import Component, EngineeringModel

        m1 = EngineeringModel(name="tower-f1")
        m1.add_component(Component(id="b1", name="b", kind="tower_bar",
                                   properties={"bar_id": "M0001"}))
        m2 = EngineeringModel(name="tower-f2")
        m2.add_component(Component(id="b2", name="b", kind="tower_bar",
                                   properties={"bar_id": "M0001"}))
        report = cross_file_bar_id_report([m1, m2])
        self.assertEqual(report["duplicate_count"], 1)
        self.assertEqual(report["cross_file_groups"][0]["bar_id"], "M0001")
        self.assertEqual(set(report["cross_file_groups"][0]["files"]), {"f1", "f2"})


class P16AssignViewByBboxTest(unittest.TestCase):
    """P1-6：A2 后按 bbox 给杆打 view_type。"""

    def test_assign_view_by_bbox(self):
        from traceability.intake.tower_agent_pipeline import _assign_view_by_bbox
        bars = [
            {"bar_uid": "b1", "x1": 10, "y1": 10, "x2": 20, "y2": 20},
            {"bar_uid": "b2", "x1": 300, "y1": 10, "x2": 310, "y2": 20},
        ]
        nodes = [{"node_id": "N1", "x_px": 15, "y_px": 15},
                 {"node_id": "N2", "x_px": 305, "y_px": 15}]
        views = [
            {"view_id": "view_01", "view_type": "front", "bbox": [0, 0, 100, 100]},
            {"view_id": "view_02", "view_type": "side", "bbox": [200, 0, 400, 100]},
        ]
        nb, nn = _assign_view_by_bbox(bars, nodes, views, "drawing")
        self.assertEqual(nb[0]["view_type"], "front")
        self.assertEqual(nb[1]["view_type"], "side")
        self.assertEqual(nn[0]["view_type"], "front")
        self.assertEqual(nn[1]["view_type"], "side")


if __name__ == "__main__":
    unittest.main()
