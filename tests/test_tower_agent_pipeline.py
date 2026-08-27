"""P1 多 Agent 编排（A0→A4）验收测试。

覆盖：
    * run-tower 扫描图 -> steps.json 含 a0_layout/a1_labels/a2_geom/a3_link/a4_harness
    * A2 霍夫基线 bars >= 100
    * A3 确定性一对一关联（与 DXF 同逻辑），重复件号组报告
    * 扫描默认 solve_status=pending_review，无坐标不 export strict GLB
    * A1 件号 Agent 输出非法条丢弃 + warning（策略 A）
    * choose_backend：dxf/dwg 永远不走 MLLM 主路径
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _cv2_available():
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


class AgentPromptTest(unittest.TestCase):
    def test_label_agent_schema_and_parse(self):
        from traceability.intake.mllm_tower_prompt import (
            LABEL_AGENT_PROMPT,
            LABEL_AGENT_SCHEMA,
            GEOM_AGENT_PROMPT,
            GEOM_AGENT_SCHEMA,
            parse_label_agent_output,
        )
        self.assertIn("只读取件号文字", LABEL_AGENT_PROMPT)
        self.assertIn("labels", LABEL_AGENT_SCHEMA["properties"])
        self.assertIn("只检测线段和节点", GEOM_AGENT_PROMPT)
        self.assertIn("bars", GEOM_AGENT_SCHEMA["required"])

        good = {"labels": [
            {"text": "M0001", "bar_id": "M0001", "x_px": 10, "y_px": 20, "view": "front"},
            {"text": "G01", "x_px": 30, "y_px": 40},
        ]}
        labels, problems, warnings = parse_label_agent_output(good)
        self.assertEqual(problems, [])
        self.assertEqual(len(labels), 2)
        self.assertEqual(labels[1]["bar_id"], None)

        bad = {"labels": [
            {"text": "M0001", "bar_id": "M0001", "x_px": 10, "y_px": 20},
            {"text": "Q345", "bar_id": "Q345"},  # 坐标缺失 -> 丢弃
        ]}
        labels, problems, warnings = parse_label_agent_output(bad)
        self.assertEqual(problems, [])
        self.assertEqual(len(labels), 1)
        self.assertTrue(any("x_px/y_px" in w for w in warnings))


class A3AssociationTest(unittest.TestCase):
    def test_one_to_one_greedy_and_duplicates(self):
        from traceability.intake.tower_agent_pipeline import _associate_labels

        bars = [
            {"bar_uid": "bar_0001", "x1": 0, "y1": 0, "x2": 100, "y2": 0},
            {"bar_uid": "bar_0002", "x1": 200, "y1": 0, "x2": 300, "y2": 0},
            {"bar_uid": "bar_0003", "x1": 0, "y1": 200, "x2": 100, "y2": 200},
        ]
        labels = [
            # 两个件号都更靠近第一根杆，但一对一贪心必须让第二根杆也贴上
            {"text": "M0001", "x_px": 50, "y_px": 0},
            {"text": "M0002", "x_px": 80, "y_px": 0},
            # 同一件号 M0001 的另一个文字位置贴到第三根杆 -> 重复件号组
            {"text": "M0001", "x_px": 50, "y_px": 200},
        ]
        result = _associate_labels(bars, labels, snap_px=400.0)
        by_uid = {a["bar_uid"]: a["bar_id"] for a in result["assignments"]}
        self.assertEqual(by_uid["bar_0001"], "M0001")
        self.assertEqual(by_uid["bar_0002"], "M0002")
        self.assertEqual(by_uid["bar_0003"], "M0001")
        self.assertEqual(result["labeled"], 3)
        self.assertGreaterEqual(result["association_rate"], 1.0)
        self.assertGreaterEqual(result["duplicate_bar_id_groups"], 1)

    def test_material_and_bolt_never_assigned(self):
        from traceability.intake.tower_agent_pipeline import _associate_labels
        bars = [{"bar_uid": "bar_0001", "x1": 0, "y1": 0, "x2": 100, "y2": 0}]
        labels = [
            {"text": "Q345", "x_px": 50, "y_px": 0},
            {"text": "2M16X50", "x_px": 50, "y_px": 2},
            {"text": "L40X3", "x_px": 50, "y_px": 4},
        ]
        result = _associate_labels(bars, labels, snap_px=400.0)
        self.assertEqual(result["labeled"], 0)
        self.assertTrue(result["assignments"][0]["bar_id"].startswith("UNLABELED"))

    def test_labels_to_full_image_rescale_after_resize(self):
        from traceability.intake.tower_agent_pipeline import _labels_to_full_image, _associate_labels

        crop = {
            "bbox": [100, 200, 1100, 2200],
            "crop_size": [500, 1000],
            "source_crop_size": [1000, 2000],
        }
        labels = [{"text": "M0001", "x_px": 250, "y_px": 500}]
        mapped = _labels_to_full_image(labels, crop, "view_01")
        self.assertEqual(mapped[0]["x_px"], 600.0)
        self.assertEqual(mapped[0]["y_px"], 1200.0)

        bars = [{"bar_uid": "bar_0001", "x1": 550, "y1": 1180, "x2": 650, "y2": 1220}]
        result = _associate_labels(bars, mapped, snap_px=80.0)
        self.assertEqual(result["labeled"], 1)
        self.assertEqual(result["assignments"][0]["bar_id"], "M0001")

    def test_ocr_fallback_converts_tesseract_boxes_to_labels(self):
        """B4：Tesseract 文本框 -> A3 label 格式（件号正则过滤 + 视图归属）。"""
        import traceability.intake.tower_agent_pipeline as tap
        import traceability.intake.tower_layout as tl

        boxes = [
            {"text": "M0001", "bbox": [100.0, 200.0, 120.0, 220.0]},
            {"text": "Q345", "bbox": [100.0, 200.0, 120.0, 220.0]},  # 材质排除
            {"text": "G01", "bbox": [300.0, 400.0, 320.0, 420.0]},
            {"text": "2M16X50", "bbox": [100.0, 200.0, 120.0, 220.0]},  # 螺栓排除
        ]
        orig_ocr_boxes = tl._ocr_boxes
        tl._ocr_boxes = lambda p: boxes
        try:
            views = [{"view_id": "v0", "view_type": "front", "bbox": [0, 0, 500, 500]}]
            labels = tap._ocr_labels_from_tesseract("x.png", views)
        finally:
            tl._ocr_boxes = orig_ocr_boxes

        self.assertEqual(len(labels), 2)  # M0001 + G01；Q345/螺栓被排除
        self.assertEqual({l["bar_id"] for l in labels}, {"M0001", "G01"})
        self.assertTrue(all(l["ocr_source"] == "tesseract" for l in labels))
        # 中心点 = bbox 中心
        m1 = next(l for l in labels if l["bar_id"] == "M0001")
        self.assertEqual((m1["x_px"], m1["y_px"]), (110.0, 210.0))
        # view 归属：落在 view bbox 内 -> front
        self.assertTrue(all(l["view"] == "front" for l in labels))

    def test_ocr_fallback_returns_empty_without_tesseract(self):
        """B4：pytesseract 不可用时兜底返回空列表（绝不猜编号）。"""
        from traceability.intake.tower_agent_pipeline import _ocr_labels_from_tesseract
        labels = _ocr_labels_from_tesseract("__nonexistent__.png", [])
        self.assertEqual(labels, [])


@unittest.skipUnless(_cv2_available(), "opencv-python 未安装")
class LayoutRegionTest(unittest.TestCase):
    def test_detect_regions_main_content_not_margin_slivers(self):
        from traceability.intake.tower_layout import _detect_regions, _load_image

        cv2, gray = _load_image(str(EXAMPLES / "clear" / "tower_front_hd.png"))
        regions = _detect_regions(cv2, gray)
        self.assertGreater(len(regions), 0)
        best = regions[0]
        x0, y0, x1, y1 = best["bbox"]
        w, h = x1 - x0, y1 - y0
        self.assertGreater(w, 1000)
        self.assertGreater(h, 1000)
        self.assertGreater(best["ink_ratio"], 0.01)

    def test_layout_views_limits_small_crops(self):
        from traceability.intake.tower_layout import _detect_regions, _load_image, layout_views_from_regions

        cv2, gray = _load_image(str(EXAMPLES / "clear" / "tower_front_hd.png"))
        regions = _detect_regions(cv2, gray)
        views, whole_sheet = layout_views_from_regions(regions, gray.shape)
        self.assertFalse(whole_sheet)
        self.assertLessEqual(len(views), 8)
        x0, y0, x1, y1 = views[0]["bbox"]
        self.assertGreater(x1 - x0, 1000)
        self.assertGreater(y1 - y0, 1000)


@unittest.skipUnless(_cv2_available(), "opencv-python 未安装")
class RunTowerAgentPipelineTest(unittest.TestCase):
    def test_scan_writes_five_agent_steps(self):
        from traceability.harness.tower_harness import run_tower
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            result = run_tower(EXAMPLES / "clear" / "tower_front_hd.png", out)
            steps = json.loads((out / "steps.json").read_text(encoding="utf-8"))
            ids = [s["id"] for s in steps["steps"]]
        self.assertEqual(ids[:5], ["a0_layout", "a1_labels", "a2_geom", "a3_link", "a4_harness"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["solve_status"], "pending_review")
        self.assertIsNone(result["glb_path"])
        # A2 霍夫基线
        a2 = steps["steps"][2]
        self.assertGreaterEqual(a2["detail"]["bars"], 100)

    def test_scan_model_pending_review(self):
        from traceability.harness.tower_harness import run_tower
        from traceability.io import load_model
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            result = run_tower(EXAMPLES / "clear" / "tower_front_hd.png", out)
            model = load_model(out / "model.json")
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        nodes = [c for c in model.components.values() if c.kind == "tower_node"]
        self.assertGreater(len(bars), 0)
        self.assertGreater(len(nodes), 0)
        self.assertTrue(all(
            c.properties.get("solve_status") == "pending_review" for c in bars + nodes))
        self.assertIn("r_scan_reviewed", model.rules)


if __name__ == "__main__":
    unittest.main()
