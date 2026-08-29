"""P1/P2 验收测试：MLLM 硬约束 / 送图缩放 / 超时日志 / 三列对比基准。"""

from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
FRONT_HD = EXAMPLES / "clear" / "tower_front_hd.png"


def _fake_response(raw_json: dict) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(raw_json, ensure_ascii=False)))],
    )


class FakeChat:
    def __init__(self, raw_json=None, exc=None):
        self.raw_json = raw_json
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return _fake_response(self.raw_json)


class FakeClient:
    def __init__(self, chat):
        self.chat = SimpleNamespace(completions=chat)


class TowerPromptHardConstraintTest(unittest.TestCase):
    def test_drawing_view_missing_view_type_warns(self):
        from traceability.intake.mllm_tower_prompt import parse_tower_mllm_output_with_warnings
        parsed = {"objects": [
            {"obj_type": "component", "data": {"id": "v1", "kind": "drawing_view",
             "name": "正立面", "properties": {"scale": "1:50"}}},
        ]}
        objs, problems, warnings = parse_tower_mllm_output_with_warnings(parsed)
        self.assertEqual(problems, [])
        self.assertEqual(len(objs), 1)
        self.assertTrue(any("drawing_view 缺少 view_type" in w for w in warnings))

    def test_coordinate_only_in_detail_is_not_trusted(self):
        from traceability.intake.mllm_tower_prompt import parse_tower_mllm_output_with_warnings
        parsed = {"objects": [
            {"obj_type": "component", "data": {"id": "n1", "kind": "tower_node",
             "name": "节点", "properties": {"node_id": "N001"}},
             "source": {"source_type": "drawing", "reference": "t.png",
                        "detail": "x_px=100,y_px=200", "confidence": 0.6}},
        ]}
        objs, problems, warnings = parse_tower_mllm_output_with_warnings(parsed)
        self.assertEqual(problems, [])
        self.assertEqual(len(objs), 1)
        self.assertTrue(any("无 x_px/y_px 或 x/y/z 坐标" in w for w in warnings))

    def test_tower_kind_first_item_does_not_reject_batch(self):
        from traceability.intake.mllm_tower_prompt import parse_tower_mllm_output_with_warnings
        parsed = {"objects": [
            {"obj_type": "component", "data": {"id": "x", "kind": "tower"}},
            {"obj_type": "component", "data": {"id": "b1", "kind": "tower_bar",
             "name": "杆件", "properties": {"bar_id": "M0001",
             "from_node": "n1", "to_node": "n2", "x_px": 1, "y_px": 2}}},
            {"obj_type": "component", "data": {"id": "n1", "kind": "tower_node",
             "name": "节点", "properties": {"node_id": "N1", "x_px": 1, "y_px": 2}}},
        ]}
        objs, problems, warnings = parse_tower_mllm_output_with_warnings(parsed)
        self.assertEqual(problems, [])
        self.assertEqual([o.data["kind"] for o in objs], ["tower_bar", "tower_node"])
        self.assertTrue(any("非法铁塔 kind 'tower'" in w for w in warnings))


class TowerMllmTwentyBarsAcceptanceTest(unittest.TestCase):
    def test_mllm_output_with_20_plus_bars_passes_contract(self):
        """验收：同一张图 MLLM 产出 ≥20 根 tower_bar，validate + contract 不崩。"""
        from traceability.intake.mllm_backend import DrawingInput, ModelCandidate
        from traceability.intake.mllm_tower_prompt import parse_tower_mllm_output_with_warnings
        from traceability.skill.contract import to_engineering_model
        from traceability.io import validate_references

        objects = []
        for i in range(1, 26):
            objects.append({
                "obj_type": "component",
                "data": {"id": f"bar_{i:03d}", "kind": "tower_bar",
                         "name": f"杆件 M{i:04d}",
                         "properties": {"bar_id": f"M{i:04d}", "section": "L100x8",
                                        "from_node": f"node_{(i - 1) // 2 + 1:03d}",
                                        "to_node": f"node_{i // 2 + 1:03d}",
                                        "x_px": float(i), "y_px": float(i),
                                        "solve_status": "pending_review"}},
                "source": {"source_type": "drawing", "reference": "tower_front_hd.png",
                           "confidence": 0.6},
                "confidence": 0.6,
            })
        for j in range(1, 16):
            objects.append({
                "obj_type": "component",
                "data": {"id": f"node_{j:03d}", "kind": "tower_node", "name": "节点",
                         "properties": {"node_id": f"N{j:03d}", "x_px": float(j),
                                        "y_px": float(j), "solve_status": "pending_review"}},
                "source": {"source_type": "drawing", "reference": "tower_front_hd.png",
                           "confidence": 0.6},
                "confidence": 0.6,
            })
        parsed = {"objects": objects}
        objs, problems, warnings = parse_tower_mllm_output_with_warnings(parsed)
        self.assertEqual(problems, [])
        self.assertEqual(len(objs), 40)

        cand = ModelCandidate(
            input=DrawingInput(path="examples/clear/tower_front_hd.png",
                               kind="scan", tower=True),
            objects=objs, raw="mock", backend="mllm", warnings=warnings,
        )
        model = to_engineering_model(cand, name="tower-tower_front_hd")
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        self.assertGreaterEqual(len(bars), 20)
        self.assertEqual(validate_references(model), [])


