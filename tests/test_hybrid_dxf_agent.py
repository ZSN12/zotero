"""Hybrid DXF + 可插拔 MLLM Agent 链测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
DXF_02 = EXAMPLES / "external" / "guowang_35A1" / "35A1-JC1-02.dxf"
OVERLAY = EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json"


class HybridDxfAgentTest(unittest.TestCase):
    def test_px_drawing_roundtrip(self):
        from traceability.intake.hybrid_dxf_agent import (
            drawing_xy_to_px,
            px_to_drawing_xy,
        )

        m = {"xlim": (0.0, 1000.0), "ylim": (0.0, 2000.0), "width": 500, "height": 1000}
        px, py = drawing_xy_to_px(250.0, 1500.0, m)
        x, y = px_to_drawing_xy(px, py, m)
        self.assertAlmostEqual(x, 250.0, places=1)
        self.assertAlmostEqual(y, 1500.0, places=1)

    def test_layout_views_from_overlay(self):
        if not OVERLAY.exists():
            self.skipTest("overlay 不存在")
        from traceability.intake.hybrid_dxf_agent import _layout_views_for_overlay

        mapping = {"width": 2000, "height": 3000, "xlim": (34000.0, 35000.0), "ylim": (-8000.0, -7000.0)}
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "fake.png"
            from PIL import Image
            Image.new("RGB", (2000, 3000), "white").save(png)
            views = _layout_views_for_overlay("35A1-JC1-02", mapping, OVERLAY, str(png))
        self.assertTrue(views)
        self.assertNotEqual(views[0]["bbox"], [0, 0, 2000, 3000])

    def test_vector_a3_pass_without_mllm(self):
        if not DXF_02.exists():
            self.skipTest("国网 02 立面不存在")

        from traceability.intake import hybrid_dxf_agent
        from traceability.intake.mllm_backend import MLLMBackend

        with tempfile.TemporaryDirectory() as tmp:
            fake_png = Path(tmp) / "preview.png"
            try:
                from PIL import Image
                Image.new("RGB", (1000, 2000), "white").save(fake_png)
            except ImportError:
                self.skipTest("PIL 未安装")
            fake_mapping = {
                "png": str(fake_png),
                "width": 1000,
                "height": 2000,
                "xlim": (0.0, 10000.0),
                "ylim": (0.0, 40000.0),
                "dpi": 400,
            }
            with mock.patch.object(
                hybrid_dxf_agent,
                "render_dxf_preview_with_mapping",
                return_value=fake_mapping,
            ):
                with mock.patch.object(MLLMBackend, "call_agent_json", return_value=(None, {})):
                    with mock.patch.object(MLLMBackend, "available", return_value=True):
                        result = hybrid_dxf_agent.run_hybrid_dxf_agent_pipeline(
                            DXF_02,
                            tmp,
                            layer_map_path=str(OVERLAY),
                            mllm=MLLMBackend(api_key="sk-test", model="test-mllm"),
                            use_ocr_fallback=False,
                            geom_method="ezdxf",  # P1：矢量路径显式走 ezdxf，不依赖 auto 回退
                        )
            steps = json.loads(Path(result["steps_path"]).read_text(encoding="utf-8"))
            a3 = next(s for s in steps["steps"] if s["id"] == "a3_link")
            self.assertIn(a3["status"], ("passed", "finished", "ok"))

    def test_hybrid_pipeline_mock_mllm(self):
        if not DXF_02.exists():
            self.skipTest("国网 02 立面不存在")

        from traceability.intake import hybrid_dxf_agent
        from traceability.intake.mllm_backend import MLLMBackend

        fake_labels = {
            "labels": [
                {"text": "101", "bar_id": "101", "x_px": 100.0, "y_px": 100.0, "view": "front"},
            ],
            "note": "",
        }

        def fake_call(self, prompt, image_path=None, schema=None, agent="agent"):
            return fake_labels, {
                "model": "test-mllm",
                "provider": "openai",
                "duration_ms": 1.0,
                "elapsed_s": 0.001,
            }

        with tempfile.TemporaryDirectory() as tmp:
            fake_png = Path(tmp) / "preview.png"
            try:
                from PIL import Image
                Image.new("RGB", (1000, 2000), "white").save(fake_png)
            except ImportError:
                self.skipTest("PIL 未安装")
            fake_mapping = {
                "png": str(fake_png),
                "width": 1000,
                "height": 2000,
                "xlim": (0.0, 10000.0),
                "ylim": (0.0, 40000.0),
                "dpi": 100,
            }
            with mock.patch.object(
                hybrid_dxf_agent,
                "render_dxf_preview_with_mapping",
                return_value=fake_mapping,
            ):
                with mock.patch.object(MLLMBackend, "call_agent_json", fake_call):
                    with mock.patch.object(MLLMBackend, "available", return_value=True):
                        result = hybrid_dxf_agent.run_hybrid_dxf_agent_pipeline(
                            DXF_02,
                            tmp,
                            layer_map_path=str(OVERLAY),
                            mllm=MLLMBackend(api_key="sk-test", model="test-mllm"),
                            use_ocr_fallback=False,
                        )
            self.assertTrue(Path(result["steps_path"]).exists())
            steps = json.loads(Path(result["steps_path"]).read_text(encoding="utf-8"))
            step_ids = [s["id"] for s in steps["steps"]]
            self.assertIn("a2_geom", step_ids)  # P1 统一：a2_vector → a2_geom
            self.assertIn("a1_labels", step_ids)
            self.assertIn("a3_link", step_ids)
            self.assertIn("a4_harness", step_ids)
            summary = json.loads((Path(tmp) / "harness_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["mode"], "hybrid_dxf_agent")
            self.assertIn("mllm_provider", summary)


class HybridM3MergeRegressionTest(unittest.TestCase):
    """P0-1 回归：MLLM 注入节点必须带 view_x/view_y + view_type=front，
    否则进不了 merge_view_coordinates 的 front 单立面分支、解不出 Z。"""

    def _overlay(self):
        return str(EXAMPLES / "external" / "guowang_35A1" / "layer_overlay.json")

    def test_drawing_xy_to_view_xy_applies_origin_scale_zflip(self):
        from traceability.intake.hybrid_dxf_agent import _drawing_xy_to_view_xy

        region = {
            "kind": "front",
            "origin": [34574.3, -9101.3],
            "scale_x": 20.0,
            "scale_y": 20.0,
            "z_flip": True,
        }
        # 图纸绝对坐标 (34574.3, -9101.3) 即 origin → 局部 (0, 0)
        vx, vy = _drawing_xy_to_view_xy(region, 34574.3, -9101.3)
        self.assertAlmostEqual(vx, 0.0, places=1)
        self.assertAlmostEqual(vy, 0.0, places=1)
        # 一个偏移点：dx=+10 图纸单位 → vx=+200；dy=-10 → 局部 -10 → z_flip → +10 → vy=+200
        vx2, vy2 = _drawing_xy_to_view_xy(region, 34584.3, -9111.3)
        self.assertAlmostEqual(vx2, 200.0, places=1)
        self.assertAlmostEqual(vy2, 200.0, places=1)

    def test_drawing_xy_to_view_xy_no_region_passthrough(self):
        from traceability.intake.hybrid_dxf_agent import _drawing_xy_to_view_xy

        vx, vy = _drawing_xy_to_view_xy(None, 123.4, -56.7)
        self.assertAlmostEqual(vx, 123.4, places=1)
        self.assertAlmostEqual(vy, -56.7, places=1)

    def test_strip_vector_geometry_removes_nodes_and_bars(self):
        from traceability.intake.hybrid_dxf_agent import _strip_vector_geometry
        from traceability.model import Component, EngineeringModel, SourceRef, SourceType

        m = EngineeringModel(name="t")
        m.add_component(Component(
            id="node_1", name="n1", kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "x", confidence=0.5),
            properties={"x": 1.0, "y": 2.0},
        ))
        m.add_component(Component(
            id="bar_1", name="b1", kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "x", confidence=0.5),
            properties={"from_node": "node_1", "to_node": "node_2"},
        ))
        m.add_component(Component(
            id="drawing_file", name="df", kind="drawing_file",
            source=SourceRef(SourceType.DRAWING, "x", confidence=0.5),
            properties={},
        ))
        removed = _strip_vector_geometry(m)
        self.assertEqual(removed, 2)
        kinds = {c.kind for c in m.components.values()}
        self.assertEqual(kinds, {"drawing_file"})

    def test_strip_vector_geometry_cleans_dimensions_and_global_rules(self):
        """P0 修复：_strip_vector_geometry 应清理 Dimension.applies_to 指向被删
        杆件的尺寸，且不误删空 applies_to 的全局规则。"""
        from traceability.intake.hybrid_dxf_agent import _strip_vector_geometry
        from traceability.model import (
            Component, Dimension, EngineeringModel, Rule, SourceRef, SourceType,
        )

        m = EngineeringModel(name="t")
        m.add_component(Component(
            id="bar_1", name="b1", kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "x", confidence=0.5),
            properties={"from_node": "n1", "to_node": "n2"},
        ))
        m.add_component(Component(
            id="drawing_file", name="df", kind="drawing_file",
            source=SourceRef(SourceType.DRAWING, "x", confidence=0.5),
            properties={},
        ))
        # 指向被删杆件的尺寸（应被清理）
        m.dimensions["dim_len_bar_1"] = Dimension(
            id="dim_len_bar_1", name="长度", value=100.0, unit="mm",
            applies_to="bar_1",
        )
        # 指向保留构件的尺寸（应保留）
        m.dimensions["dim_len_keep"] = Dimension(
            id="dim_len_keep", name="长度", value=50.0, unit="mm",
            applies_to="drawing_file",
        )
        # 指向被删杆件的规则（应被清理）
        m.rules["r_bar"] = Rule(
            id="r_bar", name="r", description="d", applies_to=["bar_1"],
        )
        # 全局规则（空 applies_to，应保留）
        m.rules["r_global"] = Rule(
            id="r_global", name="g", description="d", applies_to=[],
        )

        _strip_vector_geometry(m)

        self.assertNotIn("dim_len_bar_1", m.dimensions)
        self.assertIn("dim_len_keep", m.dimensions)
        self.assertNotIn("r_bar", m.rules)
        self.assertIn("r_global", m.rules)

    def test_ensure_node_dedups_by_distance_and_view_type(self):
        """P2 修复：空间哈希须按真实距离（≤1mm）+ view_type 分键，避免
        超距或跨视图误合并。"""
        from traceability.intake.hybrid_geometry import inject_mllm_bars_into_model
        from traceability.model import EngineeringModel

        m = EngineeringModel(name="t")
        # 两根杆：同一视图、端点相距 0.5mm（应共享节点）+ 相距 2mm（应独立节点）
        bars = [
            {"bar_uid": "b1", "x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0, "view_type": "front"},
            # 端点 (0.4, 0.3) 距 (0,0) ≈ 0.5mm → 应共享节点
            {"bar_uid": "b2", "x1": 0.4, "y1": 0.3, "x2": 20.0, "y2": 0.0, "view_type": "front"},
            # 端点 (3, 0) 距任何已有节点都 >1mm → 应独立节点
            {"bar_uid": "b3", "x1": 3.0, "y1": 0.0, "x2": 30.0, "y2": 0.0, "view_type": "front"},
            # 同坐标但不同 view_type → 应独立节点（不跨视图合并）
            {"bar_uid": "b4", "x1": 0.0, "y1": 0.0, "x2": 40.0, "y2": 0.0, "view_type": "side"},
        ]
        injected = inject_mllm_bars_into_model(m, bars, view_type="front")
        self.assertEqual(injected, 4)
        nodes = [c for c in m.components.values() if c.kind == "tower_node"]
        # b1/b2 共享一个节点 (0,0)，b1 另一端 + b2 另一端 + b3 两端 + b4 两端
        # = 1(shared) + 1 + 1 + 2 + 2 = 7 个节点
        self.assertEqual(len(nodes), 7)

    def test_mllm_injected_nodes_enter_front_merge(self):
        """04 段：注入 view_x/view_y + view_type=front 后，merge 解出 Z=23000~29800。"""
        if not OVERLAY.exists():
            self.skipTest("overlay 不存在")
        from traceability.intake.hybrid_dxf_agent import _drawing_xy_to_view_xy, _inject_mllm_bars_into_model
        from traceability.intake.tower_spec import view_region
        from traceability.intake.tower_views import merge_view_coordinates
        from traceability.model import EngineeringModel

        m = EngineeringModel(name="35A1-JC1-04")
        region = view_region("35A1-JC1-04", "front", overlay=self._overlay())
        # 用 04 region 的 origin 构造一根竖直杆（图纸坐标），模拟 MLLM 注入
        ox, oy = region["origin"]
        bars = [
            {
                "bar_uid": "b1", "x1": ox, "y1": oy,
                "x2": ox, "y2": oy - 50.0,  # 沿图纸 y 向下（z_flip 后向上）
                "view_type": "front", "geometry_origin": "mllm_geom",
            },
        ]
        injected = _inject_mllm_bars_into_model(
            m, bars, view_type="front",
            stem="35A1-JC1-04", layer_map_path=self._overlay(),
        )
        self.assertEqual(injected, 1)
        # 节点必须带 view_x/view_y + view_type=front
        nodes = [c for c in m.components.values() if c.kind == "tower_node"]
        self.assertEqual(len(nodes), 2)
        for n in nodes:
            self.assertEqual(n.properties.get("view_type"), "front")
            self.assertIsNotNone(n.properties.get("view_x"))
            self.assertIsNotNone(n.properties.get("view_y"))
            self.assertEqual(n.properties.get("drawing_view"), "35A1-JC1-04")

        merged = merge_view_coordinates(m, overlay=self._overlay())
        zs = [v["z"] for v in merged.values() if v.get("z") is not None]
        self.assertEqual(len(zs), 2)
        # 04 段 z_offset=23000，z_span_mm=6800，解出的 Z 应落在该区间内
        for z in zs:
            self.assertGreaterEqual(z, 23000.0)
            self.assertLessEqual(z, 29800.0)


if __name__ == "__main__":
    unittest.main()