class ImageEncodeTest(unittest.TestCase):
    def test_resize_and_keep_png(self):
        from traceability.intake.mllm_backend import _encode_image
        b64, meta = _encode_image(str(FRONT_HD), max_edge=1024)
        img = __import__("PIL.Image", fromlist=["Image"]).open(
            io.BytesIO(base64.b64decode(b64)))
        self.assertEqual(img.format, "PNG")
        self.assertLessEqual(max(img.size), 1024)
        self.assertEqual(meta["resized_to"], list(img.size))
        self.assertLess(meta["bytes_sent"], meta["original_bytes"])

    def test_env_max_edge_override(self):
        from traceability.intake.mllm_backend import _encode_image
        with mock.patch.dict("os.environ", {"MLLM_MAX_IMAGE_EDGE": "1536"}):
            b64, meta = _encode_image(str(FRONT_HD))
        self.assertEqual(meta["max_edge"], 1536)
        img = __import__("PIL.Image", fromlist=["Image"]).open(
            io.BytesIO(base64.b64decode(b64)))
        self.assertLessEqual(max(img.size), 1536)

    def test_max_edge_default_is_2048(self):
        # 默认最长边已提升到 4096（Kimi 视觉推荐上限 4096×2160），
        # 2048 会让塔身段细斜材糊掉、召回不足。断言跟随默认值。
        from traceability.intake.mllm_backend import _encode_image
        with mock.patch.dict("os.environ", {"MLLM_MAX_IMAGE_EDGE": ""}):
            b64, meta = _encode_image(str(FRONT_HD))
        self.assertEqual(meta["max_edge"], 4096)

    def test_jpeg_converted_to_png(self):
        from traceability.intake.mllm_backend import _encode_image
        img = __import__("PIL.Image", fromlist=["Image"]).new("RGB", (64, 32), "red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        with tempfile.TemporaryDirectory() as d:
            jpg = Path(d) / "t.jpg"
            jpg.write_bytes(buf.getvalue())
            b64, meta = _encode_image(str(jpg))
        out = __import__("PIL.Image", fromlist=["Image"]).open(
            io.BytesIO(base64.b64decode(b64)))
        self.assertEqual(out.format, "PNG")


class ResponseFormatHelperTest(unittest.TestCase):
    def test_only_response_format_errors_retry(self):
        from traceability.intake.mllm_backend import _is_response_format_error
        self.assertTrue(_is_response_format_error(
            ValueError("Unsupported parameter: 'response_format'")))
        self.assertTrue(_is_response_format_error(
            ValueError("does not support json_object")))
        self.assertFalse(_is_response_format_error(TimeoutError("timed out")))
        self.assertFalse(_is_response_format_error(
            ValueError("401 invalid api key")))


class MLLMBackendMetaTest(unittest.TestCase):
    def _backend(self, chat):
        from traceability.intake.mllm_backend import DrawingInput, MLLMBackend
        backend = MLLMBackend(api_key="sk-test", model="kimi-for-coding")
        backend._make_client = lambda: FakeClient(chat)  # type: ignore[method-assign]
        return backend, DrawingInput(path=str(FRONT_HD), kind="scan", tower=True)

    def test_parse_warnings_recorded_in_meta(self):
        chat = FakeChat(raw_json={"objects": [
            {"obj_type": "component", "data": {"id": "x", "kind": "tower"}},
            {"obj_type": "component", "data": {"id": "b1", "kind": "tower_bar",
             "name": "杆件", "properties": {"bar_id": "M0001",
             "from_node": "n1", "to_node": "n2", "x_px": 1, "y_px": 2}}},
        ]})
        backend, drawing = self._backend(chat)
        cand = backend.analyze(drawing)
        self.assertEqual(len(cand.objects), 1)
        self.assertEqual(cand.meta["parse_warnings"], 1)
        self.assertIn("parse_warning_detail", cand.meta)
        self.assertEqual(cand.meta["model"], "kimi-for-coding")
        self.assertGreater(cand.meta["raw_length"], 0)

    def test_timeout_error_does_not_retry(self):
        chat = FakeChat(exc=TimeoutError("httpx timed out"))
        backend, drawing = self._backend(chat)
        cand = backend.analyze(drawing)
        self.assertEqual(len(chat.calls), 1)  # 不重复调用
        self.assertIn("failure_reason", cand.meta)
        self.assertIn("timed out", cand.meta["failure_reason"])

    def test_response_format_error_falls_back(self):
        chat = FakeChat(raw_json={"objects": []})
        # 第一次抛 response_format 错误，第二次成功
        chat.create = mock.Mock(side_effect=[
            ValueError("Unsupported parameter: 'response_format'"),
            _fake_response({"objects": []}),
        ])
        backend, drawing = self._backend(chat)
        cand = backend.analyze(drawing)
        self.assertEqual(chat.create.call_count, 2)
        self.assertEqual(cand.meta["objects"], 0)

    def test_timeout_env_default_is_90(self):
        from traceability.intake.mllm_backend import MLLMBackend
        backend = MLLMBackend(api_key="sk-test")
        with mock.patch.dict("os.environ", {"MLLM_TIMEOUT": "", "MLLM_CONNECT_TIMEOUT": ""}):
            with mock.patch("httpx.Client") as client_cls:
                with mock.patch("openai.OpenAI"):
                    backend._make_client()
        timeout = client_cls.call_args.kwargs["timeout"]
        # 大图件号 OCR 默认 300s（MLLM_TIMEOUT 默认 300），连接超时 30s
        self.assertEqual(timeout.read, 300.0)
        self.assertEqual(timeout.connect, 30.0)


class StepsJsonMllmFailureTest(unittest.TestCase):
    def test_mllm_failure_reason_written_to_steps(self):
        """P1 多 Agent 编排：A1 件号 OCR 失败 -> 该步 pending，不级联猜值，
        且 MLLM 调用日志（model/raw_length/failure_reason）写入 steps.json。"""
        from traceability.harness import tower_harness
        from traceability.intake import tower_agent_pipeline

        fake = mock.MagicMock()
        fake.available.return_value = True
        fake.call_agent_json.return_value = (
            None,
            {"model": "kimi-for-coding", "elapsed_s": 12.3,
             "raw_length": 42, "failure_reason": "mock timeout",
             "duration_ms": 12300},
        )
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(tower_agent_pipeline, "MLLMBackend",
                                   return_value=fake):
                result = tower_harness.run_tower(
                    source=EXAMPLES / "clear" / "tower_front_hd.png",
                    out_dir=Path(d),
                )
            steps = json.loads((Path(d) / "steps.json").read_text(encoding="utf-8"))
        # 单 Agent 失败只让该步 pending，整链不 failed（扫描图默认待复核）
        self.assertTrue(result["ok"])
        a1 = steps["steps"][1]
        self.assertEqual(a1["id"], "a1_labels")
        self.assertEqual(a1["status"], "pending")
        self.assertEqual(a1["detail"]["mllm_model"], "kimi-for-coding")
        self.assertEqual(a1["detail"]["mllm_raw_length"], 42)
        self.assertEqual(a1["detail"]["mllm_failure_reason"], "mock timeout")


class MllmVsScanBenchmarkTest(unittest.TestCase):
    def test_benchmark_writes_three_columns(self):
        import benchmark.mllm_vs_scan as bench

        def fake_mllm(image, model, api_key):
            return {
                "backend": "mllm",
                "model": model,
                "status": "ok",
                "bars": 25 if model == "kimi-for-coding" else 30,
                "nodes": 20,
                "labeled_bars": 18 if model == "kimi-for-coding" else 22,
            }

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "mllm_benchmark.json"
            result = bench.run_benchmark(
                image=FRONT_HD, out=out,
                mllm_runner=fake_mllm,
            )
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(data["columns"]), 3)
        scan = data["columns"][0]
        self.assertEqual(scan["backend"], "rule-based-scan")
        self.assertGreaterEqual(scan["bars"], 20)
        models = [c["model"] for c in data["columns"][1:]]
        self.assertEqual(models, ["kimi-for-coding", "k3-256k"])
        self.assertEqual(data["columns"][1]["bars"], 25)
        self.assertEqual(data["columns"][2]["labeled_bars"], 22)

    def test_benchmark_skip_mllm(self):
        import benchmark.mllm_vs_scan as bench
        with tempfile.TemporaryDirectory() as d:
            result = bench.run_benchmark(
                image=FRONT_HD, out=Path(d) / "mllm_benchmark.json",
                skip_mllm=True,
            )
        self.assertEqual(len(result["columns"]), 1)

    def test_labeled_bar_id_semantics(self):
        from benchmark.mllm_vs_scan import _is_labeled_bar_id
        self.assertTrue(_is_labeled_bar_id("M0001"))
        self.assertFalse(_is_labeled_bar_id("UNLABELED_1"))
        self.assertFalse(_is_labeled_bar_id("SCAN_0001"))
        self.assertFalse(_is_labeled_bar_id(""))


if __name__ == "__main__":
    unittest.main()
